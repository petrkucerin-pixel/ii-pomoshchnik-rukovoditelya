"""Помощник одного руководителя: связывает профиль, почту, Telegram, протоколы и журнал.

Все действия пишутся в журнал. Решения из списка «никогда без подтверждения» уходят руководителю в Telegram.
"""
from __future__ import annotations

import html
import logging
import os
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from yadro.clock import TZ_EKB
from yadro.journal import Journal

from . import decisions as dq
from .channels.telegram import Button, Update
from .memory import brief, collapse, extract_facts, period
from .plan import (Meeting, build_day_plan, load_calendar, load_tasks, plural, render_digest, render_morning,
                   render_schedule)
from .profile import Profile
from .reports import Report, review
from .protocol import ProtocolStore, build_protocol, questions_from_previous, render_markdown
from .templates import Templates
from .triage import Decision, classify, decide
from .writer import StyleBook, audience_of

# Сколько минут руководителя экономит одно действие помощника. Допущения для метрики «освобождённые часы».
DEFER = timedelta(hours=2)        # «Позже»: вопрос скрыт столько времени, потом возвращается
MINUTES_SAVED = {"авто": 3, "делегировать": 5, "в_сводку": 2, "игнорировать": 1, "спросить_руководителя": 2,
                 "уточнить": 3}
CLARIFY_TEXT = ("Здравствуйте! Я ИИ-помощник {leader}. Уточните, пожалуйста, по какому вопросу вы пишете и что "
                "нужно — отвечу сам или передам нужному человеку.")
PROTOCOL_MINUTES_SAVED = 30
# Письма без фактов для памяти: на них не тратим вызов модели.
MEMORY_SKIP = {"", "рассылка", "приглашение", "доступы", "календарь", "спам"}   # спам не запоминаем (28.11)


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
    """Действие разбора входящего письма — не исходящее, не «прочитано / удалено / восстановлено»."""
    from .metrics import ROUTES
    return kind in ROUTES


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


def digest_from_journal(journal: Journal, org=None, since: datetime | None = None) -> list[str]:
    """Сводка «что помощник сделал сам»: только его действия, глаголом. Что ждёт руководителя и риски — в своих
    разделах, сюда не попадают. Копии одного письма — одна строка с числом раз."""
    lines: list[str] = []
    for a in journal.actions():
        if since is not None and a["at"] < since:      # «сделал без вас» за сегодня, а не вся история
            continue
        k, pl = a["kind"], a["payload"] or {}
        if k in ("mail_делегировать", "mail_авто", "mail_в_сводку"):
            ev = (journal.event(a["event_id"]) or {}).get("payload") or {} if a["event_id"] else {}
            subj = ev.get("subject") or a["summary"].split(" — ", 1)[0]
            frm = ev.get("from_name") or ev.get("from") or ""
            if k == "mail_делегировать":
                if pl.get("after"):
                    continue                     # ещё не передано: ждёт кадрового решения руководителя
                who = org.by_id.get(pl.get("delegate_to")) if org else None
                lines.append(f"Передал «{subj}» ({frm}) → {who.name if who else 'исполнителю'}")
            elif k == "mail_авто":
                lines.append(f"Ответил «принято»: «{subj}» ({frm})")
            elif "служебн" in (pl.get("reason") or ""):
                lines.append(f"Служебное уведомление «{subj}» — никому не пересылал")
            else:
                lines.append(f"Прочитал: {frm} — {pl.get('summary') or subj}")
        elif k == "delegation_released" and pl.get("verdict") == "yes":
            lines.append(f"После вашего решения передал: {a['summary'].split(': ', 1)[-1]}")
        elif k == "report_review" and pl.get("kind") != "риск_цели":
            lines.append(a["summary"])
        elif k == "task_unblocked":
            lines.append(a["summary"])
    counts: dict[str, int] = {}
    for x in lines:
        counts[x] = counts.get(x, 0) + 1
    out, seen = [], set()
    for x in reversed(lines):                    # порядок по последнему разу
        if x not in seen:
            seen.add(x)
            out.append(x + (f" (×{counts[x]})" if counts[x] > 1 else ""))
    return list(reversed(out))


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
        self.files_dir: Path | None = None        # куда класть вложения; задаёт стенд, в тестах — временная папка
        self.templates = Templates(profile.dir)
        self.digest: list[str] = digest_from_journal(journal, profile.org)   # что помощник сделал сам; переживает перезапуск
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

    def now(self) -> datetime:
        """Сейчас на рабочем дне помощника. С DEMO_DATE — учебный день и настоящее время: письма, встречи
        и протоколы живут в одном календаре (проверка Пита 27.11: факты со встреч были на две недели «старше» писем)."""
        return self.on_work_day(self.clock.now())

    def on_work_day(self, moment: datetime) -> datetime:
        d = self.today()
        return moment if moment.date() == d else moment.replace(year=d.year, month=d.month, day=d.day)

    # ---------------- решения руководителя → память (TASK_17) ----------------
    def sync_memory(self) -> int:
        """Решения руководителя по вопросам — в память, к темам писем, о которых шла речь. Читает журнал, поэтому
        решение из Telegram, кабинета и голосом попадает одинаково и один раз. Возвращает, сколько записал."""
        if self.memory is None:
            return 0
        from .memory import Fact, clusters
        acts = self.j.actions()
        done = {(x["payload"].get("key"), x["payload"].get("verdict")) for x in acts if x["kind"] == "memory_decision"}
        last: dict[str, dict] = {}
        eids: dict[str, set] = {}
        for x in acts:
            pl = x["payload"] or {}
            if x["kind"] == "letter_decided" and pl.get("verdict") in ("yes", "no"):
                last[pl["key"]] = x
                eids.setdefault(pl["key"], set()).add(x["event_id"])
        replies = {x["event_id"]: x["payload"].get("body", "") for x in acts
                   if x["kind"] == "mail_sent" and x["payload"].get("kind") == "решение"}
        facts = self.memory.all()
        group_of = {f.topic: i for i, g in enumerate(clusters(facts)) for f in g}
        n = 0
        for key, x in last.items():
            verdict = x["payload"]["verdict"]
            if (key, verdict) in done:
                continue
            evs = [e for e in (self.j.event(i) for i in sorted(eids[key]) if i) if e]
            letters = {e["external_id"] for e in evs}
            subject = next((e["payload"].get("subject") for e in evs if e["payload"].get("subject")), key)
            subject = re.sub(r"^((re|fwd?)\s*:\s*)+", "", subject, flags=re.I).strip()
            one_per_topic: dict[int, str] = {}         # склеенная тема — одна запись решения, а не по каждому названию
            for t in sorted({f.topic for f in facts if f.source in letters}):
                one_per_topic.setdefault(group_of[t], t)
            topics_ = list(one_per_topic.values()) or [subject]
            body = next((replies[i] for i in sorted(eids[key]) if replies.get(i)), "")
            said = next((ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("—")), "")
            word = "согласовано" if verdict == "yes" else "отказ"
            what = f"{word} — {subject}"
            self.memory.add([Fact(topic=t, kind="решение", what=what, who=self.p.name, when=self.on_work_day(x["at"]),
                                  source=f"решение {x['event_id']}", quote=said[:200] or word) for t in topics_])
            self._log("memory_decision", path="now", event_id=x["event_id"], summary=what,
                      payload={"key": key, "verdict": verdict, "topics": topics_})
            n += 1
        return n

    def _log(self, kind: str, *, actor="assistant", path=None, summary="", payload=None, event_id=None):
        self.j.log_action(kind, actor=actor, path=path, summary=summary, payload=payload or {}, event_id=event_id)

    @property
    def writer(self) -> StyleBook:
        """Как пишет руководитель: правила стиля и тексты писем, которые помощник составил себе сам (TASK_14)."""
        return StyleBook(self.llm, self.j, self.templates, style=self.p.style, org=self.p.org)

    @property
    def signature(self) -> str:
        return f"— ИИ-помощник {os.environ.get('LEADER_NAME_GENITIVE') or self.p.name}"

    def send(self, to: str, template: str, kind: str, *, in_reply_to=None, event_id=None, **values) -> str:
        """Письмо в стиле руководителя: текст помощник составил сам по правилам стиля, факты подставляет код.
        Каждое исходящее — в журнал, кабинет показывает его в переписке."""
        values.setdefault("подпись", self.signature)
        values.setdefault("руководитель", self.p.name)
        subject, body = self.writer.render(template, audience_of(self.p.org, to), **values)
        mid = self.send_mail.send(to, subject, body, in_reply_to=in_reply_to)
        self._log("mail_sent", path="now", event_id=event_id, summary=f"{to}: {subject}",
                  payload={"to": to, "subject": subject, "body": body, "in_reply_to": in_reply_to,
                           "kind": kind, "template": template, "message_id": mid})
        return mid

    # ---------------- почта ----------------
    def process_inbox(self) -> dict:
        self.writer.learn_pending()             # правки руководителя в карточках → правила стиля для следующих писем
        self.sync_memory()                      # решения руководителя → память
        letters = self.mail.fetch_new()
        counts: dict[str, int] = {}
        for e in letters:
            payload = letter_payload(e)
            if getattr(e, "attachments", None):
                payload["attachments"] = self.store_files(e.attachments)
            eid, new = self.j.record_event("mail", e.id, payload)
            if not new:
                continue
            cat, summ, conf = classify(self.llm, e.subject, e.body, e.from_name)
            d = decide(self.p, cat, e.from_email, summary=summ or e.subject, confidence=conf)
            d = self._not_spam_if_known(d, e.from_email)
            rec = getattr(e, "received", None) or self.clock.now()
            asked = self._asked_subject(e)
            if self._clarified(e):
                # Ответ на «уточните, что нужно»: разбор по обычным правилам; снова непонятно — к руководителю.
                if d.action == "уточнить":
                    d = Decision("спросить_руководителя", d.category, "отправитель так и не уточнил, что нужно — "
                                 "решаете вы", summary=d.summary, confidence=d.confidence, sender_role=d.sender_role)
            elif asked:                          # ответ исполнителя на вопрос помощника о задаче — узнаёт код
                d = Decision("в_сводку", cat or "статус", "ответ исполнителя на вопрос помощника о задаче",
                             summary=summ or e.subject, confidence=conf, sender_role=d.sender_role)
                self._remember_answer(e, asked, rec, eid)
                from . import cabinet           # здесь, а не наверху: cabinet сам импортирует assistant
                cabinet.apply_answer_date(self, asked, e.body or "", e.from_name)
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

    def _clarified(self, e) -> bool:
        """Помощник уже спрашивал этого отправителя «уточните, что нужно» по этой теме."""
        frm = (e.from_email or "").lower()
        base = re.sub(r"^((re|fwd?|ответ)\s*:\s*)+", "", (e.subject or "").strip(), flags=re.I).strip().lower()
        for a in self.j.actions():
            pl = a["payload"] or {}
            if a["kind"] == "mail_sent" and pl.get("kind") == "уточнение" and (pl.get("to") or "").lower() == frm:
                sent = re.sub(r"^((re|fwd?|ответ)\s*:\s*)+", "", pl.get("subject") or "", flags=re.I).strip().lower()
                if sent == base:
                    return True
        return False

    def _asked_subject(self, e) -> str:
        """Тема вопроса помощника, на который это письмо отвечает: тот же исполнитель и «Re: <тема вопроса>»."""
        subj = re.sub(r"^((re|fwd?|ответ)\s*:\s*)+", "", (e.subject or "").strip(), flags=re.I).strip().lower()
        frm = (e.from_email or "").lower()
        for a in self.j.actions():
            pl = a["payload"] or {}
            if a["kind"] == "mail_sent" and pl.get("kind") in ("выяснение", "напоминание", "уточнение") \
                    and (pl.get("to") or "").lower() == frm and (pl.get("subject") or "").lower() == subj:
                return pl["subject"]
        return ""

    def _remember_answer(self, e, asked: str, when: datetime, eid: int) -> None:
        """Суть ответа исполнителя — в память сразу, без модели: следующая встреча её учтёт."""
        if self.memory is None:
            return
        from .memory import Fact
        m = re.search(r"«(.+)»", asked)
        task = m.group(1) if m else asked
        key = re.search(r"«([^»]+)»", task)
        body = " ".join((e.body or "").split())
        what = re.split(r"(?<=[.!?])\s", body, maxsplit=1)[0][:200] if body else "ответил без текста"
        quote = " ".join(body.split()[:20])
        facts = [Fact(topic=key.group(1) if key else task, kind="задача", what=f"{task}: {body[:240]}", who=e.from_name,
                      when=when, source=e.id, quote=quote or what)]
        n = self.memory.add(facts)
        if n:
            self._log("memory_saved", path="now", event_id=eid, summary=f"ответ {e.from_name} по «{task}»",
                      payload={"source": e.id, "facts": n, "task": task})

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
        self.sync_memory()                      # решение, принятое минуту назад, уже в ответе
        facts = self.memory.related(query)
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
        self.sync_memory()                      # аватар не спросит про то, что руководитель уже решил
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
                               "sources": list(f.sources), "leader": f.leader,
                               "minutes_saved": 5 if f.to_email else 2})
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
        elif d.action == "уточнить":
            if self._clarified(e):           # уже спрашивали — не переспрашиваем по кругу
                self._log("mail_спросить_руководителя", path="decision", event_id=eid,
                          summary=f"{e.subject} — отправитель так и не уточнил",
                          payload={**payload, "reason": "отправитель так и не уточнил, что нужно — решаете вы"})
                self.decisions.append({"event_id": eid, "at": (getattr(e, "received", None) or self.clock.now()).isoformat(),
                                       "from": e.from_name, "subject": e.subject, "category": d.category,
                                       "summary": d.summary, "reason": "отправитель так и не уточнил, что нужно",
                                       "email": e.from_email, "id": e.id})
                return
            subject = e.subject if e.subject.lower().startswith("re:") else f"Re: {e.subject}"
            body = CLARIFY_TEXT.format(leader=os.environ.get("LEADER_NAME_GENITIVE") or self.p.name)
            mid = self.send_mail.send(e.from_email, subject, f"{body}\n\n{self.signature}", in_reply_to=e.id)
            self._log("mail_sent", path="now", event_id=eid, summary=f"{e.from_email}: {subject}",
                      payload={"to": e.from_email, "subject": subject, "body": f"{body}\n\n{self.signature}",
                               "in_reply_to": e.id, "kind": "уточнение", "template": "", "message_id": mid})
            self.digest.append(f"Спросил, что нужно: {e.from_name} — {e.subject}")

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

    def _correspondents(self) -> set[str]:
        """Адреса, которым мы уже писали (из кабинета или из почты руководителя)."""
        return {((a["payload"] or {}).get("to") or "").strip().lower() for a in self.j.actions()
                if a["kind"] == "mail_sent"} - {""}

    def _not_spam_if_known(self, d: Decision, from_email: str) -> Decision:
        """Письмо от того, кому мы писали, спамом не бывает: модель ошиблась — решает руководитель."""
        if d.action != "игнорировать" or d.category != "спам":
            return d
        if (from_email or "").strip().lower() not in self._correspondents():
            return d
        return Decision("уточнить", "", "ответ от адреса, которому вы писали, — спамом не считаю; непонятно, что "
                        "нужно, — помощник уточнит", summary=d.summary, confidence=d.confidence, sender_role=d.sender_role)

    def rescue_known_spam(self) -> int:
        """Письма, скрытые как спам, хотя мы писали этому адресу, — возвращаются руководителю. Повтор — без изменений."""
        known = self._correspondents()
        moved = 0
        last: dict[int, dict] = {}
        for a in self.j.actions():
            if is_mail_route(a["kind"]) and a["event_id"]:
                last[a["event_id"]] = a
        for eid, a in last.items():
            pl = a["payload"] or {}
            if a["kind"] != "mail_игнорировать" or pl.get("category") != "спам":
                continue
            e = letter_ns(self.j, eid)
            if (e.from_email or "").lower() not in known:
                continue
            d = Decision("спросить_руководителя", "", "ответ от адреса, которому вы писали, — спамом не считаю, "
                         "решаете вы", summary=pl.get("summary") or e.subject, confidence=pl.get("confidence") or 1.0)
            self._act(e, d, eid, recheck=False)
            moved += 1
        if moved:
            self._log("spam_rescued", summary=f"вернул из спама: {moved}", payload={"moved": moved})
        return moved

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
        later: dict[str, datetime] = {}                     # когда руководитель последний раз нажал «Позже»
        for a in self.j.actions():
            pl = a["payload"] or {}
            if a["kind"] == "letter_decided" and pl.get("verdict") == "later" and pl.get("key"):
                later[pl["key"]] = a["at"]
        out = []
        for g in dq.group(self.decisions):
            until = later[g["key"]] + DEFER if g["key"] in later else None
            out.append({**g, "prepared": self.prepared.get(g["key"]),
                        "deferred_until": until.isoformat() if until else None})   # и прошедшее: утро напоминает
        return out

    def active_groups(self, now: datetime | None = None) -> list[dict]:
        """Вопросы, которые можно показывать сейчас: отложенные «Позже» ждут DEFER."""
        now = now or self.clock.now()
        return [g for g in self.groups() if not g["deferred_until"] or datetime.fromisoformat(g["deferred_until"]) <= now]

    def prepare_decisions(self, limit: int | None = None, only: set[str] | None = None) -> int:
        """Готовит решения по вопросам без подготовки: черновик, совет, факты с источниками. Один вызов модели на вопрос."""
        n = 0
        for g in self.groups():
            if g["prepared"] or (limit is not None and n >= limit) or (only is not None and g["key"] not in only):
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

    # ---------------- утро в Telegram блоками (TASK_15) ----------------
    # Пит 27.11: «Целая портянка. Давай блоками в определённое время… приятные для чтения и для действия».
    # 1) в время плана: приветствие и сразу первая карточка решения; 2) график и сроки — когда решения разобраны
    # или через 30 минут; 3) «Сделал без вас» — ещё через 30 минут. Каждый блок раз в день, отметки — в журнале.
    BLOCK_GAP = timedelta(minutes=30)

    def _blocks_sent(self) -> dict[str, datetime]:
        day = self.clock.now().date().isoformat()          # день отправки — настоящий, содержимое — рабочий день
        return {x["payload"]["block"]: x["at"] for x in self.j.actions()
                if x["kind"] == "tg_block" and x["payload"].get("day") == day}

    def _block_done(self, block: str, summary: str) -> None:
        self._log("tg_block", summary=summary, payload={"day": self.clock.now().date().isoformat(), "block": block})

    def _active(self) -> list[dict]:
        """Вопросы, которые ждут сейчас: без отложенных кнопкой «Позже» (их время ещё не пришло)."""
        return getattr(self, "active_groups", self.groups)()

    def _deferred_due(self, now: datetime) -> list[list[str]]:
        """Отложенные вопросы, у которых время пришло и о которых ещё не напоминали: [[key, deferred_until]]."""
        reminded = {tuple(x) for a in self.j.actions() if a["kind"] == "tg_remind" for x in a["payload"]["items"]}
        due = [[g["key"], g["deferred_until"]] for g in self.groups()
               if g.get("deferred_until") and datetime.fromisoformat(g["deferred_until"]) <= now]
        return [x for x in due if tuple(x) not in reminded]

    def morning_tick(self) -> str | None:
        """Вызывает цикл бота на каждом опросе. Возвращает, какой блок отправил, или None."""
        now = self.clock.now()
        if now.weekday() >= 5 or now.strftime("%H:%M") < self.p.plan_time:
            return None
        sent = self._blocks_sent()
        if "morning" not in sent:
            self.morning_plan(self.today())
            return "morning"
        due = self._deferred_due(now)
        if due:                                 # «Позже» — не по кругу, а один раз, когда пришло время
            n = len(due)
            self.pult.send_text(f"🔔 <b>Вы просили напомнить: {n} {plural(n, 'вопрос', 'вопроса', 'вопросов')}.</b>")
            self._next_decision()
            self._log("tg_remind", summary=f"напомнил об отложенных: {n}", payload={"items": due})
            return "remind"
        if "schedule" not in sent:
            if self._active() and now - sent["morning"] < self.BLOCK_GAP:
                return None
            self._send_schedule(self.today())
            self._block_done("schedule", "график и сроки")
            return "schedule"
        if "digest" not in sent and now - sent["schedule"] >= self.BLOCK_GAP:
            start = self.clock.now().replace(hour=0, minute=0, second=0, microsecond=0)
            items = digest_from_journal(self.j, self.p.org, since=start)[-8:]
            if items:
                self.pult.send_text(render_digest(items, os.environ.get("STAND_URL", "")))
            self._block_done("digest", f"сделал без вас: {len(items)}")
            return "digest"
        return None

    def _plan(self, day: date):
        return build_day_plan(day, self.meetings, self.tasks)

    def morning_plan(self, day: date) -> int:
        """Блок 1: приветствие, сколько решений ждут, что важно — и сразу первая карточка решения."""
        plan, gs = self._plan(day), self._active()
        mid = self.pult.send_text(render_morning(plan, self.p.name, len(gs), self.risks[-3:]))
        if gs:
            self._next_decision()
        self._log("morning_plan", summary=f"{day.isoformat()}: встреч {len(plan.meetings)}, решений {len(gs)}")
        self._block_done("morning", f"доброе утро: решений {len(gs)}")
        return mid

    def _send_schedule(self, day: date, *, with_decisions: bool = False) -> int:
        """Блок 2: встречи по времени (какие ведёт аватар) и сроки словами."""
        from .lead import avatar_leads, default_categories, lead_overrides
        plan = self._plan(day)
        ov, cats = lead_overrides(self.j), default_categories()
        text = render_schedule(plan, {m.uid for m in plan.meetings if avatar_leads(m, ov, cats)})
        n = len(self._active())
        if with_decisions and n:
            return self.pult.send_buttons(text, [[Button(f"Решения по одному ({n})", "dec:next")]])
        return self.pult.send_text(text)

    @staticmethod
    def _times(g: dict) -> str:
        return f" (пишут {g['count']}-й раз)" if g["count"] > 1 else ""

    def _next_decision(self):
        gs = self.active_groups()
        if not gs:
            rest = self.groups()
            if rest:
                soon = min(datetime.fromisoformat(g["deferred_until"]) for g in rest)
                self.pult.send_text(f"Остальные {len(rest)} вы отложили — вернусь к ним через 2 ч, "
                                    f"в {soon.astimezone(TZ_EKB):%H:%M}.")
            else:
                self.pult.send_text("Решений, которые ждут вас, больше нет.")
            return
        x, e = gs[0], html.escape
        times = f" · пишут {x['count']}-й раз" if x["count"] > 2 else ""
        lines = [f"<b>{e(x['from'])}</b> · {e(x['subject'])}{times}"]
        p = x["prepared"]
        if not p:
            lines += [e(x["summary"]), f"<i>{e(x['reason'])}. Готовлю черновик ответа…</i>"]
        else:
            lines.append(e(p.get("ask") or x["summary"]))
            bl = p.get("blockers") or []
            if bl:
                lines.append(f"⚠️ {e(bl[0]['note'] or bl[0]['text'])}" + (f" (и ещё {len(bl) - 1})" if len(bl) > 1 else ""))
            lines.append(f"\n<b>Совет: {e(p['advice'])}</b>" + (f" — {e(p['why'])}" if p["why"] else "")
                         if p["advice"] != "нет совета" else "\nСовета нет — решаете вы.")
            no = p["advice"] == "отказать"
            draft = p.get("draft_no") if no else p.get("draft_yes", p.get("draft"))
            if draft:
                lines.append(f"\n<b>{'Если откажете' if no else 'Если согласуете'}, уйдёт:</b>\n{e(draft)}")
            if p["unverified"]:
                lines.append(f"⚠️ Проверьте числа: {e(', '.join(p['unverified']))}")
        eid = x["event_id"]
        self.pult.send_buttons("\n".join(lines),
                               [[Button("Согласовать", f"dec:yes:{eid}"), Button("Отказать", f"dec:no:{eid}")],
                                [Button("Подробнее", f"dec:more:{eid}"), Button("Позже", f"dec:later:{eid}")]])

    def _decision_details(self, eid: int):
        """«Подробнее» к карточке: почему к вам, что мешает, доводы и оба черновика. Ничего не решает."""
        x = next((g for g in self.groups() if eid in g["event_ids"]), None)
        if not x:
            self.pult.send_text("Этот вопрос уже решён.")
            return
        e, p = html.escape, x["prepared"] or {}
        lines = [f"<b>{e(x['subject'])}</b> — подробно", f"<i>Почему к вам: {e(x['reason'])}</i>"]
        bl = p.get("blockers") or []
        lines.append("\n<b>Что мешает:</b>" + ("" if bl else " в задачах, календаре и памяти препятствий не нашёл"))
        lines += [f"• {e(f['note'] or f['text'])} <i>({e(f['text'])})</i>" for f in bl]
        if p.get("facts"):
            lines.append("<b>За:</b>")
            lines += [f"• {e(f['note'] or f['text'])} <i>({e(f['text'])})</i>" for f in p["facts"][:4]]
        for key, label in (("draft_yes", "Если согласуете"), ("draft_no", "Если откажете")):
            if p.get(key):
                lines.append(f"\n<b>{label}, уйдёт:</b>\n{e(p[key])}")
        self.pult.send_buttons("\n".join(lines),
                               [[Button("Согласовать", f"dec:yes:{eid}"), Button("Отказать", f"dec:no:{eid}")]])

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

    # ---------------- почта руководителя из кабинета ----------------
    def sync_sent(self) -> int:
        """Письма из «Отправленных» ящика, которых нет в журнале (руководитель отправил из своей почты),
        — в журнал как его письма. Отправленное из кабинета не задваивается: тот же Message-ID."""
        fetch = getattr(self.send_mail, "fetch_sent", None) or getattr(self.mail, "fetch_sent", None)
        if fetch is None:
            return 0
        letters = fetch()
        if not letters:
            return 0
        known = {(a["payload"] or {}).get("message_id") for a in self.j.actions() if a["kind"] == "mail_sent"}
        n = 0
        for e in letters:
            if e.id in known:
                continue
            row = self.j.conn.execute("SELECT id FROM events WHERE channel = 'mail' AND external_id = ?",
                                      (e.in_reply_to or "",)).fetchone() if getattr(e, "in_reply_to", None) else None
            to = getattr(e, "to", "") or ""
            self._log("mail_sent", actor="human", path="now", event_id=row["id"] if row else None,
                      summary=f"{to}: {e.subject}",
                      payload={"to": to, "subject": e.subject, "body": e.body, "in_reply_to": e.in_reply_to,
                               "reply_to_id": e.in_reply_to if row else None, "kind": "из_почты", "template": "",
                               "message_id": e.id, "sent_at": e.received.isoformat() if e.received else None,
                               "attachments": self.store_files(getattr(e, "attachments", ()))})
            known.add(e.id)
            n += 1
        return n

    def store_files(self, files) -> list[dict]:
        """Вложения (имя, тип, байты) на диск по хешу содержимого. Возвращает описание для журнала и кабинета."""
        import hashlib
        out = []
        for name, ctype, data in files or ():
            key = hashlib.sha256(data).hexdigest()[:32]
            safe = "".join("_" if ch in '\\/:*?"<>|' or ord(ch) < 32 else ch for ch in name)[:120] or "file"
            if self.files_dir is not None:
                d = self.files_dir / key
                d.mkdir(parents=True, exist_ok=True)
                (d / safe).write_bytes(data)
            out.append({"name": safe, "type": ctype or "application/octet-stream", "size": len(data), "key": key})
        return out

    def load_file(self, key: str, name: str) -> bytes | None:
        if self.files_dir is None or not re.fullmatch(r"[0-9a-f]{32}", key):
            return None
        p = self.files_dir / key / name
        return p.read_bytes() if p.is_file() and p.parent == self.files_dir / key else None

    def write_letter(self, to: str, subject: str, body: str, reply_to: str | None = None, files=(),
                     draft: str = "") -> str:
        """Письмо руководителя: ответ на входящее или новое. Подпись — его имя, в журнал как действие человека.
        files — вложения (имя, тип, байты). draft — что предлагал помощник: правка уходит в обучение стиля."""
        eid, in_reply = None, None
        if reply_to:
            row = self.j.conn.execute("SELECT id FROM events WHERE channel = 'mail' AND external_id = ?",
                                      (reply_to,)).fetchone()
            eid, in_reply = (row["id"], reply_to) if row else (None, None)
        full = body.strip() + "\n\n— " + self.p.name
        kw = {"attachments": tuple(files)} if files else {}
        mid = self.send_mail.send(to, subject, full, in_reply_to=in_reply, **kw)
        self._log("mail_sent", actor="human", path="now", event_id=eid, summary=f"{to}: {subject}",
                  payload={"to": to, "subject": subject, "body": full, "in_reply_to": in_reply,
                           "reply_to_id": in_reply, "kind": "ваше_письмо", "template": "", "message_id": mid,
                           "attachments": self.store_files(files)})
        if draft and draft.strip() != body.strip():
            self._log("reply_draft_edit", actor="human", path="now", event_id=eid, summary=f"{to}: правка ответа",
                      payload={"to": to, "draft": draft.strip(), "text": body.strip(), "letter_id": in_reply})
            try:                                 # правка сразу становится правилом для следующего ответа
                self.writer.learn_pending()
            except Exception:
                logging.getLogger(__name__).exception("обучение стиля на правке ответа")
        return mid

    # ---------------- задачи: решения руководителя из кабинета ----------------
    def task_action(self, task_id: str, task: str, owner: str, due: date, action: str, new_due: date | None = None):
        """«Напомнить» — письмо исполнителю; «перенести» и «выполнена» — запись в журнал поверх задачи."""
        payload = {"task_id": task_id, "action": action, "due": new_due.isoformat() if new_due else None}
        if action == "remind":
            question = f"по задаче «{task}» срок {due:%d.%m}. На каком этапе и когда будет готово?"
            if not self._task_letter(owner, task, due, question, "напоминание"):
                raise ValueError("исполнителя нет в оргструктуре — напомнить некому")
            summary = f"Напомнил {owner}: «{task}»"
        elif action == "move":
            summary = f"Срок «{task}» перенесён на {new_due:%d.%m}"
        else:
            summary = f"«{task}» отмечена выполненной"
        self._log("task_action", actor="human", path="now", summary=summary, payload=payload)

    def _task_letter(self, owner: str, task: str, due: date, question: str, kind: str) -> bool:
        """Письмо исполнителю по задаче. Тема одна и та же — ответ «Re:» находится по ней. False — адресата нет."""
        who = next((p for p in self.p.org.people if p.name == owner), None) if self.p.org else None
        if not who:
            return False
        to = self.p.org.email_of(who)
        subject = f"Задача «{task}» — срок {due:%d.%m}"
        body = f"{owner.split()[0]}, {question} Если что-то мешает — напишите, помогу.\n\n{self.signature}"
        mid = self.send_mail.send(to, subject, body)
        self._log("mail_sent", path="now", summary=f"{to}: {subject}",
                  payload={"to": to, "subject": subject, "body": body, "in_reply_to": None,
                           "kind": kind, "template": "", "message_id": mid})
        return True

    def ask_about_task(self, task_id: str, task: str, owner: str, due: date, question: str) -> bool:
        """Помощник сам выясняет у исполнителя, что с просроченной задачей. Руководителя не зовёт."""
        if not self._task_letter(owner, task, due, question, "выяснение"):
            return False
        self._log("task_action", path="now", summary=f"Спросил исполнителя ({owner}), что с «{task}»",
                  payload={"task_id": task_id, "action": "ask", "due": None, "minutes_saved": 5})
        return True

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
        self.remember(rec["transcript"], source=f"протокол {pid}", who=self.p.name, when=self.on_work_day(p.meeting_end),
                      title=p.title)        # память — на рабочем дне помощника, протокол — в настоящем времени
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
    # ---------------- голос и свободный текст в Telegram (TASK_16) ----------------
    transcribe = None                       # распознаватель голоса: стенд ставит Whisper, тесты — заглушку
    voice_pending: dict | None = None       # решение голосом ждёт одной кнопки «Отправить»: eid → (verdict, ответ)
    HELP = ("Можно сказать: «согласуй» или «откажи, потому что…» — по текущему вопросу; «что по Облако Плюс»; "
            "«напомни Ольге про КП к пятнице»; «назначь планёрку завтра в 10 с Ольгой»; "
            "«в протоколе срок второй задачи — пятница»; «план на сегодня».")

    def voice_to_text(self, u: Update) -> Update:
        """Голосовое → текст. Стенд зовёт это вне общего замка: Whisper работает секунды, кабинет не ждёт."""
        empty = Update(u.update_id, "voice_text", u.chat_id, "", u.message_id)
        if not self.transcribe or not hasattr(self.pult, "download"):
            self.pult.send_text("Голосовые пока не подключены — напишите, пожалуйста, текстом.")
            return empty
        try:
            heard = (self.transcribe(self.pult.download(u.file_id)) or "").strip()
        except Exception:
            logging.getLogger(__name__).exception("голосовое")
            self.pult.send_text("Не получилось распознать голосовое — повторите или напишите текстом.")
            return empty
        if not heard:
            self.pult.send_text("Не расслышал — повторите, пожалуйста.")
            return empty
        self.pult.send_text(f"🎙 <i>«{html.escape(heard)}»</i>")
        return Update(u.update_id, "voice_text", u.chat_id, heard, u.message_id)

    def act(self, text: str) -> str:
        """Просьба своими словами: модель разбирает в действие, код проверяет и выполняет. Возвращает действие."""
        from .intents import context_text, parse
        gs = self._active()
        card, proto = (gs[0] if gs else None), self._draft_protocol()
        people = [p.name for p in self.p.org.people] if self.p.org else []
        d = parse(self.llm, text, context_text(today=self.today(), card=card, protocol=proto, people=people))
        getattr(self, "_act_" + d["intent"])(d, card, proto)
        self._log("voice_intent", actor="human", path="now", summary=f"{d['intent']}: {text[:120]}",
                  payload={"text": text, "intent": d})
        return d["intent"]

    def _draft_protocol(self) -> dict | None:
        for pid in reversed(self.protocols.ids()):
            rec = self.protocols.get(pid)
            if rec and rec["status"] == "черновик":
                return rec
        return None

    def _person(self, name: str):
        """Человек из оргструктуры по имени, как его назвали вслух: «Ольга», «Ольге», «Смирнова». Один — или None."""
        if not self.p.org or not name.strip():
            return None
        words = [w[:4].lower().replace("ё", "е") for w in name.split() if len(w) >= 3]
        hits = [p for p in self.p.org.people if p.role != "руководитель" and words and all(
            any(x.lower().replace("ё", "е").startswith(w) for x in p.name.split()) for w in words)]
        return hits[0] if len(hits) == 1 else None

    def _act_unknown(self, d, card, proto):
        self.pult.send_text("Не понял. " + self.HELP)

    def _act_plan(self, d, card, proto):
        self._send_schedule(self.today(), with_decisions=True)

    def _act_recall(self, d, card, proto):
        self.pult.send_text(self.recall(str(d.get("query") or "").strip()))

    def _act_decision(self, d, card, proto):
        verdict = d.get("verdict")
        if not card:
            self.pult.send_text("Вопросов, которые ждут вашего решения, сейчас нет.")
            return
        if verdict == "later":
            self.decide_letter(card["event_id"], "later")
            return
        if verdict not in ("yes", "no"):
            self._act_unknown(d, card, proto)
            return
        p = card.get("prepared") or {}
        reply = str(d.get("reply") or "").strip()
        shown = reply or (p.get("draft_yes") or p.get("draft") if verdict == "yes" else p.get("draft_no")) or \
            "стандартный ответ помощника"
        eid = card["event_id"]
        if self.voice_pending is None:
            self.voice_pending = {}
        self.voice_pending[eid] = (verdict, reply)
        e = html.escape
        # ответ уходит наружу, а голос можно расслышать не так — одна кнопка, чтобы отправить
        self.pult.send_buttons(
            f"<b>{'Согласовать' if verdict == 'yes' else 'Отказать'}</b>: {e(card['from'])} — {e(card['subject'])}\n\n"
            f"<b>Уйдёт:</b>\n{e(shown)}",
            [[Button("Отправить", f"vok:{eid}"), Button("Не то", f"vno:{eid}")]])

    def _voice_confirm(self, ok: bool, eid: int):
        pend = (self.voice_pending or {}).pop(eid, None)
        if not ok:
            self.pult.send_text("Хорошо, не отправляю. Скажите ещё раз или ответьте кнопками в карточке.")
            return
        if pend is None:
            self.pult.send_text("Подтверждение устарело — скажите, пожалуйста, ещё раз.")
            return
        verdict, reply = pend
        if not self.decide_letter(eid, verdict, text=reply or None):
            self.pult.send_text("Этот вопрос уже решён.")

    def _act_assign(self, d, card, proto):
        from yadro.deadlines import parse_deadline
        who = self._person(str(d.get("person") or ""))
        what = str(d.get("what") or "").strip().rstrip(".")
        if not who or not what:
            self.pult.send_text(f"Не нашёл «{html.escape(str(d.get('person') or ''))}» в оргструктуре — "
                                "скажите имя и фамилию." if not who else "Не понял, что поручить. " + self.HELP)
            return
        due = parse_deadline(str(d.get("due_text") or ""), self.clock.now())
        when = f" Срок — {due.due:%d.%m}." if due else ""
        self.send(self.p.org.email_of(who), "assign", "поручение", имя=who.name.split()[0], что=what, срок=when)
        self._log("voice_assign", actor="human", path="now", summary=f"{who.name}: {what}{when}",
                  payload={"person": who.name, "what": what, "due": due.due.isoformat() if due else None})
        self.pult.send_text(f"Написал {html.escape(who.name)}: «{html.escape(what)}».{when}")

    def _act_meeting(self, d, card, proto):
        try:
            day = date.fromisoformat(str(d.get("date")))
            hh, mm = (int(x) for x in str(d.get("time")).split(":"))
            start = datetime(day.year, day.month, day.day, hh, mm, tzinfo=self.clock.now().tzinfo)
            minutes = max(15, min(int(d.get("minutes") or 30), 240))
        except (TypeError, ValueError):
            self.pult.send_text("Не понял дату и время встречи — скажите, например: «завтра в 10».")
            return
        title = str(d.get("title") or "Встреча").strip()
        found, missed = [], []
        for name in d.get("people") or []:
            p = self._person(str(name))
            (found if p else missed).append(p.name if p else str(name))
        self.create_meeting(title=title, start=start, minutes=minutes, category="операционная", attendees=found,
                            emails=[], link="", avatar=False)
        e = html.escape
        msg = f"Назначил «{e(title)}» на {start:%d.%m %H:%M}, {minutes} мин."
        msg += f" Пригласил: {e(', '.join(found))}." if found else ""
        msg += f" Не нашёл в оргструктуре: {e(', '.join(missed))}." if missed else ""
        self.pult.send_text(msg + " Ссылку на Телемост добавьте в карточке встречи в кабинете.")

    def _act_protocol_edit(self, d, card, proto):
        if not proto:
            self.pult.send_text("Протокола на утверждении сейчас нет.")
            return
        text = str(d.get("instruction") or "").strip()
        p = self.protocols.apply_edit(proto["id"], text, self.llm, self.p.org)
        self._log("protocol_edit", actor="human", path="decision", summary=text, payload={"protocol_id": proto["id"]})
        self._send_protocol(proto["id"], p)

    def handle(self, u: Update) -> None:
        if u.kind == "callback":
            if u.callback_id:
                self.pult.answer_callback(u.callback_id)
            parts = u.text.split(":")
            if parts[0] in ("vok", "vno"):
                self._voice_confirm(parts[0] == "vok", int(parts[1]))
                return
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
                elif parts[1] == "more":
                    self._decision_details(int(parts[2]))
                else:
                    self._decide_letter(parts[1], int(parts[2]))
            return
        if u.kind == "voice":
            u = self.voice_to_text(u)
        if u.kind == "voice_text":
            if u.text:
                self.act(u.text)
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
            self._send_schedule(self.today(), with_decisions=True)
        elif cmd in ("/pochta", "почта"):
            c = self.process_inbox()
            self.pult.send_text("Почта разобрана: " + (", ".join(f"{k} {v}" for k, v in c.items()) or "новых писем нет"))
        elif cmd in ("/voprosy", "вопросы"):
            q = self.meeting_questions()
            self.pult.send_text("Вопросы по прошлому протоколу:\n" + "\n".join(f"• {html.escape(x)}" for x in q)
                                if q else "Утверждённых протоколов пока нет.")
        elif cmd == "/start":
            self.pult.send_text("Я ИИ-помощник руководителя. Команды: /plan, /pochta, /voprosy, «что по …» (/chto). "
                                "Можно и своими словами — текстом или голосовым.")
        elif text.startswith("/"):
            self.pult.send_text("Такой команды нет. " + self.HELP)
        else:                                   # своими словами — тот же разбор, что и для голосовых
            self.act(text)
