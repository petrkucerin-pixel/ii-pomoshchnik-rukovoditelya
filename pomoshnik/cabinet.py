"""Данные кабинета руководителя: разделы по стр. 3 ТЗ. Только чтение баз и сборка JSON, решения — в помощнике.

Контракт ответов — TASK_08_КАБИНЕТ_GPT.md и tests/fixtures/cabinet/.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from yadro.journal import from_db

from .assistant import Assistant, digest_from_journal, meeting_brief, protocol_tasks
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
    if kind == "игнорировать":
        return {"kind": "info", "label": "Пропущено: спам" if ap.get("category") == "спам" else "Пропущено: рассылка"}
    return {"kind": "info", "label": "Разобрано"}


def _incoming(a: Assistant) -> list[dict]:
    rows = a.j.conn.execute("SELECT id, external_id, payload FROM events WHERE channel = 'mail' ORDER BY id").fetchall()
    acts: dict[int, dict] = {}
    verdicts: dict[int, str] = {}
    held: set[int] = set()
    released: dict[int, str] = {}
    for x in a.j.actions():
        pl = x["payload"] or {}
        if x["kind"].startswith("mail_") and x["kind"] != "mail_sent" and x["event_id"]:
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
                    "status": _letter_status(a, act, ap, verdicts, held, released, deferred)})
    return out


# Кто инициатор исходящего письма — метка в «Отправленных», как категория в Outlook.
SENT_LABELS = {"передача": ("assistant", "Помощник передал исполнителю"), "автоответ": ("assistant", "Помощник ответил сам"),
               "вопрос_ревизора": ("assistant", "Помощник: вопрос по отчёту"),
               "выяснение": ("assistant", "Помощник: что с задачей"), "можно_начинать": ("assistant", "Помощник: можно начинать"),
               "решение": ("you", "По вашему решению"), "протокол": ("you", "Протокол после вашего утверждения"),
               "приглашение": ("you", "Ваше приглашение на встречу"), "напоминание": ("you", "Напоминание по вашей кнопке"),
               "поручение": ("you", "Ваше поручение")}


def _outgoing(a: Assistant) -> list[dict]:
    out = []
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
        out.append({"id": f"out-{x['id']}", "direction": "out", "at": _iso(x["at"]),
                    "from_name": a.signature.removeprefix("— "), "from_email": a.p.org.email_of(a.p.org.leader)
                    if a.p.org else "", "to_name": "", "to_email": pl.get("to", ""),
                    "subject": pl.get("subject", ""), "body": pl.get("body", ""), "category": pl.get("kind", ""),
                    "action": "отправлено", "reason": f"шаблон «{t.title}»" if t else "",
                    "reply_to": ext.get(eid) if eid else None, "status": {"kind": by, "label": label}})
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
        x["to_name"] = p.name if p else g.party_of_external(x["to_email"])
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
        if not (x["kind"].startswith("mail_") and x["kind"] != "mail_sent" and x["event_id"]):
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
            if not (x["kind"].startswith("mail_") and x["kind"] != "mail_sent" and x["event_id"]) or x["event_id"] in seen:
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
            "overdue": [task_dict(t, day, asked=asked_tasks(a)) for t in plan.overdue],
            "due_soon": [task_dict(t, day) for t in plan.due_soon],
            "focus": _focus(a.meetings, day),
            "digest": digest_from_journal(a.j, a.p.org)[-10:]}


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


def profile(a: Assistant) -> dict:
    p = a.p
    return {"id": p.id, "name": p.name, "title": p.title, "goals": p.goals, "confirm": sorted(p.confirm),
            "rules": {k: {"action": r.action, "delegate_to": r.delegate_to, "source": r.source} for k, r in p.rules.items()},
            "plan_time": p.plan_time, "week_time": p.week_time, "style": p.style}


def journal(a: Assistant, limit: int = 50) -> dict:
    acts = a.j.actions()[-max(1, min(limit, 500)):][::-1]
    return {"actions": [{"at": x["at"].isoformat(), "actor": x["actor"], "kind": x["kind"], "summary": x["summary"]}
                        for x in acts]}
