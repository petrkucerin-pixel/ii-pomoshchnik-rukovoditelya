"""Данные кабинета руководителя: разделы по стр. 3 ТЗ. Только чтение баз и сборка JSON, решения — в помощнике.

Контракт ответов — TASK_08_КАБИНЕТ_GPT.md и tests/fixtures/cabinet/.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from yadro.journal import from_db

from .assistant import Assistant, digest_from_journal, is_mail_route, meeting_brief, protocol_tasks
from .memory import collapse, period, topics, title_of
from .metrics import compute_metrics
from .plan import Meeting, TaskRow, build_day_plan, focus_windows
from .protocol import questions_from_previous, render_markdown

GROUPS = [{"key": "counterparty", "title": "Контрагенты"},
          {"key": "team", "title": "Отделы: участники совещаний"},
          {"key": "internal", "title": "Отделы: другие"},
          {"key": "newsletter", "title": "Рассылки"},
          {"key": "spam", "title": "Спам"}]
TEAM_MEETING_CATEGORY = "операционная"
NEWSLETTER = {"рассылка": "рассылка", "приглашение": "приглашение"}
CLIENT_CATEGORIES = {"обязательства_клиентам", "продажи", "клиенты"}
DONE = ("готово", "закрыта", "выполнена")
_PARTY = re.compile(r"«([^»]+)»")


def _iso(v) -> str | None:
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


# ---------------- переписка ----------------
class Grouper:
    """Группа письма по собеседнику — правилами в коде, без модели."""

    def __init__(self, a: Assistant):
        self.org = a.p.org
        team_names = {n for m in a.meetings if m.category == TEAM_MEETING_CATEGORY for n in m.attendees}
        self.team = {p.id for p in (self.org.people if self.org else []) if p.name in team_names}
        self.external_names: dict[str, str] = {}

    def person(self, email: str):
        return self.org.find_by_email(email) if self.org and email else None

    def party_of_external(self, email: str, name: str = "") -> str:
        m = _PARTY.search(name or "") or _PARTY.search(self.external_names.get(email.lower(), ""))
        if m:
            return m.group(1)
        if name:
            return name
        domain = email.split("@")[-1]
        return domain.split(".")[0] if domain else email

    def classify(self, email: str, name: str, category: str) -> tuple[str, str, str]:
        if category == "спам":
            return "spam", self.party_of_external(email, name), "спам"
        if category in NEWSLETTER:
            return "newsletter", self.party_of_external(email, name), NEWSLETTER[category]
        p = self.person(email)
        if p:
            return ("team" if p.id in self.team else "internal"), p.name, p.position
        return "counterparty", self.party_of_external(email, name), \
            "клиент" if category in CLIENT_CATEGORIES else "контрагент"


def _letter_status(a: Assistant, act: dict | None, ap: dict, verdicts: dict, held: set, released: dict,
                   deferred: dict) -> dict:
    """Что с письмом — одной плашкой: you — ждёт руководителя, wait — ждёт его решения по другому вопросу,
    done — помощник (или руководитель) уже сделал, info — для сведения, ничего делать не нужно."""
    if not act:
        return {"kind": "info", "label": "Ещё не разобрано"}
    kind, eid = act["kind"].removeprefix("mail_"), act["event_id"]
    reason = ap.get("reason") or ""
    if kind == "спросить_руководителя":
        v = verdicts.get(eid)
        if v == "yes":
            return {"kind": "done", "label": "Вы согласовали — ответ отправлен"}
        if v == "no":
            return {"kind": "done", "label": "Вы отказали — ответ отправлен"}
        until = deferred.get(eid)
        if until:
            return {"kind": "you", "label": f"Ждёт вашего решения · отложено до {until:%d.%m %H:%M}"}
        return {"kind": "you", "label": "Ждёт вашего решения"}
    if kind == "делегировать":
        who = a.p.org.by_id.get(ap.get("delegate_to")) if a.p.org else None
        name = who.name if who else "исполнителю"
        if eid in held and eid not in released:
            return {"kind": "wait", "label": f"Передам {name} после вашего кадрового решения"}
        if released.get(eid) == "no":
            return {"kind": "done", "label": "Не передано — вы отказали по связанному вопросу"}
        return {"kind": "done", "label": f"Передал {name}"}
    if kind == "авто":
        return {"kind": "done", "label": "Ответил сам"}
    if kind == "в_сводку":
        if "ответ исполнителя" in reason:
            return {"kind": "done", "label": "Ответ на вопрос помощника — учтён в задаче и памяти"}
        if "служебн" in reason:
            return {"kind": "info", "label": "Служебное уведомление — никому не пересылал"}
        return {"kind": "info", "label": "Прочитал — в сводке"}
    if kind == "уточнить":
        return {"kind": "done", "label": "Спросил отправителя, что нужно — ждём ответ"}
    if kind == "игнорировать":
        return {"kind": "info", "label": "Пропущено: спам" if ap.get("category") == "спам" else "Пропущено: рассылка"}
    return {"kind": "info", "label": "Разобрано"}


def _incoming(a: Assistant) -> list[dict]:
    rows = a.j.conn.execute("SELECT id, external_id, received_at, payload FROM events WHERE channel = 'mail' ORDER BY id").fetchall()
    acts: dict[int, dict] = {}
    verdicts: dict[int, str] = {}
    held: set[int] = set()
    released: dict[int, str] = {}
    for x in a.j.actions():
        pl = x["payload"] or {}
        if is_mail_route(x["kind"]) and x["event_id"]:
            acts[x["event_id"]] = x
            if x["kind"] == "mail_делегировать" and pl.get("after"):
                held.add(x["event_id"])
        elif x["kind"] == "letter_decided" and pl.get("verdict") in ("yes", "no"):
            verdicts[x["event_id"]] = pl["verdict"]
        elif x["kind"] == "delegation_released":
            released[x["event_id"]] = pl.get("verdict")
    now = a.clock.now()
    deferred = {eid: datetime.fromisoformat(g["deferred_until"]) for g in a.groups() if g.get("deferred_until")
                and datetime.fromisoformat(g["deferred_until"]) > now for eid in g["event_ids"]}
    out = []
    gone, seen = deleted_letters(a), read_letters(a)
    for r in rows:
        pl = json.loads(r["payload"])
        act = acts.get(r["id"])
        ap = (act or {}).get("payload") or {}
        subject = pl.get("subject", "")
        out.append({"id": r["external_id"], "event_id": r["id"], "direction": "in",
                    "at": pl.get("received") or _iso(act["at"] if act else None),
                    "from_name": pl.get("from_name") or pl.get("from", ""), "from_email": pl.get("from", ""),
                    "to_name": a.p.name, "to_email": pl.get("to") or "",
                    "subject": subject, "body": pl.get("body", ""),
                    "category": ap.get("category", ""), "action": act["kind"].removeprefix("mail_") if act else None,
                    "reason": ap.get("reason") or (act["summary"].removeprefix(subject + " — ") if act else ""),
                    "status": _letter_status(a, act, ap, verdicts, held, released, deferred),
                    "deleted": r["external_id"] in gone, "attachments": pl.get("attachments") or [],
                    "arrived": _iso(from_db(r["received_at"], a.j.tz)), "seq": r["id"]})
        # Непрочитанное — только то, что ждёт руководителя и ещё не открыто: остальное помощник уже сделал сам.
        out[-1]["read"] = out[-1]["status"]["kind"] not in ("you", "wait") or r["external_id"] in seen
    return out


# Кто инициатор исходящего письма — метка в «Отправленных», как категория в Outlook.
SENT_LABELS = {"передача": ("assistant", "Помощник передал исполнителю"), "автоответ": ("assistant", "Помощник ответил сам"),
               "вопрос_ревизора": ("assistant", "Помощник: вопрос по отчёту"),
               "выяснение": ("assistant", "Помощник: что с задачей"), "можно_начинать": ("assistant", "Помощник: можно начинать"),
               "решение": ("you", "По вашему решению"), "протокол": ("you", "Протокол после вашего утверждения"),
               "приглашение": ("you", "Ваше приглашение на встречу"), "напоминание": ("you", "Напоминание по вашей кнопке"),
               "поручение": ("you", "Ваше поручение"), "ваше_письмо": ("you", "Вы написали"),
               "из_почты": ("you", "Вы отправили из почты"),
               "уточнение": ("assistant", "Помощник: уточнил, что нужно")}


def read_letters(a: Assistant) -> set[str]:
    """Письма, которые руководитель открыл в кабинете."""
    return {x["payload"]["id"] for x in a.j.actions() if x["kind"] == "mail_read"}


def deleted_letters(a: Assistant) -> set[str]:
    """Письма, которые руководитель удалил в кабинете (последнее действие — удалить, а не восстановить)."""
    state: dict[str, bool] = {}
    for x in a.j.actions():
        if x["kind"] in ("mail_deleted", "mail_restored"):
            state[x["payload"]["id"]] = x["kind"] == "mail_deleted"
    return {k for k, v in state.items() if v}


def drafts(a: Assistant) -> dict:
    """Черновики руководителя: последнее сохранение каждого, удалённые и отправленные — нет."""
    cur: dict[str, dict] = {}
    for x in a.j.actions():
        pl = x["payload"] or {}
        if x["kind"] == "draft_saved":
            cur[pl["draft_id"]] = {**pl, "at": _iso(x["at"])}
        elif x["kind"] == "draft_deleted":
            cur.pop(pl.get("draft_id"), None)
    return {"drafts": sorted(cur.values(), key=lambda d: d["at"], reverse=True)}


def reply_draft(a: Assistant, letter_id: str) -> dict | None:
    """Готовый текст ответа на входящее письмо. Письмо ждёт решения руководителя — два варианта (согласие и отказ)
    из карточки решения; иначе — один ответ от модели в стиле руководителя. Готовый ответ запоминается в журнале."""
    row = a.j.conn.execute("SELECT id, payload FROM events WHERE channel = 'mail' AND external_id = ?",
                           (letter_id,)).fetchone()
    if not row:
        return None
    eid, pl = row["id"], json.loads(row["payload"])
    g = next((x for x in a.groups() if eid in x["event_ids"]), None)
    if g:
        if not g["prepared"]:
            a.prepare_decisions(only={g["key"]})
            g = next(x for x in a.groups() if x["key"] == g["key"])
        p = g["prepared"] or {}
        return {"mode": "decision", "event_id": g["event_id"], "advice": p.get("advice", "нет совета"),
                "yes": p.get("draft_yes") or p.get("draft") or "", "no": p.get("draft_no") or "",
                "unverified": p.get("unverified", [])}
    for x in reversed(a.j.actions()):
        if x["kind"] == "reply_draft" and x["payload"].get("letter_id") == letter_id:
            return {"mode": "reply", "text": x["payload"]["text"], "unverified": x["payload"].get("unverified", [])}
    from . import decisions as dq
    from .writer import audience_of
    letter = f"{pl.get('subject', '')}\n{pl.get('body', '')}"
    srcs = dq.sources(letter, goals=a.p.goals, tasks=a.tasks, meetings=a.meetings, memory=a.memory, org=a.p.org,
                      sender_email=pl.get("from", ""), today=a.today(), exclude={letter_id})
    rules = ""
    try:
        rules = a.writer.rules_text("reply", audience_of(a.p.org, pl.get("from", "")))
    except Exception:                    # правила стиля — подсказка, без них ответ всё равно пишется
        rules = ""
    d = dq.reply_draft(a.llm, pl.get("from_name") or pl.get("from", ""), letter, srcs, a.p.style, a.p.name, rules)
    a._log("reply_draft", path="now", event_id=eid, summary=f"черновик ответа: {pl.get('subject', '')}",
           payload={"letter_id": letter_id, "text": d["text"], "unverified": d["unverified"]})
    return {"mode": "reply", **d}


def contacts(a: Assistant) -> dict:
    """Адресная книга: оргструктура и все, кто нам писал. Себя руководитель в ней не видит."""
    out: dict[str, dict] = {}
    me = a.p.org.email_of(a.p.org.leader).lower() if a.p.org else ""
    for p in (a.p.org.people if a.p.org else []):
        e = a.p.org.email_of(p).lower()
        out[e] = {"name": p.name, "email": e, "note": p.position}
    for r in a.j.conn.execute("SELECT payload FROM events WHERE channel = 'mail'").fetchall():
        pl = json.loads(r["payload"])
        e = (pl.get("from") or "").lower()
        if e and e not in out and "@" in e:
            out[e] = {"name": pl.get("from_name") or e, "email": e, "note": "писал(а) нам"}
    out.pop(me, None)
    return {"contacts": sorted(out.values(), key=lambda x: x["name"].lower())}


def _outgoing(a: Assistant) -> list[dict]:
    out, gone = [], deleted_letters(a)
    ext = {}
    for x in a.j.actions():
        if x["kind"] != "mail_sent":
            continue
        pl = x["payload"]
        t = a.templates.get(pl["template"]) if pl.get("template") else None
        eid = x["event_id"]
        if eid and eid not in ext:
            ext[eid] = (a.j.event(eid) or {}).get("external_id")
        by, label = SENT_LABELS.get(pl.get("kind", ""), ("assistant", "Отправил помощник"))
        if pl.get("reply_to_id"):            # письмо руководителя из кабинета: на какое входящее отвечает
            ext[eid] = pl["reply_to_id"]
        out.append({"id": f"out-{x['id']}", "direction": "out", "at": pl.get("sent_at") or _iso(x["at"]),
                    "from_name": a.signature.removeprefix("— "), "from_email": a.p.org.email_of(a.p.org.leader)
                    if a.p.org else "", "to_name": "", "to_email": pl.get("to", ""),
                    "subject": pl.get("subject", ""), "body": pl.get("body", ""), "category": pl.get("kind", ""),
                    "action": "отправлено", "reason": f"шаблон «{t.title}»" if t else "",
                    "reply_to": pl.get("reply_to_id") or (ext.get(eid) if eid else None),
                    "status": {"kind": by, "label": label}, "deleted": f"out-{x['id']}" in gone,
                    "attachments": pl.get("attachments") or []})
    return out


def mail(a: Assistant) -> dict:
    g = Grouper(a)
    inc, out = _incoming(a), _outgoing(a)
    for x in inc:
        if not g.person(x["from_email"]):
            g.external_names.setdefault(x["from_email"].lower(), x["from_name"])
    for x in inc:
        x["group"], x["party"], x["party_note"] = g.classify(x["from_email"], x["from_name"], x["category"])
        x.pop("event_id")
    for x in out:
        p = g.person(x["to_email"])
        # Внешний получатель — имя из его писем, иначе адрес целиком (а не домен «yandex»).
        x["to_name"] = p.name if p else (g.external_names.get(x["to_email"].lower()) or x["to_email"])
        x["group"], x["party"], x["party_note"] = g.classify(x["to_email"], g.external_names.get(
            x["to_email"].lower(), ""), "")
    letters = sorted(inc + out, key=lambda x: x["at"] or "")
    kinds = [x["status"]["kind"] for x in inc]
    counts = {"you": len(a.groups()), "you_letters": kinds.count("you"), "wait": kinds.count("wait"),
              "done": kinds.count("done"), "info": kinds.count("info")}
    return {"groups": GROUPS, "letters": letters, "counts": counts}


# ---------------- сегодня, задачи ----------------
def why_late(t: TaskRow, today: date, source: str) -> str:
    """Почему задача считается просроченной — одной строкой; пусто, если не просрочена."""
    if t.status in DONE:
        return ""
    if t.due < today:
        return f"срок {t.due:%d.%m} прошёл {(today - t.due).days} дн. назад, отметки о выполнении нет"
    if t.status == "просрочена":
        where = "в файле задач заказчика" if source == "tasks.csv" else f"в {source}"
        return f"статус «просрочена» стоит {where}, хотя срок {t.due:%d.%m} ещё не наступил"
    return ""


def task_dict(t: TaskRow, today: date, source: str = "tasks.csv", asked: dict | None = None) -> dict:
    why = why_late(t, today, source)
    if why and asked and t.id in asked:
        why += f" · помощник выясняет у исполнителя (спросил {asked[t.id]:%d.%m})"
    return {"id": t.id, "task": t.task, "owner": t.owner, "due": t.due.isoformat(), "status": t.status,
            "overdue": t.status not in DONE and (t.status == "просрочена" or t.due < today), "source": source,
            "why": why}


def asked_tasks(a: Assistant) -> dict[str, datetime]:
    """Когда помощник (или руководитель кнопкой) последний раз спрашивал исполнителя о задаче."""
    out: dict[str, datetime] = {}
    for x in a.j.actions():
        pl = x["payload"] or {}
        if x["kind"] == "task_action" and pl.get("action") in ("ask", "remind"):
            out[pl["task_id"]] = x["at"]
    return out


ASK_AGAIN = timedelta(days=2)


def _question(t: TaskRow, today: date) -> str:
    if t.due < today:
        return (f"по задаче «{t.task}» срок {t.due:%d.%m} прошёл. На каком этапе работа и какой новый срок "
                f"реальный?")
    return (f"в списке задач у «{t.task}» статус «просрочена», а срок стоит {t.due:%d.%m}. Что не так: срок "
            f"сдвинулся или что-то мешает?")


def chase_overdue(a: Assistant) -> int:
    """Помощник сам выясняет у исполнителей, что с просроченными задачами: одно письмо, повтор не раньше чем
    через 2 дня. Задачи самого руководителя не трогает. Возвращает число отправленных вопросов."""
    today, now, asked, n = a.today(), a.clock.now(), asked_tasks(a), 0
    for t, src in all_tasks(a):
        if not why_late(t, today, src) or t.owner == a.p.name:
            continue
        if t.id in asked and now - asked[t.id] < ASK_AGAIN:
            continue
        if a.ask_about_task(t.id, t.task, t.owner, t.due, _question(t, today)):
            n += 1
    return n


def plan_conflicts(a: Assistant, t: TaskRow, new: date) -> list[str]:
    """Расходится ли новый срок с планами: прошлая дата, встреча по той же теме раньше срока, задача-последователь
    из протокола с более ранним сроком."""
    today = a.today()
    now = datetime.combine(today, a.clock.now().timetz())      # прошедшие сегодня встречи не в счёт
    out = []
    if new <= today:
        out.append(f"дата {new:%d.%m} уже прошла")
    key = _entity(t.task).lower()
    if key:
        for m in a.meetings:
            if key in m.title.lower() and m.start >= now and m.start.date() < new:
                out.append(f"встреча «{m.title}» {m.start:%d.%m} раньше нового срока")
                break
    pt = protocol_tasks(a.j)
    me = next((x for x in pt if f"p{x['protocol_id']}-{x['action_id']}" == t.id), None)
    if me:
        for x in pt:
            if x["protocol_id"] == me["protocol_id"] and me["n"] in x["after"] and x["due"] < new:
                out.append(f"после неё идёт «{x['what']}» со сроком {x['due']:%d.%m}")
    return out


def apply_answer_date(a: Assistant, asked_subject: str, body: str, owner: str) -> dict | None:
    """Исполнитель назвал дату в ответе: срок переносится сам, если не расходится с планами; иначе — предложение
    руководителю на странице задачи. Возвращает, что сделано, или None — даты в ответе нет."""
    from yadro.deadlines import parse_deadline
    m = re.match(r"Задача «(.+)» — срок", asked_subject)
    t = next((x for x, _ in all_tasks(a) if m and x.task == m.group(1) and x.owner == owner and x.status not in DONE),
             None)
    if not t:
        return None
    dl = parse_deadline(body, datetime.combine(a.today(), a.clock.now().timetz()))
    if not dl:
        return None
    new = dl.due.date()
    if new == t.due:
        return {"action": "same", "due": new}
    conflicts = plan_conflicts(a, t, new)
    if conflicts:
        a._log("task_action", path="decision", summary=f"{owner} предлагает срок «{t.task}» {new:%d.%m}, но "
               + "; ".join(conflicts) + " — решаете вы",
               payload={"task_id": t.id, "action": "proposal", "due": new.isoformat(), "conflicts": conflicts})
        return {"action": "proposal", "due": new, "conflicts": conflicts}
    a._log("task_action", path="now", summary=f"Перенёс срок «{t.task}» на {new:%d.%m}: так ответил исполнитель "
           f"({owner}), с планами не расходится",
           payload={"task_id": t.id, "action": "move", "due": new.isoformat(), "by": "answer", "minutes_saved": 3})
    return {"action": "move", "due": new}


def task_questions(a: Assistant, names: set[str]) -> list[str]:
    """Для встречи: по задачам участников, о которых помощник спрашивал, — что ответили или что ответа нет."""
    asked, out = asked_tasks(a), []
    for t, _ in all_tasks(a):
        if t.id not in asked or t.owner not in names or t.status in DONE:
            continue
        ans = _answer(a, t, asked[t.id])
        if ans:
            body = " ".join(ans["body"].split())[:180]
            out.append(f"{t.owner} — {t.task}: ответ от {ans['at'][8:10]}.{ans['at'][5:7]}: «{body}». "
                       f"Подтвердите срок и следующий шаг.")
        else:
            out.append(f"{t.owner} — {t.task}: помощник спрашивал {asked[t.id]:%d.%m}, ответа нет. "
                       f"Что с задачей и какой срок реальный?")
    return out


def _answer(a: Assistant, t: TaskRow, since: datetime) -> dict | None:
    """Ответ исполнителя после вопроса: письмо от него с темой задачи («Re: Задача «…»»)."""
    who = next((p for p in a.p.org.people if p.name == t.owner), None) if a.p.org else None
    if not who:
        return None
    email = a.p.org.email_of(who).lower()
    for x in reversed(a.j.actions()):
        if x["at"] < since:
            break
        if not (is_mail_route(x["kind"]) and x["event_id"]):
            continue
        ev = (a.j.event(x["event_id"]) or {}).get("payload") or {}
        if (ev.get("from") or "").lower() == email and t.task.lower() in (ev.get("subject") or "").lower():
            return {"at": x["at"].isoformat(), "subject": ev.get("subject", ""), "body": ev.get("body", "")}
    return None


TASK_ACTIONS = ("remind", "move", "done")
ROUTE_LABELS = {"делегировать": "помощник передал исполнителю", "авто": "помощник ответил сам",
                "в_сводку": "в сводку", "игнорировать": "пропущено", "спросить_руководителя": "ждёт вашего решения"}


def task_history(a: Assistant, task_id: str) -> list[dict]:
    return [{"action": x["payload"]["action"], "due": x["payload"].get("due"), "at": x["at"].isoformat(),
             "summary": x["summary"]}
            for x in a.j.actions() if x["kind"] == "task_action" and x["payload"].get("task_id") == task_id]


def all_tasks(a: Assistant) -> list[tuple[TaskRow, str]]:
    """Задачи из файла заказчика и из утверждённых протоколов, с решениями руководителя из кабинета поверх."""
    rows: list[tuple[TaskRow, str]] = [(t, "tasks.csv") for t in a.tasks]
    for t in protocol_tasks(a.j):       # статусы со сверки на следующих встречах и порядок «ждёт задачи N»
        rows.append((TaskRow(f"p{t['protocol_id']}-{t['action_id']}", t["what"], t["owner"], t["due"], t["status"]),
                     f"протокол {t['protocol_id']}"))
    over: dict[str, dict] = {}
    for x in a.j.actions():
        pl = x["payload"] or {}
        if x["kind"] == "task_action" and pl.get("action") in ("move", "done"):
            o = over.setdefault(pl["task_id"], {})
            if pl["action"] == "move":
                o.update(due=date.fromisoformat(pl["due"]), status="перенесена")
            else:
                o["status"] = "выполнена"
    out = []
    for t, src in rows:
        o = over.get(t.id)
        out.append((TaskRow(t.id, t.task, t.owner, o.get("due", t.due), o.get("status", t.status)) if o else t, src))
    return out


def _entity(text: str) -> str:
    m = re.search(r"«([^»]+)»", text)
    return m.group(1) if m else ""


def task_page(a: Assistant, task_id: str) -> dict | None:
    """Задача целиком: суть, что известно (письма, память), противоречия и что делать дальше."""
    today = a.today()
    found = next(((t, src) for t, src in all_tasks(a) if t.id == task_id), None)
    if not found:
        return None
    t, src = found
    key = _entity(t.task)
    letters, seen = [], set()
    if key:
        for x in a.j.actions():
            if not (is_mail_route(x["kind"]) and x["event_id"]) or x["event_id"] in seen:
                continue
            ev = (a.j.event(x["event_id"]) or {}).get("payload") or {}
            if key.lower() in f"{ev.get('from_name', '')} {ev.get('subject', '')} {ev.get('body', '')}".lower():
                seen.add(x["event_id"])
                letters.append({"from": ev.get("from_name") or ev.get("from", ""), "at": ev.get("received"),
                                "subject": ev.get("subject", ""),
                                "route": ROUTE_LABELS.get(x["kind"].removeprefix("mail_"), "")})
    facts = []
    if key and a.memory is not None:
        facts = [{"when": f.when.date().isoformat(), "who": f.who, "what": f.what, "quote": f.quote, "source": f.source}
                 for f in a.memory.search(key)[-5:]]
    flags = []                          # почему просрочена — в task["why"], здесь только дополнительное
    late = t.status not in DONE and (t.due < today or t.status == "просрочена")
    if letters:
        flags.append(f"По теме «{key}» писем: {len(letters)}, последнее — {letters[-1]['from']}.")
    hist = task_history(a, task_id)
    asked = asked_tasks(a)
    answer = _answer(a, t, asked[task_id]) if task_id in asked else None
    proposal = None
    for h in hist:                       # последнее предложение исполнителя, если после него срок не переносили
        if h["action"] == "proposal":
            proposal = {"due": h["due"], "text": h["summary"]}
        elif h["action"] in ("move", "done"):
            proposal = None
    now = datetime.combine(today, a.clock.now().timetz())       # прошедшие сегодня встречи не предлагаем
    meet = next((m for m in a.meetings if m.start >= now and t.owner in m.attendees), None)
    where = f" на встрече «{meet.title}» {meet.start:%d.%m %H:%M}" if meet else " один на один"
    if t.status in DONE:
        text = "Задача закрыта, ничего делать не нужно."
    elif proposal:
        text = (f"{proposal['text']}. Нажмите «Перенести срок» — дата уже подставлена — или обсудите{where}.")
    elif answer and any(h["action"] == "move" and h["at"] >= asked[task_id].isoformat() for h in hist):
        text = f"Исполнитель ({t.owner}) назвал новый срок, помощник перенёс его сам: с планами не расходится."
    elif answer:
        text = (f"Ответ пришёл от исполнителя ({t.owner}) — прочитайте ниже и решите: перенести срок или "
                f"отметить выполненной.")
    elif late and task_id in asked:
        text = (f"Помощник спросил исполнителя ({t.owner}) {asked[task_id]:%d.%m}, ждём ответ — от вас ничего не нужно. "
                f"Если ответа не будет 2 дня, помощник спросит ещё раз; можно обсудить{where}.")
    elif late:
        text = f"Помощник сам спросит исполнителя ({t.owner}), что с задачей, — ответ появится здесь."
    else:
        text = f"Срок {t.due:%d.%m}, всё в рамках — вмешиваться не нужно."
    return {"task": task_dict(t, today, src, asked), "letters": letters[-6:], "facts": facts, "flags": flags,
            "history": hist, "answer": answer, "proposal": proposal, "next": {"text": text, "actions": list(TASK_ACTIONS),
                                      "meeting": {"uid": meet.uid, "title": meet.title, "start": meet.start.isoformat()}
                                      if meet else None}}


def tasks(a: Assistant) -> dict:
    today = a.today()
    asked = asked_tasks(a)
    return {"tasks": [task_dict(t, today, src, asked) for t, src in all_tasks(a)]}


def meeting_short(m: Meeting, a: Assistant, plist: list[dict] | None = None, links: dict | None = None) -> dict:
    p = _protocol_for(m, plist if plist is not None else _protocol_rows(a))
    if links is None:
        from .robot.manager import links_from_journal
        links = links_from_journal(a.j)
    from .lead import avatar_leads, default_categories, lead_overrides
    emails = {}
    if a.p.org:
        emails = {p.name: a.p.org.email_of(p) for p in a.p.org.people}
    by_avatar = avatar_leads(m, lead_overrides(a.j), default_categories())
    return {"uid": m.uid, "start": m.start.isoformat(), "end": m.end.isoformat(), "title": m.title,
            "category": m.category, "avatar": by_avatar,
            "avatar_at": (m.start - timedelta(minutes=1)).isoformat() if by_avatar else None,
            "attendees": [{"name": n, "email": emails.get(n, n if "@" in n else "")} for n in m.attendees],
            "protocol_id": p["id"] if p else None, "protocol_status": p["status"] if p else None,
            "link": links.get(m.uid)}


def risk_items(a: Assistant, today: date, days: int = 7) -> list[dict]:
    """Риски с подробностями: из какого отчёта, что помощник уже сделал (кому и что написал в тот же день)."""
    acts = [x for x in a.j.actions() if x["kind"] == "report_review"]
    reports = {r.id: r for r in a.reports}
    out, seen = [], set()
    for x in acts:
        pl = x["payload"] or {}
        if pl.get("kind") != "риск_цели" or x["summary"] in seen:
            continue
        if not 0 <= (today - date.fromisoformat(pl["day"])).days <= days:
            continue
        seen.add(x["summary"])
        srcs = [{"id": r.id, "from": r.from_name, "at": r.received.isoformat(), "subject": r.subject, "body": r.body}
                for r in (reports.get(s) for s in pl.get("sources", [])) if r]
        # Только то, что относится к этому риску: тот же отчёт или вопрос его автору.
        authors = {s["from"] for s in srcs}
        ids = set(pl.get("sources", []))
        done = [{"to_name": y["payload"].get("to_name"), "topic": y["payload"].get("topic"),
                 "question": y["payload"].get("question")}
                for y in acts if y["payload"].get("day") == pl["day"] and y["payload"].get("to")
                and y["payload"].get("question")
                and (ids & set(y["payload"].get("sources", [])) or y["payload"].get("to_name") in authors)]
        out.append({"text": x["summary"], "day": pl["day"], "leader": pl.get("leader", ""), "sources": srcs,
                    "actions": done, "next": _risk_next(a, x["summary"], authors, done, today)})
    return out


def _risk_next(a: Assistant, text: str, authors: set[str], done: list[dict], today: date) -> dict:
    """Что делать руководителю: суть одной строкой, сейчас ли нужно его участие, где обсудить."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", text)
    gap = f"До плана не хватает {100 - float(m.group(1).replace(',', '.')):g} %. " if m and float(m.group(1).replace(",", ".")) < 100 else ""
    who = ", ".join(sorted(authors)) or "исполнителя"
    now = datetime.combine(today, a.clock.now().timetz())      # учебный день + текущее время: прошедшие встречи не берём
    meet = next((x for x in a.meetings if x.start >= now and authors & set(x.attendees)), None)
    if done:
        asked = ", ".join(sorted({d["to_name"].split()[0] for d in done}))
        msg = f"Сейчас от вас ничего не нужно: помощник уже запросил подробности ({asked}) и ответы пришлёт в сводку."
    else:
        msg = f"Помощник исполнителям не писал — спросите сами: {who}."
    then = (f"Если ответа нет или прогноз не растёт — обсудите на встрече «{meet.title}» {meet.start:%d.%m в %H:%M} "
            f"(участник: {who}).") if meet else f"Если ответа нет — назначьте встречу (участник: {who})."
    return {"text": gap + msg, "then": then,
            "meeting": {"uid": meet.uid, "title": meet.title, "start": meet.start.isoformat()} if meet else None}


def _risks(a: Assistant, today: date, days: int = 7) -> list[str]:
    out = []
    for x in a.j.actions():
        pl = x["payload"] or {}
        if x["kind"] == "report_review" and pl.get("kind") == "риск_цели":
            if 0 <= (today - date.fromisoformat(pl["day"])).days <= days and x["summary"] not in out:
                out.append(x["summary"])
    return out


def _focus(meetings: list[Meeting], day: date) -> list[dict]:
    return [{"start": s.isoformat(), "end": e.isoformat(), "title": "Окно для фокуса"} for s, e in focus_windows(meetings, day)]


def today(a: Assistant, protocols) -> dict:
    day = a.today()
    plan = build_day_plan(day, a.meetings, [t for t, _ in all_tasks(a)])   # с решениями из кабинета поверх
    plist = _protocol_rows(a)
    return {"day": day.isoformat(), "leader": a.p.name,
            "metrics": compute_metrics(a.j, protocols)["cards"],
            "decisions": [{k: g[k] for k in ("event_id", "at", "from", "subject", "summary", "reason", "count",
                                              "prepared")}
                          for g in a.groups()],
            "risks": _risks(a, day), "risk_items": risk_items(a, day),
            "meetings": [meeting_short(m, a, plist) for m in plan.meetings],
            # Пит 28.11: «не встречи дня, а просто встречи» — что впереди, с этой минуты, по дням
            "upcoming": [meeting_short(m, a, plist) for m in sorted(a.meetings, key=lambda m: m.start)
                         if m.end > a.now()][:8],
            "overdue": [task_dict(t, day, asked=asked_tasks(a)) for t in plan.overdue],
            "due_soon": [task_dict(t, day) for t in plan.due_soon],
            "focus": _focus(a.meetings, day),
            **_digest_today(a)}


def _digest_today(a: Assistant) -> dict:
    """Сводка «что сделал без вас» — за сегодня (настоящий день); раньше — числом со ссылкой в журнал (Пит 28.11)."""
    now = a.clock.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_ = digest_from_journal(a.j, a.p.org, since=start)
    earlier = len(digest_from_journal(a.j, a.p.org)) - len(today_)
    return {"digest": today_[-10:], "digest_earlier": max(0, earlier), "updated_at": now.isoformat(timespec="minutes")}


# ---------------- совещания и протоколы ----------------
def protocol_dict(a: Assistant, pid: int) -> dict:
    rec = a.protocols.get(pid)
    p = a.protocols.protocol(pid, a.p.org)
    return {"id": pid, "title": rec["title"], "status": rec["status"], "meeting_end": rec["meeting_end"].isoformat(),
            "meeting_uid": rec["meeting_uid"], "minutes_to_draft": round(rec["minutes_to_draft"], 1),
            "minutes_to_approved": round(rec["minutes_to_approved"], 1) if rec["minutes_to_approved"] is not None else None,
            "edits": rec["edits"],
            "decisions": [{"text": d.text, "quote": d.quote, "quote_ok": d.quote_ok} for d in p.decisions],
            "tasks": [{"owner": t.owner, "what": t.what, "due": t.due.isoformat(), "quote": t.quote,
                       "quote_ok": t.quote_ok, "flags": t.flags} for t in p.tasks],
            "open_questions": p.open_questions, "markdown": render_markdown(p)}


def _protocol_rows(a: Assistant) -> list[dict]:
    return [a.protocols.get(pid) for pid in a.protocols.ids()]


def _protocol_for(m: Meeting, plist: list[dict]) -> dict | None:
    linked = [p for p in plist if p["meeting_uid"] == m.uid] or \
        [p for p in plist if not p["meeting_uid"] and p["title"] == m.title and p["meeting_end"].date() == m.start.date()]
    return linked[-1] if linked else None


def protocols(a: Assistant) -> dict:
    return {"protocols": [protocol_dict(a, pid) for pid in reversed(a.protocols.ids())]}


def meetings(a: Assistant, start: date, days: int) -> dict:
    end = start + timedelta(days=days)
    plist = _protocol_rows(a)
    ms = [m for m in a.meetings if start <= m.start.date() < end]
    focus = [f for i in range(days) for f in _focus(a.meetings, start + timedelta(days=i))
             if (start + timedelta(days=i)).weekday() < 5]
    return {"start": start.isoformat(), "days": days, "meetings": [meeting_short(m, a, plist) for m in ms],
            "focus": focus}


def meeting(a: Assistant, uid: str) -> dict | None:
    m = next((x for x in a.meetings if x.uid == uid), None)
    if not m:
        return None
    plist = _protocol_rows(a)
    short = meeting_short(m, a, plist)
    names = set(m.attendees)
    approved = [p for p in plist if p["status"] == "утверждён" and p["meeting_end"] < m.start]
    same = [p for p in approved if p["title"] == m.title] or approved
    prev = questions_from_previous(a.protocols.protocol(same[-1]["id"], a.p.org)) if same else []
    _, hq = meeting_brief(a.j, None, a.today())
    hq = task_questions(a, names) + ([q for q in hq if q.split(" — ", 1)[0] in names] or hq)
    today_ = a.today()
    own = [task_dict(t, today_, src) for t, src in all_tasks(a) if t.owner in names and t.status not in DONE]
    return {"meeting": short, "previous_questions": prev, "history_questions": hq, "tasks": own,
            "protocol": protocol_dict(a, short["protocol_id"]) if short["protocol_id"] else None,
            "avatar_url": "/avatar?" + urlencode({"title": m.title, "uid": m.uid})}


# ---------------- память, правки, профиль, журнал ----------------
def memory(a: Assistant, q: str = "") -> dict:
    mem = a.memory
    if mem is None:
        return {"q": q, "facts": 0, "topics": []}
    a.sync_memory()                          # решения руководителя — сразу в памяти (TASK_17)
    groups = topics(mem)
    if q.strip():
        keys = {(f.topic, f.source, f.quote) for f in mem.related(q)}
        groups = [fs for fs in groups if any((f.topic, f.source, f.quote) in keys for f in fs)]
    return {"q": q, "facts": len(mem.all()), "topics": [
        {"topic": title_of(fs), "kind": fs[-1].kind, "last_at": fs[-1].when.isoformat(), "last_what": fs[-1].what,
         "history": [{"period": period(g), "who": g[0].who, "what": g[-1].what, "quote": g[0].quote,
                      "sources": [x.source for x in g]} for g in collapse(fs)]} for fs in groups]}


def edits(a: Assistant) -> dict:
    tz = a.clock.now().tzinfo
    pe =[{"at": from_db(r["at"], tz).isoformat(), "protocol_id": r["protocol_id"], "instruction": r["instruction"]}
          for r in a.protocols.conn.execute("SELECT protocol_id, at, instruction FROM protocol_edits ORDER BY id")]
    te = [{"at": x["at"].isoformat(), "key": x["payload"].get("key"), "title": x["payload"].get("title", ""),
           "summary": x["summary"]} for x in a.j.actions() if x["kind"] == "template_edit"]
    return {"protocol_edits": pe, "template_edits": te}


# ---------------- Профиль: карточка руководителя, штат (Пит 28.11) ----------------
CARD_FIELDS = ("name", "title", "about", "email", "phone", "address")


def card(a: Assistant) -> dict:
    """Кто руководитель и чем занимается. Правки руководителя — в журнале, последняя главнее файла профиля."""
    p, org = a.p, a.p.org
    leader = org.leader if org else None
    team = [x for x in org.people if x.role == "подчинённый"] if org else []
    boss = next((x for x in org.people if x.role == "руководитель руководителя"), None) if org else None
    about = " ".join(filter(None, [                 # без повтора должности и целей — они ниже отдельно
        f"В команде {len(team)} человек: " + ", ".join(x.position[:1].lower() + x.position[1:]
                                                                    for x in team) + "." if team else "",
        f"Подчиняется: {boss.name}, {boss.position[:1].lower() + boss.position[1:]}." if boss else ""]))
    base = {"name": p.name, "title": p.title, "about": about, "email": org.email_of(leader) if leader else "", "phone": "", "address": ""}
    saved = next((x["payload"] for x in reversed(a.j.actions()) if x["kind"] == "profile_card"), {})
    out = {k: saved.get(k, base[k]) for k in CARD_FIELDS}
    return out | {"goals": p.goals, "style": p.style, "plan_time": p.plan_time, "week_time": p.week_time}


def card_problems(d: dict) -> str:
    if not d.get("name", "").strip():
        return "напишите имя"
    if d.get("email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", d["email"].strip()):
        return "почта выглядит неверно"
    if d.get("phone") and not re.fullmatch(r"[+\d\s()\-]{5,25}", d["phone"].strip()):
        return "в телефоне только цифры, пробелы, скобки, + и -"
    if any(len(str(d.get(k, ""))) > (2000 if k == "about" else 200) for k in CARD_FIELDS):
        return "слишком длинный текст"
    return ""


def apply_card(a: Assistant) -> None:
    """Имя и должность из карточки — в профиль помощника: подпись в письмах, аватар, Telegram."""
    c = card(a)
    a.p.name, a.p.title = c["name"], c["title"]


def save_card(a: Assistant, d: dict) -> dict:
    clean = {k: str(d.get(k, "")).strip() for k in CARD_FIELDS}
    a._log("profile_card", actor="human", path="now", summary="Вы обновили карточку руководителя", payload=clean)
    apply_card(a)
    return card(a)


def staff(a: Assistant) -> dict:
    """Штат: руководство, моя команда, коллеги — с тем, что сейчас на каждом (задачи, письма)."""
    org = a.p.org
    if not org:
        return {"groups": [], "total": 0}
    today = a.today()
    tasks_by: dict[str, list] = {}
    for t, _ in all_tasks(a):
        tasks_by.setdefault(t.owner, []).append(t)
    letters: dict[str, list] = {}
    for x in a.j.actions():
        if x["kind"] in _ROUTES and x["event_id"]:
            ev = (a.j.event(x["event_id"]) or {}).get("payload") or {}
            letters.setdefault((ev.get("from") or "").lower(), []).append(ev.get("received") or "")
    def person(p):
        email = org.email_of(p)
        ts = [t for t in tasks_by.get(p.name, []) if t.status not in DONE]
        ls = sorted(set(letters.get(email.lower(), [])))
        return {"id": p.id, "name": p.name, "position": p.position, "email": email, "role": p.role,
                "tasks_open": len(ts), "tasks_late": sum(1 for t in ts if t.due < today or t.status == "просрочена"),
                "letters": len(ls), "last_letter": ls[-1][:10] if ls else None}
    groups = [("Руководство", "руководитель руководителя"), ("Моя команда", "подчинённый"), ("Коллеги", "коллега")]
    out = [{"title": title, "people": [person(p) for p in org.people if p.role == role]} for title, role in groups]
    return {"groups": out, "total": sum(len(g["people"]) for g in out)}


def chosen_avatar(journal, default: str) -> str:
    """Аватар, которого выбрал руководитель в «Мои аватары»; не выбирал — из настроек стенда."""
    return next((x["payload"]["avatar_id"] for x in reversed(journal.actions()) if x["kind"] == "avatar_choice"), default)


_AVATAR_STATUS = {"ACTIVE": "готов", "DEPLOYING": "обучается — появится, когда будет готов",
                  "PENDING": "обучается — появится, когда будет готов", "FAILED": "обучение не удалось"}


def avatars(la, current: str, presets: list[str]) -> dict:
    """Мои аватары: обученные на руководителе (их список растёт сам) и готовые аватары LiveAvatar."""
    rows, note = [], ""
    try:
        own = la.avatars() if la else []
        extra = []
        for aid in presets + ([current] if current else []):
            if aid and aid not in {x["id"] for x in own + extra}:
                try:
                    extra.append(la.avatar(aid))
                except Exception:
                    pass
        for x, source in [(x, "Обучен на вас") for x in own] + [(x, "Готовый аватар LiveAvatar") for x in extra]:
            st = x.get("status", "")
            rows.append({"id": x["id"], "name": x.get("name", "").replace(" (Friendly)", ""), "source": source,
                         "type": "по фото" if x.get("type") == "IMAGE" else "по видео",
                         "status": st, "status_text": _AVATAR_STATUS.get(st, st.lower()), "ready": st == "ACTIVE",
                         "preview": x.get("preview_url") or "", "current": x["id"] == current})
    except Exception as e:                      # нет связи с LiveAvatar — показываем, что знаем
        note = f"LiveAvatar сейчас недоступен ({type(e).__name__}) — список обновится позже."
        if current:
            rows.append({"id": current, "name": "Текущий аватар", "source": "", "type": "", "status": "",
                         "status_text": "нет связи", "ready": True, "preview": "", "current": True})
    return {"current": current, "avatars": rows, "note": note}


def profile(a: Assistant) -> dict:
    p = a.p
    return {"id": p.id, "name": p.name, "title": p.title, "goals": p.goals, "confirm": sorted(p.confirm),
            "rules": {k: {"action": r.action, "delegate_to": r.delegate_to, "source": r.source} for k, r in p.rules.items()},
            "plan_time": p.plan_time, "week_time": p.week_time, "style": p.style,
            "staff_total": len(p.org.people) - 1 if p.org else 0}


# ---------------- Профиль → «Правила» (Пит 28.11: «большая кнопка, проваливаемся и читаем внутри») ----------------
RULE_GROUPS = [("делегировать", "Передаю ответственному", "Письмо уходит сотруднику, вы видите это в сводке."),
               ("авто", "Отвечаю сам", "Короткий ответ от вашего имени, без вас."),
               ("в_сводку", "Читаю и кладу в сводку", "Не отвечаю, но то, что важно, будет в утренней сводке."),
               ("игнорировать", "Пропускаю", "Рассылки, спам и то, что не требует действий."),
               ("уточнить", "Уточняю у отправителя", "Непонятное письмо снаружи — помощник сам спрашивает, что нужно.")]
RULE_TITLES = {"доступы": "Доступы к системам", "календарь": "Перенос внутренних встреч", "клиенты": "Запросы клиентов",
               "отчёт": "Отчёты", "продажи": "Запросы по продажам", "рассылка": "Рассылки", "статус": "Статусы",
               "спам": "Спам", "приглашение": "Приглашения", "": "Без категории",
               "обязательства_клиентам": "Обязательства клиентам"}


def _confirm_lines(p) -> dict[str, tuple[str, str]]:
    """«Никогда без подтверждения» из профиля: ключ → (название, примеры в скобках)."""
    from .profile import _find, _items, _sections
    out = {}
    for line in _items(_find(_sections(p.text), "без подтверждения")):
        text = line.rstrip(";. ")
        title, _, rest = text.partition("(")
        for key in p.confirm:
            stems = [w[:6] for w in key.replace("_", " ").split()]
            if key not in out and all(s in title.lower() for s in stems):
                out[key] = (title.strip()[:1].upper() + title.strip()[1:], rest.rstrip(")").strip())
    return out


def rules(a: Assistant) -> dict:
    p, org = a.p, a.p.org
    last: dict = {}                     # как в «Метриках»: одно письмо — один раз, по последнему разбору
    for x in a.j.actions():
        if x["kind"] in _ROUTES:
            last[x["event_id"] if x["event_id"] is not None else ("a", x["id"])] = x
    counts: dict[tuple[str, str], int] = {}
    for x in last.values():
        k = (x["kind"][5:], (x["payload"] or {}).get("category") or "")
        counts[k] = counts.get(k, 0) + 1
    to_me = {c: n for (act, c), n in counts.items() if act == "спросить_руководителя" for _ in [0]}
    lines = _confirm_lines(p)
    only_me = [{"key": k, "title": lines.get(k, (RULE_TITLES.get(k, readable_key(k)), ""))[0],
                "examples": lines.get(k, ("", ""))[1], "letters": to_me.get(k, 0)} for k in sorted(p.confirm)]
    groups = []
    for action, title, what in RULE_GROUPS:
        rs = []
        for cat, r in sorted(p.rules.items()):
            if r.action != action:
                continue
            who = org.by_id.get(r.delegate_to) if org and r.delegate_to else None
            rs.append({"category": cat, "title": RULE_TITLES.get(cat, readable_key(cat)),
                       "to": {"name": who.name, "position": who.position} if who else None,
                       "profile_line": r.source.rstrip(";. "), "letters": counts.get((action, cat), 0), "own": False})
        # письма, ушедшие так по правилу самого помощника (спам, служебные, «уточнить») — тоже видны, с пометкой
        for (act, cat), n in sorted(counts.items()):
            if act == action and cat not in {x["category"] for x in rs}:
                rs.append({"category": cat, "title": RULE_TITLES.get(cat, readable_key(cat)), "to": None,
                           "profile_line": "", "letters": n, "own": True})
        if rs:
            groups.append({"action": action, "title": title, "what": what, "rules": rs})
    total = sum(counts.values())
    asked = sum(n for (act, _), n in counts.items() if act == "спросить_руководителя")
    unruled = sum(n for c, n in to_me.items() if c not in p.confirm)
    return {"only_me": only_me, "groups": groups,
            "fallback": {"text": "Для чего правила нет — спрашиваю вас и запоминаю, как вы решили.", "letters": unruled},
            "summary": {"only_me": len(only_me), "auto": sum(len(g["rules"]) for g in groups), "letters": total,
                        "groups_letters": sum(x["letters"] for g in groups for x in g["rules"]),
                        "letters_to_you": asked, "letters_without_you": total - asked},
            "file": f"profiles/{p.id}/profile.md"}


def readable_key(key: str) -> str:
    t = key.replace("_", " ")
    return t[:1].upper() + t[1:]


# ---------------- Профиль → «Журнал действий» (Пит 28.11) ----------------
JOURNAL_GROUPS = ["Почта и решения", "Встречи и аватар", "Протоколы и задачи", "Память и обучение", "Telegram", "Прочее"]
_VERDICT = {"yes": "согласовали", "no": "отказали по", "later": "отложили"}
_HIDDEN = {"inbox_done", "decision_prepared", "robot_live", "tg_sent", "mail_read"}   # «прочитано» — состояние почты
from .metrics import ROUTES as _ROUTES                                               # записи разбора письма   # tg_sent — текст сообщений, виден в подробностях      # служебное: результат виден в других записях


def _jgroup(kind: str) -> str:
    if kind.startswith(("mail_", "letter_", "decision_", "delegation_", "queue_", "reply_")):
        return "Почта и решения"
    if kind.startswith(("meeting", "avatar_", "robot_", "person_")):
        return "Встречи и аватар"
    if kind.startswith(("protocol", "task_")) or kind == "report_review":
        return "Протоколы и задачи"
    if kind.startswith(("memory_", "style_", "template_")):
        return "Память и обучение"
    if kind.startswith(("tg_", "morning", "voice_")):
        return "Telegram"
    return "Прочее"                     # новый вид записи не выдаём за сообщение в Telegram (Пит 28.11)


def _subject(summary: str) -> str:
    return summary.split(" — ", 1)[0].strip()


def _jtext(x: dict) -> str:
    """Запись журнала — фраза «кто что сделал»."""
    k, s, pl = x["kind"], x["summary"] or "", x["payload"] or {}
    if k == "letter_decided":
        verdict, _, subj = s.partition(": ")
        return f"Вы {_VERDICT.get(verdict, verdict)} «{subj}»"
    if k == "mail_спросить_руководителя":
        subj, _, why = s.partition(" — ")
        return f"Письмо «{subj}» — к вам на решение" + (f": {why}" if why else "")
    if k == "mail_делегировать":
        return f"Передал ответственному «{_subject(s)}»"
    if k == "mail_авто":
        return f"Ответил сам на «{_subject(s)}»"
    if k == "mail_в_сводку":
        return f"Прочитал «{_subject(s)}», положил в сводку"
    if k == "mail_игнорировать":
        return f"Пропустил «{_subject(s)}»"
    if k == "mail_уточнить":
        return f"Уточнил у отправителя, что нужно по «{_subject(s)}» — вас не звал"
    if k == "mail_sent":
        to, _, subj = s.partition(": ")
        return f"Отправил письмо «{subj}» → {to}"
    if k == "memory_saved":
        return f"Запомнил из «{s.rsplit(': фактов', 1)[0]}»"
    if k == "memory_decision":
        return f"Записал в память ваше решение: {s}"
    if k == "morning_plan":
        return "Прислал утренний план в Telegram"
    if k == "tg_block":
        return f"Telegram: {s}"
    if k == "protocol_draft":
        return f"Собрал черновик протокола «{s.split(':', 1)[0]}»"
    if k == "protocol_sent":
        return f"Вы утвердили протокол «{s.split(':', 1)[0]}», помощник разослал его"
    if k == "reply_draft":
        return f"Подготовил черновик ответа на «{s.split(': ', 1)[-1]}» — ждёт вас в почте"
    if k in ("protocol_edit", "decision_draft_edit", "reply_draft_edit"):
        return f"Вы поправили: {s}"
    if k == "protocol_rejected":
        return "Вы отклонили протокол"
    if k == "avatar_auto_done":
        title, _, res = s.partition(": ")
        return {"protocol": f"Встреча «{title}» с аватаром прошла, протокол готов",
                "no_session": f"«{title}» прошла без аватара", "empty": f"«{title}»: разговора не было",
                "no_link": f"«{title}»: нет ссылки на Телемост, аватар не ходил"}.get(res, s)
    if k == "avatar_auto_error":
        return f"Аватар не смог подключиться: {s}"
    if k == "robot_started":
        return f"Аватар зашёл в Телемост: {s.split(':', 1)[0]}"
    if k == "robot_done":
        return f"Аватар вышел со встречи: {s}"
    if k == "meeting_lead":
        return "Вы выбрали, кто ведёт встречу: " + s.split(": ", 1)[-1]
    if k == "meeting_created":
        return f"Вы назначили встречу «{s}»"
    if k == "delegation_released":
        verdict, _, subj = s.partition(": ")
        return f"После вашего решения {'передал' if verdict == 'yes' else 'не стал передавать'} «{subj}»"
    if k == "queue_rechecked":
        return f"Пересмотрел очередь по новым правилам: {s}"
    if k == "person_alias":
        return f"Запомнил, кто есть кто: {s}"
    if k == "task_action" and pl.get("action") == "ask":
        m = re.match(r"Спросил (.+?), что с «(.+)»$", s)
        if m:
            return f"Спросил исполнителя ({m.group(1)}), что с задачей «{m.group(2)}»"
    return s or k.replace("_", " ")


def journal_feed(a: Assistant, *, who: str = "", group: str = "", limit: int = 300) -> dict:
    """Журнал для руководителя: по дням, новые сверху, копии одного действия подряд — одна строка с «×N»."""
    acts = [x for x in a.j.actions() if x["kind"] not in _HIDDEN]
    counts = {"Помощник": 0, "Вы": 0}         # считаем строки, которые видит руководитель, а не копии писем
    rows: list[dict] = []
    last_seen: tuple | None = None
    for x in acts:
        item = {"id": x["id"], "at": x["at"].isoformat(), "who": "Вы" if x["actor"] == "human" else "Помощник",
                "group": _jgroup(x["kind"]), "text": _jtext(x), "times": 1}
        key = (item["who"], item["text"], item["at"][:10])
        if key != last_seen:
            counts[item["who"]] += 1
            last_seen = key
        if who == "human" and item["who"] != "Вы" or who == "assistant" and item["who"] != "Помощник":
            continue
        if group and item["group"] != group:
            continue
        prev = rows[-1] if rows else None
        if prev and (prev["who"], prev["text"]) == (item["who"], item["text"]) and prev["at"][:10] == item["at"][:10]:
            prev["times"] += 1
            prev["at"], prev["id"] = item["at"], item["id"]
            continue
        rows.append(item)
    rows = rows[-limit:][::-1]
    days: dict[str, list] = {}
    for r in rows:
        days.setdefault(r["at"][:10], []).append(r)
    return {"groups": JOURNAL_GROUPS, "counts": counts,
            "days": [{"day": d, "label": _day_label(d, a), "items": items} for d, items in days.items()]}


def _strip_html(text: str) -> str:
    import html as _h
    return _h.unescape(re.sub(r"<[^>]+>", "", text or ""))


def journal_detail(a: Assistant, action_id: int) -> dict | None:
    """Подробности записи журнала: письмо, что ушло, кому и почему, протокол, встреча, текст в Telegram."""
    from datetime import timedelta as _td
    acts = a.j.actions()
    x = next((y for y in acts if y["id"] == action_id), None)
    if x is None:
        return None
    k, pl = x["kind"], x["payload"] or {}
    fields: list[dict] = []
    blocks: list[dict] = []
    links: list[dict] = []
    note = ""

    def f(label, value):
        if value not in (None, "", []):
            fields.append({"label": label, "value": str(value)})

    def letter(eid):
        ev = a.j.event(eid) if eid else None
        if not ev:
            return None
        p = ev["payload"] or {}
        f("От", (p.get("from_name") or "") + (f" <{p['from']}>" if p.get("from") else ""))
        f("Тема", p.get("subject"))
        f("Получено", (p.get("received") or "")[:16].replace("T", " "))
        if p.get("body"):
            blocks.append({"label": "Письмо", "text": p["body"][:3000]})
        links.append({"label": "Открыть в переписке", "href": "#mail"})
        return p

    def sent_for(eid):
        key = pl.get("key")                     # копии одного вопроса: ответ записан к первой
        eids = {y["event_id"] for y in acts if key and y["kind"] == "letter_decided"
                and (y["payload"] or {}).get("key") == key} | {eid}
        for y in acts:
            if y["kind"] == "mail_sent" and y["event_id"] in eids and y["event_id"]:
                sp = y["payload"]
                blocks.append({"label": f"Ушло → {sp.get('to', '')}", "text": f"{sp.get('subject', '')}\n\n{sp.get('body', '')}"[:3000]})

    if k.startswith("mail_") and k != "mail_sent":
        p = letter(x["event_id"]) or {}
        f("Решение", _jtext(x).split(" «", 1)[0])
        who = a.p.org.by_id.get(pl.get("delegate_to")) if a.p.org and pl.get("delegate_to") else None
        if who:
            f("Кому", f"{who.name}, {who.position}")
        f("Почему", pl.get("reason") or (x["summary"].split(" — ", 1)[1] if " — " in x["summary"] else ""))
        f("Категория", (pl.get("category") or "").replace("_", " "))
        sent_for(x["event_id"])
        title = p.get("subject") or _jtext(x)
    elif k == "letter_decided":
        letter(x["event_id"])
        f("Ваше решение", {"yes": "согласовать", "no": "отказать", "later": "отложить"}.get(pl.get("verdict"), pl.get("verdict")))
        sent_for(x["event_id"])
        title = x["summary"].split(": ", 1)[-1]
    elif k == "mail_sent":
        f("Кому", pl.get("to"))
        f("Вид письма", pl.get("kind"))
        blocks.append({"label": "Письмо", "text": f"{pl.get('subject', '')}\n\n{pl.get('body', '')}"[:3000]})
        title = pl.get("subject") or "Письмо"
    elif k == "reply_draft":
        ev = next((y for y in acts if y["kind"].startswith("mail_") and y["event_id"]
                   and (a.j.event(y["event_id"]) or {}).get("external_id") == pl.get("letter_id")), None)
        if ev:
            letter(ev["event_id"])
        blocks.append({"label": "Черновик ответа (в почте, на кнопке «Ответить»)", "text": pl.get("text", "")})
        note = "Черновик никуда не отправлен: он ждёт вас в почте. В Telegram его не было."
        title = x["summary"].split(": ", 1)[-1]
    elif k in ("decision_draft_edit", "reply_draft_edit"):
        blocks += [{"label": "Черновик помощника", "text": pl.get("draft", "")},
                   {"label": "Вы отправили", "text": pl.get("text", "")}]
        title = x["summary"]
    elif k.startswith("protocol") and pl.get("protocol_id"):
        rec = a.protocols.get(pl["protocol_id"])
        if rec:
            f("Протокол", f"№{rec['id']} · {rec['title']}")
            f("Статус", rec["status"])
            tasks = (rec["data"] or {}).get("tasks") or []
            if tasks:
                blocks.append({"label": "Задачи", "text": "\n".join(f"• {t.get('owner', '')}: {t.get('what', '')}"
                                                                   for t in tasks)})
        if pl.get("minutes_to_draft") is not None:
            f("Черновик через", f"{pl['minutes_to_draft']:.1f} мин после встречи")
        if pl.get("minutes_to_approved") is not None:
            f("Утверждён через", f"{pl['minutes_to_approved']:.0f} мин после встречи")
        links.append({"label": "Все протоколы", "href": "#meetings/protocols"})
        title = rec["title"] if rec else _jtext(x)
    elif k.startswith(("robot_", "avatar_", "meeting")):
        f("Встреча", pl.get("title"))
        f("Начало", (pl.get("start") or "")[11:16])
        f("Ссылка", pl.get("link") or pl.get("url"))
        f("Причина", pl.get("reason") or pl.get("error"))
        f("Участники", ", ".join(pl.get("participants") or []))
        f("Протокол", f"№{pl['protocol_id']}" if pl.get("protocol_id") else "")
        if pl.get("uid") and any(m.uid == pl["uid"] for m in a.meetings):   # встречи робота без календаря — без ссылки
            links.append({"label": "Открыть встречу", "href": "#meetings/m/" + pl["uid"]})
        title = pl.get("title") or _jtext(x)
    elif k == "task_action":
        tid = pl.get("task_id")
        found = next(((t, src) for t, src in all_tasks(a) if t.id == tid), None)
        what = {"ask": "Задача просрочена — помощник сам написал исполнителю и спросил, что с ней. Вас не звал.",
                "remind": "Напомнил исполнителю по вашей просьбе.",
                "move": f"Перенёс срок на {(pl.get('due') or '')[8:10]}.{(pl.get('due') or '')[5:7]}"
                        + (": так ответил исполнитель, с планами не расходится." if pl.get("by") == "answer" else "."),
                "done": "Задача отмечена выполненной.",
                "proposal": "Исполнитель назвал новый срок, но он расходится с планами — решаете вы."}.get(pl.get("action"), "")
        f("Что сделано", what)
        if found:
            t, _ = found
            f("Задача", t.task)
            f("Исполнитель", t.owner)
            f("Срок", f"{t.due:%d.%m.%Y}")
            f("Статус", t.status)
            mail = [y for y in acts if y["kind"] == "mail_sent"
                    and (y["payload"].get("subject") or "").startswith(f"Задача «{t.task}»")]
            if mail:
                y = min(mail, key=lambda y: abs((y["at"] - x["at"]).total_seconds()))
                blocks.append({"label": f"Письмо исполнителю → {y['payload'].get('to', '')}",
                               "text": f"{y['payload'].get('subject', '')}\n\n{y['payload'].get('body', '')}"[:3000]})
            page = task_page(a, tid) or {}
            ans = page.get("answer")
            if ans:
                text = ans if isinstance(ans, str) else (ans.get("text") or ans.get("body") or ans.get("quote") or "")
                blocks.append({"label": "Ответ исполнителя", "text": str(text)[:3000]})
            elif pl.get("action") == "ask":
                note = "Ответа пока нет. Если его не будет 2 дня, помощник спросит ещё раз."
            if pl.get("conflicts"):
                f("С чем расходится", "; ".join(pl["conflicts"]))
            links.append({"label": "Открыть задачу", "href": "#meetings/t/" + t.id})
        title = found[0].task if found else _jtext(x)
    elif k == "report_review":
        f("Что заметил", x["summary"])
        f("Кому написал", pl.get("to_name") or pl.get("to"))
        if pl.get("question"):
            blocks.append({"label": "Вопрос", "text": pl["question"]})
        f("Источник", ", ".join(pl.get("sources") or []))
        title = pl.get("topic") or _jtext(x)
    elif k == "memory_saved":
        facts = [m for m in (a.memory.all() if a.memory else []) if m.source == pl.get("source")]
        blocks.append({"label": "Что запомнил", "text": "\n".join(f"• {m.topic}: {m.what}" for m in facts) or "—"})
        f("Источник", pl.get("source"))
        links.append({"label": "Память", "href": "#memory"})
        title = _jtext(x)
    elif k == "memory_decision":
        f("Темы памяти", ", ".join(pl.get("topics") or []))
        links.append({"label": "Память", "href": "#memory"})
        title = x["summary"]
    elif k in ("morning_plan", "tg_block", "tg_remind"):
        sent = [y for y in acts if y["kind"] == "tg_sent" and x["at"] - _td(seconds=5) <= y["at"] <= x["at"] + _td(minutes=2)]
        for y in sent:
            blocks.append({"label": "Сообщение в Telegram", "text": y["payload"].get("text", "")})
        if not sent:
            note = "Текст сообщения тогда не сохранялся — помощник сохраняет его с 28.11."
        title = _jtext(x)
    else:
        # прочее: только понятные поля, служебные (id, ключи, флаги) не показываем
        names = {"to": "Кому", "subject": "Тема", "title": "Встреча", "topic": "Тема", "reason": "Причина",
                 "person": "Человек", "display": "Подпись в Телемосте", "moved": "Сколько писем", "summary": "Суть",
                 "question": "Вопрос", "error": "Ошибка", "facts": "Фактов", "text": "Текст"}
        for key, v in pl.items():
            if key in names and isinstance(v, (str, int, float)) and v != "":
                f(names[key], v)
        title = _jtext(x)
    if not (fields or blocks or note):
        note = "Подробностей у этой записи нет."
    return {"id": x["id"], "at": x["at"].isoformat(), "who": "Вы" if x["actor"] == "human" else "Помощник",
            "title": title, "text": _jtext(x), "fields": fields, "blocks": blocks, "links": links, "note": note}


def _day_label(d: str, a: Assistant) -> str:
    from datetime import date as _date
    day, today = _date.fromisoformat(d), a.clock.now().date()
    if day == today:
        return "Сегодня"
    if (today - day).days == 1:
        return "Вчера"
    return f"{day:%d.%m}, " + ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][day.weekday()]


def journal(a: Assistant, limit: int = 50) -> dict:
    acts = a.j.actions()[-max(1, min(limit, 500)):][::-1]
    return {"actions": [{"at": x["at"].isoformat(), "actor": x["actor"], "kind": x["kind"], "summary": x["summary"]}
                        for x in acts]}
