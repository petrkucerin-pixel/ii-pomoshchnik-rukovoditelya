"""Помощник одного руководителя: связывает профиль, почту, Telegram, протоколы и журнал.

Все действия пишутся в журнал. Решения из списка «никогда без подтверждения» уходят руководителю в Telegram.
"""
from __future__ import annotations

import html
import os
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from yadro.journal import Journal

from .channels.telegram import Button, Update
from .memory import extract_facts
from .plan import build_day_plan, load_calendar, load_tasks, render_plan
from .profile import Profile
from .reports import Report, review
from .protocol import ProtocolStore, build_protocol, questions_from_previous, render_markdown
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


class Assistant:
    def __init__(self, profile: Profile, llm, pult, mailbox, journal: Journal, protocols: ProtocolStore, *,
                 calendar_path: str | Path, tasks_path: str | Path, outbox_mailbox=None, memory=None):
        self.p, self.llm, self.pult, self.mail = profile, llm, pult, mailbox
        self.memory = memory
        self.send_mail = outbox_mailbox or mailbox
        self.j, self.protocols = journal, protocols
        self.meetings = load_calendar(calendar_path)
        self.tasks = load_tasks(tasks_path)
        self.pending: Pending | None = None
        self.decisions: list[dict] = []      # письма, которые ждут руководителя
        self.digest: list[str] = []
        self.risks: list[str] = []           # риски для целей руководителя от ревизора
        self.reports: list[Report] = []      # отчёты и статусы команды для ревизора

    @property
    def clock(self):
        return self.j.clock

    def today(self) -> date:
        """Рабочий день помощника. DEMO_DATE в .env сдвигает его на даты учебного календаря заказчика."""
        demo = os.environ.get("DEMO_DATE")
        return date.fromisoformat(demo) if demo else self.clock.now().date()

    def _log(self, kind: str, *, actor="assistant", path=None, summary="", payload=None, event_id=None):
        self.j.log_action(kind, actor=actor, path=path, summary=summary, payload=payload or {}, event_id=event_id)

    # ---------------- почта ----------------
    def process_inbox(self) -> dict:
        letters = self.mail.fetch_new()
        counts: dict[str, int] = {}
        for e in letters:
            eid, new = self.j.record_event("mail", e.id, {"from": e.from_email, "subject": e.subject})
            if not new:
                continue
            cat, summ, conf = classify(self.llm, e.subject, e.body, e.from_name)
            d = decide(self.p, cat, e.from_email, summary=summ or e.subject, confidence=conf)
            self._act(e, d, eid)
            counts[d.action] = counts.get(d.action, 0) + 1
            rec = getattr(e, "received", None) or self.clock.now()
            if d.category not in MEMORY_SKIP:
                self.remember(e.body, source=e.id, who=e.from_name, when=rec, title=e.subject, event_id=eid)
            if d.category in ("отчёт", "статус"):
                self.reports.append(Report(e.id, e.from_name, e.from_email, rec, e.subject, e.body))
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
        groups: list[list] = []          # одинаковый статус подряд — одна строка с датами и числом повторов
        for f in facts:
            if groups and (groups[-1][0].who, groups[-1][0].quote.lower()) == (f.who, f.quote.lower()):
                groups[-1].append(f)
            else:
                groups.append([f])
        lines = [f"<b>Что известно: {html.escape(query)}</b>"]
        for g in groups[-8:]:
            f, last = g[0], g[-1]
            when = f"{f.when:%d.%m}" if len(g) == 1 else f"{f.when:%d.%m}–{last.when:%d.%m}, {len(g)} {'раза' if 2 <= len(g) % 10 <= 4 and not 12 <= len(g) % 100 <= 14 else 'раз'} без изменений"
            src = ", ".join(x.source for x in g)
            lines.append(f"• {when} {html.escape(f.who)} — {html.escape(last.what)}\n"
                         f"  <i>«{html.escape(f.quote)}»</i> ({html.escape(src)})")
        self._log("memory_recall", actor="human", summary=f"{query}: фактов {len(facts)}")
        return "\n".join(lines)

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
                self.send_mail.send(f.to_email, f"Вопрос по отчёту: {f.topic}",
                                    f.question + "\n\n— ИИ-помощник Ивана Петрова")
            if f.leader:
                self.risks.append(f.leader)
            self.digest.append(f.digest)
            self._log("report_review", path="now", summary=f.digest,
                      payload={"kind": f.kind, "topic": f.topic, "day": today.isoformat(), "to": f.to_email,
                               "sources": list(f.sources), "minutes_saved": 5 if f.to_email else 2})
            n += 1
        return n

    def _act(self, e, d: Decision, eid: int):
        path = "decision" if d.action == "спросить_руководителя" else "now"
        self._log(f"mail_{d.action}", path=path, event_id=eid, summary=f"{e.subject} — {d.reason}",
                  payload={"category": d.category, "delegate_to": d.delegate_to, "minutes_saved": MINUTES_SAVED[d.action]})
        if d.action == "спросить_руководителя":
            self.decisions.append({"event_id": eid, "from": e.from_name, "subject": e.subject,
                                   "summary": d.summary, "reason": d.reason, "email": e.from_email, "id": e.id})
        elif d.action == "делегировать" and self.p.org:
            who = self.p.org.by_id[d.delegate_to]
            self.send_mail.send(self.p.org.email_of(who), f"Fwd: {e.subject}",
                                f"{who.name.split()[0]}, возьмите, пожалуйста, в работу и сообщите итог.\n\n"
                                f"— {e.from_name}: {e.body}", in_reply_to=e.id)
            self.digest.append(f"Передано {who.name}: {e.subject}")
        elif d.action == "авто":
            self.send_mail.send(e.from_email, f"Re: {e.subject}", "Принято, подтверждаю. — ИИ-помощник Ивана Петрова",
                                in_reply_to=e.id)
            self.digest.append(f"Подтверждено автоматически: {e.subject}")
        elif d.action == "в_сводку":
            self.digest.append(f"{e.from_name}: {d.summary or e.subject}")

    # ---------------- план дня ----------------
    def morning_plan(self, day: date) -> int:
        items = [f"⚠️ {r}" for r in self.risks[-3:]]
        items += [f"{x['from']}: {x['subject']} — {x['reason']}" for x in self.decisions[:10]]
        if len(self.decisions) > 10:
            items.append(f"…и ещё {len(self.decisions) - 10}")
        plan = build_day_plan(day, self.meetings, self.tasks, decisions=items, digest=self.digest[-8:])
        text = render_plan(plan, self.p.name)
        buttons = [[Button("Решения по одному", "dec:next")]] if self.decisions else None
        mid = self.pult.send_buttons(text, buttons) if buttons else self.pult.send_text(text)
        self._log("morning_plan", summary=f"{day.isoformat()}: встреч {len(plan.meetings)}, решений {len(self.decisions)}")
        return mid

    def _next_decision(self):
        if not self.decisions:
            self.pult.send_text("Решений, которые ждут вас, больше нет.")
            return
        x = self.decisions[0]
        self.pult.send_buttons(
            f"<b>{html.escape(x['from'])}</b>: {html.escape(x['subject'])}\n{html.escape(x['summary'])}\n"
            f"<i>Почему к вам: {html.escape(x['reason'])}</i>",
            [[Button("Согласовать", f"dec:yes:{x['event_id']}"), Button("Отказать", f"dec:no:{x['event_id']}")],
             [Button("Позже", f"dec:later:{x['event_id']}")]])

    def _decide_letter(self, verdict: str, eid: int):
        x = next((d for d in self.decisions if d["event_id"] == eid), None)
        if not x:
            return
        self.decisions.remove(x)
        if verdict == "later":
            self.decisions.append(x)
        else:
            text = "Согласовано." if verdict == "yes" else "К сожалению, не можем согласовать."
            self.send_mail.send(x["email"], f"Re: {x['subject']}", f"{text}\n\n— Иван Петров (через ИИ-помощника)",
                                in_reply_to=x["id"])
        self._log("letter_decided", actor="human", path="decision", event_id=eid, summary=f"{verdict}: {x['subject']}")
        self._next_decision()

    # ---------------- встречи ----------------
    def meeting_questions(self) -> list[str]:
        pid = self.protocols.last_approved()
        return questions_from_previous(self.protocols.protocol(pid, self.p.org)) if pid else []

    def new_meeting(self, transcript: str, title: str, meeting_end: datetime) -> int:
        p = build_protocol(self.llm, transcript, title=title, meeting_end=meeting_end, org=self.p.org,
                           rules=self.protocols.leader_rules())
        pid = self.protocols.save_draft(p, transcript)
        self._send_protocol(pid, p)
        rec = self.protocols.get(pid)
        self._log("protocol_draft", path="decision", summary=f"{title}: {rec['minutes_to_draft']:.1f} мин после встречи",
                  payload={"protocol_id": pid, "minutes_to_draft": rec["minutes_to_draft"]})
        return pid

    def _send_protocol(self, pid: int, p) -> int:
        return self.pult.send_buttons(
            "Протокол на утверждение\n\n" + md_to_html(render_markdown(p)),
            [[Button("Утвердить", f"prot:ok:{pid}"), Button("Править", f"prot:edit:{pid}"),
              Button("Отклонить", f"prot:no:{pid}")]])

    def approve_protocol(self, pid: int) -> int:
        if not self.protocols.approve(pid):
            return 0
        p = self.protocols.protocol(pid, self.p.org)
        body = render_markdown(p)
        to = sorted({self.p.org.email_of(self.p.org.by_id[t.owner_id]) for t in p.tasks if t.owner_id} if self.p.org else set())
        for addr in to:
            self.send_mail.send(addr, f"Протокол: {p.title}", body)
        for t in p.tasks:
            self._log("task_assigned", path="now", summary=f"{t.owner}: {t.what} до {t.due.date().isoformat()}",
                      payload={"protocol_id": pid, "owner_id": t.owner_id, "due": t.due.isoformat()})
        rec = self.protocols.get(pid)
        self.remember(rec["transcript"], source=f"протокол {pid}", who=self.p.name, when=p.meeting_end, title=p.title)
        self._log("protocol_sent", actor="human", path="decision",
                  summary=f"{p.title}: разослано {len(to)}, задач {len(p.tasks)}",
                  payload={"protocol_id": pid, "minutes_to_approved": rec["minutes_to_approved"],
                           "minutes_saved": PROTOCOL_MINUTES_SAVED})
        self.pult.send_text(f"Протокол утверждён и разослан: {len(to)} адресатов, задач {len(p.tasks)}.")
        return len(to)

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
