"""Помощник одного руководителя: связывает профиль, почту, Telegram, протоколы и журнал.

Все действия пишутся в журнал. Решения из списка «никогда без подтверждения» уходят руководителю в Telegram.
"""
from __future__ import annotations

import html
import os
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from yadro.journal import Journal

from . import decisions as dq
from .channels.telegram import Button, Update
from .memory import brief, collapse, extract_facts, period
from .plan import Meeting, build_day_plan, load_calendar, load_tasks, render_plan
from .profile import Profile
from .reports import Report, review
from .protocol import ProtocolStore, build_protocol, questions_from_previous, render_markdown
from .templates import Templates
from .triage import Decision, classify, decide

# Сколько минут руководителя экономит одно действие помощника. Допущения для метрики «освобождённые часы».
MINUTES_SAVED = {"авто": 3, "делегировать": 5, "в_сводку": 2, "игнорировать": 1, "спросить_руководителя": 2}
PROTOCOL_MINUTES_SAVED = 30
# Письма без фактов для памяти: на них не тратим вызов модели.
MEMORY_SKIP = {"", "рассылка", "приглашение", "доступы", "календарь"}


@dataclass
class Pending:
    kind: str
    ref: int


def md_to_html(md: str) -> str:
    out = []
    for line in md.splitlines():
        e = html.escape(line)
        if line.startswith("### "):
            out.append(f"<b>{html.escape(line[4:])}</b>")
        elif line.startswith("## "):
            out.append(f"\n<b>{html.escape(line[3:]).upper()}</b>")
        elif line.startswith("> "):
            out.append(f"<i>{html.escape(line[2:])}</i>")
        elif line.startswith("- [ ] "):
            out.append("☐ " + html.escape(line[6:]))
        elif line.startswith("- "):
            out.append("• " + html.escape(line[2:]))
        else:
            out.append(e)
    out[0] = f"<b>{out[0]}</b>" if out else ""
    return "\n".join(out)


REPORT_CATEGORIES = ("отчёт", "статус")


def work_day(clock) -> date:
    """Рабочий день помощника. DEMO_DATE в .env сдвигает его на даты учебного календаря заказчика."""
    demo = os.environ.get("DEMO_DATE")
    return date.fromisoformat(demo) if demo else clock.now().date()


def reports_from_journal(journal: Journal) -> list[Report]:
    out = []
    for a in journal.actions():
        r = (a["payload"] or {}).get("report") if a["kind"].startswith("mail_") else None
        if r:
            out.append(Report(r["id"], r["from_name"], r["from_email"], datetime.fromisoformat(r["received"]),
                              r["subject"], r["body"]))
    return out


def meeting_brief(journal: Journal, memory, today: date, days: int = 7) -> tuple[str, list[str]]:
    """Для аватара: вся история из памяти и открытые вопросы ревизора за последние `days` дней.

    Берётся из баз, а не из памяти процесса, поэтому переживает перезапуск стенда."""
    history = brief(memory) if memory is not None else ""
    latest: dict[tuple, str] = {}
    for a in journal.actions():
        pl = a["payload"] or {}
        if a["kind"] != "report_review" or not pl.get("to") or not pl.get("question"):
            continue
        age = (today - date.fromisoformat(pl["day"])).days
        if 0 <= age <= days:
            latest[(pl.get("kind"), pl.get("topic"))] = f"{pl.get('to_name')} — {pl.get('topic')}: {pl['question']}"
    return history, list(latest.values())


def letter_payload(e) -> dict:
    """Письмо целиком в журнале: кабинет показывает переписку и после перезапуска."""
    rec = getattr(e, "received", None)
    return {"from": e.from_email, "from_name": e.from_name, "to": getattr(e, "to", "") or "",
            "subject": e.subject, "body": e.body, "received": rec.isoformat() if rec else None}


def is_mail_route(kind: str) -> bool:
    """Действие разбора входящего письма (не исходящее письмо)."""
    return kind.startswith("mail_") and kind != "mail_sent"


def decisions_from_journal(journal: Journal) -> list[dict]:
    """Письма «ждут только вас», по которым руководитель ещё не решил. «Позже» — остаётся в очереди.

    Решает последний разбор письма: после пересмотра по новым правилам письмо может уйти из очереди."""
    last, decided, order = {}, set(), []
    for a in journal.actions():
        if is_mail_route(a["kind"]):
            if a["event_id"] not in last:
                order.append(a["event_id"])
            last[a["event_id"]] = a
        elif a["kind"] == "letter_decided":
            verdict = (a["payload"] or {}).get("verdict") or a["summary"].split(":", 1)[0]
            if verdict in ("yes", "no"):
                decided.add(a["event_id"])
    out = []
    for eid in order:
        a = last[eid]
        if a["kind"] != "mail_спросить_руководителя" or eid in decided:
            continue
        ev = journal.event(a["event_id"]) or {}
        pl, ap = ev.get("payload") or {}, a["payload"] or {}
        subject = pl.get("subject", "")
        out.append({"event_id": a["event_id"], "at": pl.get("received") or a["at"].isoformat(),
                    "from": pl.get("from_name") or pl.get("from", ""), "email": pl.get("from", ""),
                    "subject": subject, "summary": ap.get("summary") or subject,
                    "reason": ap.get("reason") or a["summary"].removeprefix(subject + " — "),
                    "category": ap.get("category", ""), "id": ev.get("external_id", "")})
    return out


def held_from_journal(journal: Journal) -> list[dict]:
    """Передачи, которые ждут решения руководителя по тому же человеку (доступ ждёт оффер)."""
    held, released = {}, set()
    for a in journal.actions():
        pl = a["payload"] or {}
        if is_mail_route(a["kind"]):
            held.pop(a["event_id"], None)
            if a["kind"] == "mail_делегировать" and pl.get("after"):
                held[a["event_id"]] = {"event_id": a["event_id"], "after": pl["after"], "delegate_to": pl["delegate_to"]}
        elif a["kind"] == "delegation_released":
            released.add(a["event_id"])
    return [h for eid, h in held.items() if eid not in released]


TASK_DONE = ("выполнена",)
_UPDATE_STATUS = {"выполнена": "выполнена", "в работе": "в работе", "перенесена": "перенесена", "изменилась": "изменена"}


def protocol_tasks(journal: Journal) -> list[dict]:
    """Задачи из утверждённых протоколов с учётом сверки на следующих встречах и порядка выполнения.

    Статус: «новая», «в работе», «выполнена», «перенесена», «изменена» или «ждёт задачи N», пока предшественник
    не выполнен."""
    tasks: dict[tuple, dict] = {}
    per_protocol: dict = {}
    for a in journal.actions():
        pl = a["payload"] or {}
        if a["kind"] == "task_assigned":
            pid = pl.get("protocol_id")
            per_protocol[pid] = per_protocol.get(pid, 0) + 1
            n = pl.get("n") or per_protocol[pid]
            owner, what = (a["summary"].split(": ", 1) + [""])[:2]
            tasks[(pid, n)] = {"protocol_id": pid, "n": n, "owner": pl.get("owner") or owner,
                               "what": pl.get("what") or what.rsplit(" до ", 1)[0], "owner_id": pl.get("owner_id"),
                               "due": datetime.fromisoformat(pl["due"]).date(), "after": pl.get("after") or [],
                               "status": "новая", "updated": False, "action_id": a["id"]}
        elif a["kind"] == "task_update" and (pl.get("protocol_id"), pl.get("n")) in tasks:
            t = tasks[(pl["protocol_id"], pl["n"])]
            t["status"], t["updated"] = _UPDATE_STATUS.get(pl.get("status"), t["status"]), True
            if pl.get("what"):
                t["what"] = pl["what"]
            if pl.get("due"):
                t["due"] = datetime.fromisoformat(pl["due"]).date()
    for t in tasks.values():
        waiting = [n for n in t["after"] if tasks.get((t["protocol_id"], n), {}).get("status") not in TASK_DONE]
        if waiting and not t["updated"]:
            t["status"] = "ждёт задачи " + ", ".join(map(str, waiting))
    return list(tasks.values())


def prepared_from_journal(journal: Journal) -> dict[str, dict]:
    return {a["payload"]["key"]: a["payload"] for a in journal.actions()
            if a["kind"] == "decision_prepared" and (a["payload"].get("v") or 1) >= dq.PREP_VERSION}


def letter_ns(journal: Journal, eid: int) -> SimpleNamespace:
    """Письмо из журнала в том же виде, что приходит из почты."""
    ev = journal.event(eid) or {}
    pl = ev.get("payload") or {}
    rec = datetime.fromisoformat(pl["received"]) if pl.get("received") else None
    return SimpleNamespace(id=ev.get("external_id", ""), from_email=pl.get("from", ""), from_name=pl.get("from_name", ""),
                           to=pl.get("to", ""), subject=pl.get("subject", ""), body=pl.get("body", ""), received=rec)


def meetings_from_journal(journal: Journal) -> list[Meeting]:
    """Встречи, которые руководитель назначил в кабинете (TASK_10). Календарь заказчика не меняется."""
    out = []
    for a in journal.actions():
        if a["kind"] == "meeting_created":
            pl = a["payload"]
            out.append(Meeting(pl["uid"], datetime.fromisoformat(pl["start"]), datetime.fromisoformat(pl["end"]),
                               pl["title"], pl["category"], tuple(pl["attendees"]), bool(pl.get("avatar"))))
    return out


class Assistant:
    def __init__(self, profile: Profile, llm, pult, mailbox, journal: Journal, protocols: ProtocolStore, *,
                 calendar_path: str | Path, tasks_path: str | Path, outbox_mailbox=None, memory=None):
        self.p, self.llm, self.pult, self.mail = profile, llm, pult, mailbox
        self.memory = memory
        self.send_mail = outbox_mailbox or mailbox
        self.j, self.protocols = journal, protocols
        self.meetings = sorted(load_calendar(calendar_path) + meetings_from_journal(journal), key=lambda m: m.start)
        self.tasks = load_tasks(tasks_path)
        self.pending: Pending | None = None
        self.decisions: list[dict] = decisions_from_journal(journal)   # письма, которые ждут руководителя
        self.held: list[dict] = held_from_journal(journal)              # передачи, ждущие решения руководителя
        self.prepared: dict[str, dict] = prepared_from_journal(journal)  # готовые решения по вопросам очереди
        self.templates = Templates(profile.dir)
        self.digest: list[str] = []
        self.risks: list[str] = []           # риски для целей руководителя от ревизора
        self.reports: list[Report] = reports_from_journal(journal)   # отчёты и статусы команды для ревизора

    @property
    def clock(self):
        return self.j.clock

    def create_meeting(self, *, title: str, start: datetime, minutes: int, category: str, attendees: list[str],
                       emails: list[str], link: str = "", avatar: bool = False) -> Meeting:
        """Новая встреча из кабинета: в календарь, приглашения участникам, ссылка Телемоста, аватар по желанию."""
        import uuid
        m = Meeting(f"new-{uuid.uuid4().hex[:10]}@pomoshnik", start, start + timedelta(minutes=minutes), title,
                    category, tuple([*attendees, *emails]), avatar)
        self._log("meeting_created", actor="human", path="now", summary=f"{title} {start:%d.%m %H:%M}",
                  payload={"uid": m.uid, "title": title, "start": start.isoformat(), "end": m.end.isoformat(),
                           "category": category, "attendees": list(m.attendees), "avatar": avatar})
        if link:
            from .robot.manager import set_link
            set_link(self.j, m.uid, link)
        self.meetings = sorted([*self.meetings, m], key=lambda x: x.start)
        org = self.p.org
        to = [org.email_of(p) for n in attendees for p in org.people if p.name == n] if org else []
        for addr in [*to, *emails]:
            self.send(addr, "invite", "приглашение", встреча=title, когда=f"{start:%d.%m %H:%M}",
                      длительность=minutes, ссылка=link or "пришлю отдельно")
        return m

    def today(self) -> date:
        return work_day(self.clock)

    def _log(self, kind: str, *, actor="assistant", path=None, summary="", payload=None, event_id=None):
        self.j.log_action(kind, actor=actor, path=path, summary=summary, payload=payload or {}, event_id=event_id)

    @property
    def signature(self) -> str:
        return f"— ИИ-помощник {os.environ.get('LEADER_NAME_GENITIVE') or self.p.name}"

    def send(self, to: str, template: str, kind: str, *, in_reply_to=None, event_id=None, **values) -> str:
        """Письмо по шаблону руководителя. Каждое исходящее — в журнал, кабинет показывает его в переписке."""
        values.setdefault("подпись", self.signature)
        values.setdefault("руководитель", self.p.name)
        subject, body = self.templates.render(template, **values)
        mid = self.send_mail.send(to, subject, body, in_reply_to=in_reply_to)
        self._log("mail_sent", path="now", event_id=event_id, summary=f"{to}: {subject}",
                  payload={"to": to, "subject": subject, "body": body, "in_reply_to": in_reply_to,
                           "kind": kind, "template": template, "message_id": mid})
        return mid

    # ---------------- почта ----------------
    def process_inbox(self) -> dict:
        letters = self.mail.fetch_new()
        counts: dict[str, int] = {}
        for e in letters:
            eid, new = self.j.record_event("mail", e.id, letter_payload(e))
            if not new:
                continue
            cat, summ, conf = classify(self.llm, e.subject, e.body, e.from_name)
            d = decide(self.p, cat, e.from_email, summary=summ or e.subject, confidence=conf)
            rec = getattr(e, "received", None) or self.clock.now()
            report = Report(e.id, e.from_name, e.from_email, rec, e.subject, e.body) \
                if d.category in REPORT_CATEGORIES else None
            self._act(e, d, eid, report)
            counts[d.action] = counts.get(d.action, 0) + 1
            if d.category not in MEMORY_SKIP:
                self.remember(e.body, source=e.id, who=e.from_name, when=rec, title=e.subject, event_id=eid)
            if report:
                self.reports.append(report)
        if letters:
            self._log("inbox_done", summary=json.dumps(counts, ensure_ascii=False), payload=counts)
            counts["ревизор"] = self.review_reports()
        return counts

    # ---------------- память ----------------
    def remember(self, text: str, *, source: str, who: str, when: datetime, title: str = "", event_id=None) -> int:
        if self.memory is None:
            return 0
        n = self.memory.add(extract_facts(self.llm, text, source=source, who=who, when=when, title=title))
        if n:
            self._log("memory_saved", path="now", event_id=event_id, summary=f"{title or source}: фактов {n}",
                      payload={"source": source, "facts": n})
        return n

    def recall(self, query: str) -> str:
        query = query.strip().strip("?").strip()
        if not query:
            return "Напишите тему, например: «что по Облако Плюс»."
        if self.memory is None:
            return "Память помощника не подключена."
        facts = self.memory.history(query) or self.memory.search(query)
        if not facts:
            return f"В памяти ничего нет по «{html.escape(query)}»."
        lines = [f"<b>Что известно: {html.escape(query)}</b>"]
        for g in collapse(facts)[-8:]:      # одинаковый статус подряд — одна строка с датами и числом повторов
            f, last = g[0], g[-1]
            src = ", ".join(x.source for x in g)
            lines.append(f"• {period(g)} {html.escape(f.who)} — {html.escape(last.what)}\n"
                         f"  <i>«{html.escape(f.quote)}»</i> ({html.escape(src)})")
        self._log("memory_recall", actor="human", summary=f"{query}: фактов {len(facts)}")
        return "\n".join(lines)

    def meeting_brief(self, days: int = 7) -> tuple[str, list[str]]:
        return meeting_brief(self.j, self.memory, self.today(), days)

    def review_reports(self) -> int:
        """Ревизор отчётов: вопросы исполнителям — сам, руководителю — одна строка. Один вопрос по теме в сутки."""
        today = self.today()
        done = {(a["payload"].get("kind"), a["payload"].get("topic"), a["payload"].get("day"))
                for a in self.j.actions() if a["kind"] == "report_review"}
        n = 0
        for f in review(self.reports, self.tasks, self.p.org, self.p.goals, today):
            key = (f.kind, f.topic, today.isoformat())
            if key in done:
                continue
            if f.to_email:
                self.send(f.to_email, "review_question", "вопрос_ревизора", тема=f.topic, вопрос=f.question,
                          имя=f.to_name)
            if f.leader:
                self.risks.append(f.leader)
            self.digest.append(f.digest)
            self._log("report_review", path="now", summary=f.digest,
                      payload={"kind": f.kind, "topic": f.topic, "day": today.isoformat(), "to": f.to_email,
                               "to_name": f.to_name, "question": f.question,
                               "sources": list(f.sources), "minutes_saved": 5 if f.to_email else 2})
            n += 1
        return n

    def _act(self, e, d: Decision, eid: int, report: Report | None = None, *, recheck: bool = False):
        path = "decision" if d.action == "спросить_руководителя" else "now"
        saved = MINUTES_SAVED[d.action] - (MINUTES_SAVED["спросить_руководителя"] if recheck else 0)
        payload = {"category": d.category, "delegate_to": d.delegate_to, "minutes_saved": saved,
                   "reason": d.reason, "summary": d.summary, "confidence": d.confidence}
        after = self._hr_question_about(e) if d.action == "делегировать" else []
        if after:
            payload["after"] = after
        if recheck:
            payload["recheck"] = True
        if report:      # отчёт хранится в журнале, чтобы ревизор видел его и после перезапуска
            payload["report"] = {"id": report.id, "from_name": report.from_name, "from_email": report.from_email,
                                 "received": report.received.isoformat(), "subject": report.subject,
                                 "body": report.body}
        self._log(f"mail_{d.action}", path=path, event_id=eid, summary=f"{e.subject} — {d.reason}", payload=payload)
        if d.action == "спросить_руководителя":
            rec = getattr(e, "received", None)
            self.decisions.append({"event_id": eid, "at": rec.isoformat() if rec else self.clock.now().isoformat(),
                                   "from": e.from_name, "subject": e.subject, "category": d.category,
                                   "summary": d.summary, "reason": d.reason, "email": e.from_email, "id": e.id})
        elif d.action == "делегировать" and after:
            self.held.append({"event_id": eid, "after": after, "delegate_to": d.delegate_to})
            self.digest.append(f"Ждёт вашего кадрового решения, потом уйдёт исполнителю: {e.subject}")
        elif d.action == "делегировать" and self.p.org:
            self._forward(e, d.delegate_to, eid)
        elif d.action == "авто":
            self.send(e.from_email, "auto_reply", "автоответ", in_reply_to=e.id, event_id=eid,
                      тема=e.subject, отправитель=e.from_name)
            self.digest.append(f"Подтверждено автоматически: {e.subject}")
        elif d.action == "в_сводку":
            self.digest.append(f"{e.from_name}: {d.summary or e.subject}")

    def _forward(self, e, delegate_to: str, eid: int, note: str = ""):
        who = self.p.org.by_id[delegate_to]
        self.send(self.p.org.email_of(who), "delegate", "передача", in_reply_to=e.id, event_id=eid,
                  имя=who.name.split()[0], тема=e.subject, отправитель=e.from_name, текст=e.body)
        self.digest.append(f"Передано {who.name}: {e.subject}{note}")

    def _hr_question_about(self, e) -> list[int]:
        """Открытое кадровое решение про того же человека: передача ждёт его (доступ — после оффера)."""
        people = dq.names(f"{e.subject} {e.body}", exclude=(self.p.name,))
        if not people:
            return []
        for g in self.groups():
            if g.get("category") != "кадры":
                continue
            ev = self.j.event(g["event_ids"][0]) or {}
            if people & dq.names(f"{g['subject']} {(ev.get('payload') or {}).get('body', '')}", exclude=(self.p.name,)):
                return list(g["event_ids"])
        return []

    def _release(self, eids: list[int], verdict: str, subject: str):
        """Кадровое решение принято: ждавшие его передачи уходят исполнителю, при отказе — снимаются."""
        done = set(eids)
        sent = set()
        for h in [h for h in self.held if done & set(h["after"])]:
            self.held.remove(h)
            e = letter_ns(self.j, h["event_id"])
            key = dq.group_key(e.from_email, e.subject)
            if key not in sent:                 # копии одного письма передаём один раз
                sent.add(key)
                if verdict == "yes" and self.p.org:
                    self._forward(e, h["delegate_to"], h["event_id"], note=f" (после вашего решения: {subject})")
                else:
                    self.digest.append(f"Не передано: {e.subject} — по «{subject}» вы отказали")
            self._log("delegation_released", event_id=h["event_id"], path="now", summary=f"{verdict}: {e.subject}",
                      payload={"verdict": verdict, "after": subject})

    def recheck_queue(self) -> int:
        """Пересмотр очереди по текущим правилам профиля: письма, которым руководитель больше не нужен, уходят сами.

        Повторный запуск ничего не меняет. Возвращает, сколько писем ушло из очереди."""
        moved = 0
        for x in list(self.decisions):
            last = [a for a in self.j.actions(x["event_id"]) if is_mail_route(a["kind"])][-1]
            pl = last["payload"] or {}
            conf = pl.get("confidence")
            if conf is None:                    # старые записи: уверенность только в тексте причины
                m = re.search(r"уверенность разбора: ([\d.]+)", pl.get("reason", ""))
                conf = float(m.group(1)) if m else 1.0
            d = decide(self.p, pl.get("category", ""), x["email"], summary=pl.get("summary", ""), confidence=conf)
            if d.action == "спросить_руководителя":
                continue
            self.decisions.remove(x)
            e = letter_ns(self.j, x["event_id"])
            report = Report(e.id, e.from_name, e.from_email, e.received or self.clock.now(), e.subject, e.body) \
                if d.category in REPORT_CATEGORIES else None
            self._act(e, d, x["event_id"], report, recheck=True)
            if report:
                self.reports.append(report)
            moved += 1
        if moved:
            self._log("queue_rechecked", summary=f"по новым правилам ушло из очереди: {moved}", payload={"moved": moved})
        return moved

    # ---------------- очередь решений ----------------
    def groups(self) -> list[dict]:
        """Вопросы к руководителю: копии одного письма — одна карточка, с готовым решением, если оно подготовлено."""
        return [{**g, "prepared": self.prepared.get(g["key"])} for g in dq.group(self.decisions)]

    def prepare_decisions(self, limit: int | None = None) -> int:
        """Готовит решения по вопросам без подготовки: черновик, совет, факты с источниками. Один вызов модели на вопрос."""
        n = 0
        for g in self.groups():
            if g["prepared"] or (limit is not None and n >= limit):
                continue
            e = letter_ns(self.j, g["event_ids"][-1])
            letter = f"{e.subject}\n{e.body}"
            copies = {letter_ns(self.j, eid).id for eid in g["event_ids"]}
            srcs = dq.sources(letter, goals=self.p.goals, tasks=self.tasks, meetings=self.meetings, memory=self.memory,
                              org=self.p.org, sender_email=e.from_email, today=self.today(), exclude=copies)
            p = {**dq.prepare(self.llm, g, letter, srcs, self.p.style, self.p.name), "key": g["key"],
                 "v": dq.PREP_VERSION}
            self.prepared[g["key"]] = p
            self._log("decision_prepared", event_id=g["event_id"], path="now",
                      summary=f"{g['subject']}: совет — {p['advice']}, фактов {len(p['facts'])}", payload=p)
            n += 1
        return n

    # ---------------- план дня ----------------
    def morning_plan(self, day: date) -> int:
        items = [f"⚠️ {r}" for r in self.risks[-3:]]
        gs = self.groups()
        items += [f"{x['from']}: {x['subject']}{self._times(x)} — {x['reason']}" for x in gs[:10]]
        if len(gs) > 10:
            items.append(f"…и ещё {len(gs) - 10}")
        plan = build_day_plan(day, self.meetings, self.tasks, decisions=items, digest=self.digest[-8:])
        text = render_plan(plan, self.p.name)
        buttons = [[Button("Решения по одному", "dec:next")]] if self.decisions else None
        mid = self.pult.send_buttons(text, buttons) if buttons else self.pult.send_text(text)
        self._log("morning_plan", summary=f"{day.isoformat()}: встреч {len(plan.meetings)}, решений {len(gs)}")
        return mid

    @staticmethod
    def _times(g: dict) -> str:
        return f" (пишут {g['count']}-й раз)" if g["count"] > 1 else ""

    def _next_decision(self):
        gs = self.groups()
        if not gs:
            self.pult.send_text("Решений, которые ждут вас, больше нет.")
            return
        x, e = gs[0], html.escape
        lines = [f"<b>{e(x['from'])}</b>: {e(x['subject'])}{self._times(x)}", e(x["summary"]),
                 f"<i>Почему к вам: {e(x['reason'])}</i>"]
        p = x["prepared"]
        if p:
            if p.get("ask"):
                lines.append(f"\n<b>Запрос:</b> {e(p['ask'])}")
            bl = p.get("blockers") or []
            lines.append("<b>Что мешает:</b> " + ("" if bl else "в задачах, календаре и памяти препятствий не нашёл"))
            lines += [f"• {e(f['note'] or f['text'])} <i>({e(f['text'])})</i>" for f in bl]
            if p["facts"]:
                lines.append("<b>За:</b>")
                lines += [f"• {e(f['note'] or f['text'])} <i>({e(f['text'])})</i>" for f in p["facts"][:4]]
            lines.append(f"<b>Совет: {e(p['advice'])}</b>" + (f" — {e(p['why'])}" if p["why"] else ""))
            for key, label in (("draft_yes", "Если согласуете"), ("draft_no", "Если откажете")):
                if p.get(key):
                    lines.append(f"\n<b>{label}, уйдёт:</b>\n{e(p[key])}")
            if p["unverified"]:
                lines.append(f"⚠️ Числа нет ни в письме, ни в базе — проверьте: {e(', '.join(p['unverified']))}")
        eid = x["event_id"]
        self.pult.send_buttons("\n".join(lines),
                               [[Button("Согласовать", f"dec:yes:{eid}"), Button("Отказать", f"dec:no:{eid}")],
                                [Button("Позже", f"dec:later:{eid}")]])

    def decide_letter(self, eid: int, verdict: str, *, notify: bool = True, text: str | None = None) -> bool:
        """Решение руководителя по вопросу: из Telegram или из кабинета. Закрывает все копии письма одним ответом.

        «Согласовать» и «Отказать» отправляют свой подготовленный черновик или текст руководителя.
        False — такого письма в очереди нет."""
        if verdict not in ("yes", "no", "later"):
            raise ValueError(verdict)
        g = next((x for x in self.groups() if eid in x["event_ids"]), None)
        if not g:
            return False
        letters = [d for d in self.decisions if d["event_id"] in g["event_ids"]]
        for d in letters:
            self.decisions.remove(d)
        if verdict == "later":
            self.decisions.extend(letters)
        else:
            p = g["prepared"] or {}
            draft = p.get("draft_yes", p.get("draft", "")) if verdict == "yes" else p.get("draft_no", "")
            body = (text or "").strip() or draft
            if body:
                self._reply(g, body)
                if text and text.strip() != draft:
                    self._log("decision_draft_edit", actor="human", path="decision", event_id=g["event_id"],
                              summary=f"{g['subject']}: руководитель поправил ответ",
                              payload={"key": g["key"], "draft": draft, "text": text.strip()})
            else:
                self.send(g["email"], "decision_yes" if verdict == "yes" else "decision_no", "решение",
                          in_reply_to=g["id"], event_id=g["event_id"], тема=g["subject"], отправитель=g["from"])
        for d in letters:
            self._log("letter_decided", actor="human", path="decision", event_id=d["event_id"],
                      summary=f"{verdict}: {d['subject']}", payload={"verdict": verdict, "key": g["key"]})
        if verdict != "later":
            self._release(g["event_ids"], verdict, g["subject"])
        if notify:
            self._next_decision()
        return True

    def _reply(self, g: dict, body: str):
        """Ответ руководителя своим текстом (черновик помощника или правка), не по шаблону."""
        subject = f"Re: {g['subject']}"
        full = f"{body}\n\n— {self.p.name} (через ИИ-помощника)"
        mid = self.send_mail.send(g["email"], subject, full, in_reply_to=g["id"])
        self._log("mail_sent", path="now", event_id=g["event_id"], summary=f"{g['email']}: {subject}",
                  payload={"to": g["email"], "subject": subject, "body": full, "in_reply_to": g["id"],
                           "kind": "решение", "template": "", "message_id": mid})

    def _decide_letter(self, verdict: str, eid: int):
        self.decide_letter(eid, verdict)

    # ---------------- встречи ----------------
    def meeting_questions(self) -> list[str]:
        pid = self.protocols.last_approved()
        return questions_from_previous(self.protocols.protocol(pid, self.p.org)) if pid else []

    def previous_protocol(self, title: str, before: datetime) -> int | None:
        """Прошлый утверждённый протокол той же встречи (по названию), иначе последний утверждённый до неё."""
        approved = [r for r in (self.protocols.get(i) for i in self.protocols.ids())
                    if r["status"] == "утверждён" and r["meeting_end"] <= before]
        same = [r for r in approved if r["title"] == title]
        pick = same or approved
        return max(pick, key=lambda r: r["meeting_end"])["id"] if pick else None

    def new_meeting(self, transcript: str, title: str, meeting_end: datetime, meeting_uid: str | None = None) -> int:
        prev_id = self.previous_protocol(title, meeting_end)
        prev = self.protocols.protocol(prev_id, self.p.org) if prev_id else None
        p = build_protocol(self.llm, transcript, title=title, meeting_end=meeting_end, org=self.p.org,
                           rules=self.protocols.leader_rules(), previous=prev, previous_id=prev_id)
        pid = self.protocols.save_draft(p, transcript, meeting_uid=meeting_uid)
        self._send_protocol(pid, p)
        rec = self.protocols.get(pid)
        self._log("protocol_draft", path="decision", summary=f"{title}: {rec['minutes_to_draft']:.1f} мин после встречи",
                  payload={"protocol_id": pid, "minutes_to_draft": rec["minutes_to_draft"]})
        return pid

    def _send_protocol(self, pid: int, p) -> int:
        head = "Протокол на утверждение\n\n"
        if p.changes():
            head += (f"⚠️ Изменились задачи прошлого протокола: {len(p.changes())}. "
                     "Утверждая протокол, вы согласуете изменения.\n\n")
        return self.pult.send_buttons(
            head + md_to_html(render_markdown(p)),
            [[Button("Утвердить", f"prot:ok:{pid}"), Button("Править", f"prot:edit:{pid}"),
              Button("Отклонить", f"prot:no:{pid}")]])

    def approve_protocol(self, pid: int) -> int:
        if not self.protocols.approve(pid):
            return 0
        p = self.protocols.protocol(pid, self.p.org)
        rec = self.protocols.get(pid)
        body = render_markdown(p)
        to = self._protocol_recipients(p, rec)
        for addr in to:
            self.send(addr, "protocol_mail", "протокол", встреча=p.title, протокол=body)
        for i, t in enumerate(p.tasks, 1):
            self._log("task_assigned", path="now", summary=f"{t.owner}: {t.what} до {t.due.date().isoformat()}",
                      payload={"protocol_id": pid, "owner_id": t.owner_id, "due": t.due.isoformat(), "n": i,
                               "after": t.after, "owner": t.owner, "what": t.what})
        self._apply_previous(p, pid)
        self.remember(rec["transcript"], source=f"протокол {pid}", who=self.p.name, when=p.meeting_end, title=p.title)
        self._log("protocol_sent", actor="human", path="decision",
                  summary=f"{p.title}: разослано {len(to)}, задач {len(p.tasks)}",
                  payload={"protocol_id": pid, "minutes_to_approved": rec["minutes_to_approved"],
                           "minutes_saved": PROTOCOL_MINUTES_SAVED})
        self.pult.send_text(f"Протокол утверждён и разослан участникам встречи и исполнителям: {len(to)} адресатов, "
                            f"задач {len(p.tasks)}.")
        return len(to)

    def _protocol_recipients(self, p, rec: dict) -> list[str]:
        """Кому протокол: участники встречи по календарю, исполнители новых и изменённых задач. Руководителю — нет."""
        org = self.p.org
        if not org:
            return []
        m = next((x for x in self.meetings if rec.get("meeting_uid") and x.uid == rec["meeting_uid"]), None) or \
            next((x for x in self.meetings if x.title == p.title and x.start.date() == p.meeting_end.date()), None)
        by_name = {x.name: x for x in org.people}
        to = set()
        for a in (m.attendees if m else ()):
            if "@" in a:
                to.add(a.strip().lower())
            elif a in by_name:
                to.add(org.email_of(by_name[a]))
        ids = [t.owner_id for t in p.tasks] + [x["owner_id"] for x in p.previous if x["status"] != "не обсуждали"]
        to |= {org.email_of(org.by_id[i]) for i in ids if i in org.by_id}
        to.discard(org.email_of(org.leader))
        return sorted(to)

    def _apply_previous(self, p, pid: int) -> None:
        """Утверждённая сверка с прошлым протоколом: статусы задач, изменения, «можно начинать» по цепочке."""
        if not p.previous_id:
            return
        for x in p.previous:
            if x["status"] == "не обсуждали":
                continue
            self._log("task_update", path="decision", actor="human",
                      summary=f"{x['owner']}: {x['was_what']} — {x['status']}",
                      payload={"protocol_id": p.previous_id, "n": x["n"], "status": x["status"],
                               "what": x["new_what"] or None,
                               "due": x["new_due"].isoformat() if x["new_due"] else None, "by_protocol": pid})
        self._release_dependents()

    def _release_dependents(self) -> None:
        """Задача, у которой все предшественники выполнены, начинается: исполнителю письмо «можно начинать» один раз."""
        tasks = protocol_tasks(self.j)
        told = {(a["payload"]["protocol_id"], a["payload"]["n"]) for a in self.j.actions() if a["kind"] == "task_unblocked"}
        for t in tasks:
            if not t["after"] or (t["protocol_id"], t["n"]) in told or t["status"].startswith("ждёт"):
                continue
            if t["status"] in TASK_DONE or not t["owner_id"] or not self.p.org:
                continue
            who = self.p.org.by_id.get(t["owner_id"])
            if not who:
                continue
            before = ", ".join(f"«{x['what']}»" for x in tasks if x["protocol_id"] == t["protocol_id"] and x["n"] in t["after"])
            self.send(self.p.org.email_of(who), "task_go", "можно_начинать", имя=who.name.split()[0], задача=t["what"],
                      после=before, срок=t["due"].strftime("%d.%m"))
            self._log("task_unblocked", path="now", summary=f"{who.name}: можно начинать «{t['what']}»",
                      payload={"protocol_id": t["protocol_id"], "n": t["n"], "minutes_saved": 3})

    # ---------------- Telegram ----------------
    def handle(self, u: Update) -> None:
        if u.kind == "callback":
            if u.callback_id:
                self.pult.answer_callback(u.callback_id)
            parts = u.text.split(":")
            if parts[0] == "prot":
                pid = int(parts[2])
                if parts[1] == "ok":
                    self.approve_protocol(pid)
                elif parts[1] == "no":
                    self.protocols.reject(pid)
                    self._log("protocol_rejected", actor="human", path="decision", payload={"protocol_id": pid})
                    self.pult.send_text("Протокол отклонён.")
                elif parts[1] == "edit":
                    self.pending = Pending("edit", pid)
                    self.pult.send_text("Напишите правку одним сообщением, например: «исполнитель второй задачи — Ольга, срок пятница».")
            elif parts[0] == "dec":
                if parts[1] == "next":
                    self._next_decision()
                else:
                    self._decide_letter(parts[1], int(parts[2]))
            return
        if u.kind != "text":
            return
        text = u.text.strip()
        if self.pending and self.pending.kind == "edit" and not text.startswith("/"):
            pid = self.pending.ref
            self.pending = None
            p = self.protocols.apply_edit(pid, text, self.llm, self.p.org)
            self._log("protocol_edit", actor="human", path="decision", summary=text, payload={"protocol_id": pid})
            self._send_protocol(pid, p)
            return
        cmd = re.split(r"\s+", text)[0].lower()
        m = re.match(r"(?:/chto|что\s+по|что\s+с|что\s+известно\s+(?:о|об|про|по))(?:\s+|$)(.*)", text, re.I | re.S)
        if m:
            self.pult.send_text(self.recall(m.group(1)))
            return
        if cmd in ("/plan", "план"):
            self.morning_plan(self.today())
        elif cmd in ("/pochta", "почта"):
            c = self.process_inbox()
            self.pult.send_text("Почта разобрана: " + (", ".join(f"{k} {v}" for k, v in c.items()) or "новых писем нет"))
        elif cmd in ("/voprosy", "вопросы"):
            q = self.meeting_questions()
            self.pult.send_text("Вопросы по прошлому протоколу:\n" + "\n".join(f"• {html.escape(x)}" for x in q)
                                if q else "Утверждённых протоколов пока нет.")
        elif cmd == "/start":
            self.pult.send_text("Я ИИ-помощник руководителя. Команды: /plan, /pochta, /voprosy, «что по …» (/chto).")
