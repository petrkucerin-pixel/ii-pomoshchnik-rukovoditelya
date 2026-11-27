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
У задачи может быть поле "after": [номера задач этого протокола, начиная с 1, после которых её можно начинать].
Ставь его, только если в речи прямо сказано, что задача идёт после других («после метрик», «когда будет витрина»).
Задачи без "after" идут параллельно.
Если в запросе есть задачи прошлого протокола, добавь в JSON поле
 "previous": [{"n": <номер задачи прошлого протокола>, "status": "выполнена | в работе | перенесена | изменилась | не обсуждали",
               "new_what": "<новая формулировка, если задача изменилась>", "new_due_text": "<новый срок словами, если его назвали>",
               "quote": "<дословный фрагмент расшифровки, где это прозвучало>"}]
«Изменилась» — поменялись суть, объём или исполнитель задачи; «перенесена» — поменялся только срок.
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
    after: list[int] = field(default_factory=list)      # номера задач этого протокола, после которых можно начинать


PREV_STATUSES = ("выполнена", "в работе", "перенесена", "изменилась", "не обсуждали")


@dataclass
class Protocol:
    title: str
    meeting_end: datetime
    topics: list[dict] = field(default_factory=list)
    decisions: list[Item] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    previous: list[dict] = field(default_factory=list)   # сверка с задачами прошлого протокола
    previous_id: int | None = None

    def changes(self) -> list[dict]:
        """Задачи прошлого протокола, которые изменились или перенесены: их согласует руководитель."""
        return [x for x in self.previous if x["status"] in ("изменилась", "перенесена")]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["meeting_end"] = self.meeting_end.isoformat()
        for t in d["tasks"]:
            t["due"] = t["due"].isoformat()
        for x in d["previous"]:
            for k in ("was_due", "new_due"):
                x[k] = x[k].isoformat() if x.get(k) else None
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
    _order(p, data.get("tasks", []))
    _previous(p, data, transcript)
    return p


def _order(p: Protocol, raw_tasks: list) -> None:
    """Зависимости: только на задачи выше по списку (так цикла не бывает). Срок раньше, чем у предшественника, — флаг."""
    for i, (task, raw) in enumerate(zip(p.tasks, raw_tasks), 1):
        after = []
        for x in (raw.get("after") or []) if isinstance(raw, dict) else []:
            try:
                n = int(x)
            except (TypeError, ValueError):
                continue
            if 1 <= n < i and n not in after:
                after.append(n)
        task.after = after
        for n in after:
            if task.due < p.tasks[n - 1].due:
                task.flags.append(f"срок раньше, чем у задачи {n}")
                p.open_questions.append(f"Задача {i} ({task.owner}) идёт после задачи {n}, но её срок "
                                        f"{task.due:%d.%m} раньше — уточнить сроки")


def previous_snapshot(prev: "Protocol") -> list[dict]:
    """Задачи прошлого протокола в том виде, в каком их видит модель и сверка. Хранится в новом протоколе."""
    return [{"n": i, "owner": t.owner, "owner_id": t.owner_id, "what": t.what, "due": t.due.isoformat()}
            for i, t in enumerate(prev.tasks, 1)]


def _previous(p: Protocol, data: dict, transcript: str) -> None:
    snap = {x["n"]: x for x in data.get("previous_tasks") or []}
    if not snap:
        return
    p.previous_id = data.get("previous_id")
    said = {}
    for x in data.get("previous") or []:
        try:
            n = int(x.get("n"))
        except (TypeError, ValueError, AttributeError):
            continue
        if n in snap and n not in said:
            said[n] = x
    for n, t in sorted(snap.items()):
        x = said.get(n, {})
        status = x.get("status") if x.get("status") in PREV_STATUSES else "не обсуждали"
        quote = str(x.get("quote") or "")
        quote_ok = bool(quote) and check_quote(quote, transcript, min_words=3).found
        if status != "не обсуждали" and not quote_ok:
            status = "не обсуждали"            # без подтверждения в расшифровке статус не меняем
        new_due = None
        if x.get("new_due_text") and status in ("изменилась", "перенесена"):
            dl = parse_deadline(str(x["new_due_text"]), p.meeting_end)
            new_due = dl.due if dl else None
        new_what = str(x.get("new_what") or "").strip() if status == "изменилась" else ""
        p.previous.append({"n": n, "owner": t["owner"], "owner_id": t.get("owner_id"), "was_what": t["what"],
                           "was_due": datetime.fromisoformat(t["due"]), "status": status, "new_what": new_what,
                           "new_due": new_due, "quote": quote, "quote_ok": quote_ok})


def _rules_block(rules: list[str]) -> str:
    if not rules:
        return ""
    return "\n\nПравки руководителя к прошлым протоколам — учитывай их:\n" + "\n".join(f"- {r}" for r in rules[-10:])


def build_protocol(llm, transcript: str, *, title: str, meeting_end: datetime, org: Org | None,
                   rules: list[str] | None = None, previous: "Protocol | None" = None,
                   previous_id: int | None = None) -> Protocol:
    user = f"Встреча: {title}\n"
    snap = previous_snapshot(previous) if previous and previous.tasks else []
    if snap:
        user += (f"\nЗадачи прошлого протокола «{previous.title}» от {previous.meeting_end:%d.%m}:\n"
                 + "\n".join(f"{x['n']}. {x['owner']}: {x['what']} — до {datetime.fromisoformat(x['due']):%d.%m}"
                             for x in snap) + "\n")
    data = llm.complete_json(SYSTEM + _rules_block(rules or []), f"{user}Расшифровка:\n{transcript}")
    if not isinstance(data, dict):
        data = {}
    if snap:
        data["previous_tasks"], data["previous_id"] = snap, previous_id
    return _from_json(data, transcript, title=title, meeting_end=meeting_end, org=org)


def _steps(p: Protocol) -> list[list[int]]:
    """Шаги выполнения: задача попадает на шаг после всех, от которых зависит. Задачи одного шага — параллельно."""
    level: dict[int, int] = {}
    for i, t in enumerate(p.tasks, 1):
        level[i] = 1 + max((level[n] for n in t.after), default=0)
    steps: dict[int, list[int]] = {}
    for i, lv in level.items():
        steps.setdefault(lv, []).append(i)
    return [steps[k] for k in sorted(steps)]


def _prev_line(x: dict) -> str:
    head = f"- {x['n']}. {x['owner']}: {x['was_what']} — {x['status']}"
    if x["status"] == "изменилась":
        new_due = x["new_due"] or x["was_due"]
        return (f"{head}: было «{x['was_what']}» до {_d(x['was_due'])} → стало «{x['new_what'] or x['was_what']}» "
                f"до {_d(new_due)} ⚠️ согласуйте")
    if x["status"] == "перенесена":
        return f"{head}: до {_d(x['was_due'])} → до {_d(x['new_due']) if x['new_due'] else 'срок не назван'} ⚠️ согласуйте"
    return head


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
    if p.previous:
        out += ["", "## По прошлому протоколу"]
        out += [_prev_line(x) for x in p.previous]
    out += ["", "## Дальнейшие шаги"]
    for i, t in enumerate(p.tasks, 1):
        flags = f" ⚠️ {', '.join(t.flags)}" if t.flags else ""
        after = f" (после {', '.join(map(str, t.after))})" if t.after else ""
        out.append(f"- [ ] {i}. {t.owner}: {t.what} — до {_d(t.due)}{after}{flags}")
    if any(t.after for t in p.tasks):
        parts = []
        for k, step in enumerate(_steps(p), 1):
            what = f"задача {step[0]}" if len(step) == 1 else f"задачи {', '.join(map(str, step))} — параллельно"
            parts.append(f"Шаг {k}: {what}")
        out.append("> Порядок: " + " → ".join(parts))
    if p.open_questions:
        out += ["", "## Предложения ИИ", "> Нерешённые вопросы и риски:"]
        out += [f"> {i}. {q}" for i, q in enumerate(p.open_questions, 1)]
    return "\n".join(out)


def questions_from_previous(p: Protocol) -> list[str]:
    qs = [f"{t.owner}, по задаче «{t.what}» срок {t.due.strftime('%d.%m')} — что готово? Задача не поменялась?"
          for t in p.tasks]
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
        data = data if isinstance(data, dict) else {}
        for k in ("previous_tasks", "previous_id", "previous"):   # сверку с прошлым протоколом правка не теряет
            if k in rec["data"] and k not in data:
                data[k] = rec["data"][k]
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

