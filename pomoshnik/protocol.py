"""Протокол встречи: расшифровка → протокол с цитатами → утверждение → правки руководителя.

Модель пишет черновик и находит цитаты. Код сверяет цитаты, исполнителей и сроки.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from yadro.db import connect
from yadro.deadlines import default_deadline, parse_deadline
from yadro.journal import from_db, to_db
from yadro.quotes import check_quote

from .org import Org

SYSTEM = """Ты секретарь руководителя. По расшифровке встречи составь протокол.
Верни JSON:
{"topics": [{"title": "...", "points": ["..."]}],
 "decisions": [{"text": "...", "quote": "<дословный фрагмент расшифровки>"}],
 "tasks": [{"owner": "<имя как в речи>", "what": "<что сделать>", "due_text": "<срок словами из речи или пусто>",
            "quote": "<дословный фрагмент расшифровки, где поручение прозвучало>"}],
 "open_questions": ["<что не решили, риски, кто и когда не определён>"]}
Правила: цитата — дословно из расшифровки, 4–20 слов. Не придумывай задачи, исполнителей и сроки, которых не было.
ИИ-помощник — не исполнитель: что он обещал «вынести руководителю», — задача руководителя (owner «Руководитель»).
Реплики подписаны «Участник» без имени: исполнителя бери из обращения («Ольга, …») или из того, как человек назвал себя.
Пиши коротко и по делу, с конкретным следующим шагом."""

EDIT_SYSTEM = """Ты секретарь руководителя. Руководитель прислал правку к протоколу.
Примени правку и верни протокол целиком в том же JSON-формате. Остальное не меняй."""


@dataclass
class Item:
    text: str
    quote: str
    quote_ok: bool = False


@dataclass
class Task:
    owner: str
    what: str
    due: datetime
    due_text: str = ""
    quote: str = ""
    owner_id: str | None = None
    quote_ok: bool = False
    default_due: bool = False
    flags: list[str] = field(default_factory=list)


@dataclass
class Protocol:
    title: str
    meeting_end: datetime
    topics: list[dict] = field(default_factory=list)
    decisions: list[Item] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["meeting_end"] = self.meeting_end.isoformat()
        for t in d["tasks"]:
            t["due"] = t["due"].isoformat()
        return d


def _resolve_owner(name: str, org: Org | None):
    if not org or not name:
        return None
    n = name.lower().replace("ё", "е").strip()
    if n in ("руководитель", "руководителю"):
        return org.leader
    for p in org.people:
        if p.name.lower().replace("ё", "е") == n:
            return p
    first = n.split()[0]
    hits = [p for p in org.people if p.name.lower().replace("ё", "е").split()[0] == first]
    return hits[0] if len(hits) == 1 else None


def _from_json(data: dict, transcript: str, *, title: str, meeting_end: datetime, org: Org | None) -> Protocol:
    p = Protocol(title=title, meeting_end=meeting_end, raw=data)
    p.topics = [t for t in data.get("topics", []) if isinstance(t, dict)]
    for d in data.get("decisions", []):
        q = d.get("quote", "")
        p.decisions.append(Item(d.get("text", ""), q, check_quote(q, transcript, min_words=3).found))
    for t in data.get("tasks", []):
        owner, quote, due_text = t.get("owner", ""), t.get("quote", ""), t.get("due_text", "") or ""
        person = _resolve_owner(owner, org)
        dl = parse_deadline(due_text, meeting_end) if due_text else None
        task = Task(owner=person.name if person else owner, what=t.get("what", ""),
                    due=dl.due if dl else default_deadline(meeting_end, 3), due_text=due_text, quote=quote,
                    owner_id=person.id if person else None,
                    quote_ok=check_quote(quote, transcript, min_words=3).found, default_due=dl is None)
        if not task.quote_ok:
            task.flags.append("не подтверждена расшифровкой")
        if not person:
            task.flags.append("уточнить исполнителя")
        if task.default_due:
            task.flags.append("срок по умолчанию")
        p.tasks.append(task)
    p.open_questions = [str(x) for x in data.get("open_questions", [])]
    return p


def _rules_block(rules: list[str]) -> str:
    if not rules:
        return ""
    return "\n\nПравки руководителя к прошлым протоколам — учитывай их:\n" + "\n".join(f"- {r}" for r in rules[-10:])


def build_protocol(llm, transcript: str, *, title: str, meeting_end: datetime, org: Org | None,
                   rules: list[str] | None = None) -> Protocol:
    data = llm.complete_json(SYSTEM + _rules_block(rules or []), f"Встреча: {title}\nРасшифровка:\n{transcript}")
    return _from_json(data, transcript, title=title, meeting_end=meeting_end, org=org)


def _d(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y")


def render_markdown(p: Protocol) -> str:
    out = [f"{p.title}", "", "> **Информация о встрече**", f"> Дата: {p.meeting_end.strftime('%Y-%m-%d %H:%M')}", ""]
    out.append("## Заметки о встрече")
    for t in p.topics:
        out.append(f"### {t.get('title', '')}")
        out += [f"- {x}" for x in t.get("points", [])]
    if p.decisions:
        out += ["", "## Решения"]
        out += [f"- {d.text}" + ("" if d.quote_ok else " ⚠️ не подтверждено расшифровкой") for d in p.decisions]
    out += ["", "## Дальнейшие шаги"]
    for i, t in enumerate(p.tasks, 1):
        flags = f" ⚠️ {', '.join(t.flags)}" if t.flags else ""
        out.append(f"- [ ] {i}. {t.owner}: {t.what} — до {_d(t.due)}{flags}")
    if p.open_questions:
        out += ["", "## Предложения ИИ", "> Нерешённые вопросы и риски:"]
        out += [f"> {i}. {q}" for i, q in enumerate(p.open_questions, 1)]
    return "\n".join(out)


def questions_from_previous(p: Protocol) -> list[str]:
    qs = [f"{t.owner}, по задаче «{t.what}» срок {t.due.strftime('%d.%m')} — что готово?" for t in p.tasks]
    qs += [f"В прошлый раз остался открытым вопрос: {q}. Есть решение?" for q in p.open_questions]
    return qs


_SCHEMA = """
CREATE TABLE IF NOT EXISTS protocols (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    meeting_end TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    transcript TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS protocol_edits (
    id INTEGER PRIMARY KEY,
    protocol_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    instruction TEXT NOT NULL
);
"""


class ProtocolStore:
    def __init__(self, path: str | Path, clock):
        self.clock = clock
        self.conn = connect(path)  # общий замок: веб, бот и роботы в разных потоках
        self.conn.executescript(_SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(protocols)")}
        if "meeting_uid" not in cols:       # базы, созданные до привязки протокола к встрече календаря
            self.conn.execute("ALTER TABLE protocols ADD COLUMN meeting_uid TEXT")
            self.conn.commit()

    def close(self):
        self.conn.close()

    def ids(self) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT id FROM protocols ORDER BY id")]

    def save_draft(self, p: Protocol, transcript: str = "", meeting_uid: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO protocols (title, meeting_end, created_at, status, data, transcript, meeting_uid) "
            "VALUES (?,?,?,?,?,?,?)",
            (p.title, to_db(p.meeting_end), to_db(self.clock.now()), "черновик",
             json.dumps(p.raw, ensure_ascii=False), transcript, meeting_uid))
        self.conn.commit()
        return cur.lastrowid

    def get(self, pid: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM protocols WHERE id = ?", (pid,)).fetchone()
        if not r:
            return None
        tz = self.clock.now().tzinfo
        end, created = from_db(r["meeting_end"], tz), from_db(r["created_at"], tz)
        approved = from_db(r["approved_at"], tz)
        edits = self.conn.execute("SELECT COUNT(*) FROM protocol_edits WHERE protocol_id = ?", (pid,)).fetchone()[0]
        return {"id": r["id"], "title": r["title"], "status": r["status"], "meeting_end": end,
                "meeting_uid": r["meeting_uid"],
                "data": json.loads(r["data"]), "transcript": r["transcript"], "edits": edits,
                "minutes_to_draft": (created - end).total_seconds() / 60,
                "minutes_to_approved": (approved - end).total_seconds() / 60 if approved else None}

    def protocol(self, pid: int, org: Org | None) -> Protocol:
        rec = self.get(pid)
        return _from_json(rec["data"], rec["transcript"], title=rec["title"], meeting_end=rec["meeting_end"], org=org)

    def _set_status(self, pid: int, status: str, approved: bool = False) -> bool:
        cur = self.conn.execute(
            "UPDATE protocols SET status = ?, approved_at = COALESCE(approved_at, ?) WHERE id = ? AND status = 'черновик'",
            (status, to_db(self.clock.now()) if approved else None, pid))
        self.conn.commit()
        return cur.rowcount == 1

    def approve(self, pid: int) -> bool:
        return self._set_status(pid, "утверждён", approved=True)

    def reject(self, pid: int) -> bool:
        return self._set_status(pid, "отклонён")

    def apply_edit(self, pid: int, instruction: str, llm, org: Org | None) -> Protocol:
        rec = self.get(pid)
        if rec is None or rec["status"] != "черновик":
            raise ValueError("править можно только черновик")
        user = (f"Протокол (JSON):\n{json.dumps(rec['data'], ensure_ascii=False)}\n\nПравка руководителя: {instruction}"
                f"\n\nРасшифровка для цитат:\n{rec['transcript']}")
        data = llm.complete_json(EDIT_SYSTEM, user)
        self.conn.execute("UPDATE protocols SET data = ? WHERE id = ?", (json.dumps(data, ensure_ascii=False), pid))
        self.conn.execute("INSERT INTO protocol_edits (protocol_id, at, instruction) VALUES (?,?,?)",
                          (pid, to_db(self.clock.now()), instruction))
        self.conn.commit()
        return _from_json(data, rec["transcript"], title=rec["title"],
                          meeting_end=rec["meeting_end"], org=org)

    def leader_rules(self, limit: int = 10) -> list[str]:
        rows = self.conn.execute("SELECT instruction FROM protocol_edits ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [r[0] for r in reversed(rows)]

    def last_approved(self) -> int | None:
        r = self.conn.execute("SELECT id FROM protocols WHERE status = 'утверждён' ORDER BY approved_at DESC LIMIT 1").fetchone()
        return r[0] if r else None

