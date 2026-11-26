"""Данные кабинета руководителя: разделы по стр. 3 ТЗ. Только чтение баз и сборка JSON, решения — в помощнике.

Контракт ответов — TASK_08_КАБИНЕТ_GPT.md и tests/fixtures/cabinet/.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from yadro.journal import from_db

from .assistant import Assistant, meeting_brief
from .memory import collapse, period, topics
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


def _incoming(a: Assistant) -> list[dict]:
    rows = a.j.conn.execute("SELECT id, external_id, payload FROM events WHERE channel = 'mail' ORDER BY id").fetchall()
    acts: dict[int, dict] = {}
    for x in a.j.actions():
        if x["kind"].startswith("mail_") and x["kind"] != "mail_sent" and x["event_id"]:
            acts[x["event_id"]] = x
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
                    "reason": ap.get("reason") or (act["summary"].removeprefix(subject + " — ") if act else "")})
    return out


def _outgoing(a: Assistant) -> list[dict]:
    out = []
    for x in a.j.actions():
        if x["kind"] != "mail_sent":
            continue
        pl = x["payload"]
        t = a.templates.get(pl["template"]) if pl.get("template") else None
        out.append({"id": f"out-{x['id']}", "direction": "out", "at": _iso(x["at"]),
                    "from_name": a.signature.removeprefix("— "), "from_email": a.p.org.email_of(a.p.org.leader)
                    if a.p.org else "", "to_name": "", "to_email": pl.get("to", ""),
                    "subject": pl.get("subject", ""), "body": pl.get("body", ""), "category": pl.get("kind", ""),
                    "action": "отправлено", "reason": f"шаблон «{t.title}»" if t else ""})
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
    return {"groups": GROUPS, "letters": letters}


# ---------------- сегодня, задачи ----------------
def task_dict(t: TaskRow, today: date, source: str = "tasks.csv") -> dict:
    return {"id": t.id, "task": t.task, "owner": t.owner, "due": t.due.isoformat(), "status": t.status,
            "overdue": t.status not in DONE and (t.status == "просрочена" or t.due < today), "source": source}


def all_tasks(a: Assistant) -> list[tuple[TaskRow, str]]:
    """Задачи из файла заказчика и из утверждённых протоколов."""
    rows: list[tuple[TaskRow, str]] = [(t, "tasks.csv") for t in a.tasks]
    for x in a.j.actions():
        if x["kind"] != "task_assigned":
            continue
        pl = x["payload"]
        owner, what = (x["summary"].split(": ", 1) + [""])[:2]
        what = what.rsplit(" до ", 1)[0]
        rows.append((TaskRow(f"p{pl.get('protocol_id')}-{x['id']}", what, owner,
                             datetime.fromisoformat(pl["due"]).date(), "новая"), f"протокол {pl.get('protocol_id')}"))
    return rows


def tasks(a: Assistant) -> dict:
    today = a.today()
    return {"tasks": [task_dict(t, today, src) for t, src in all_tasks(a)]}


def meeting_short(m: Meeting, a: Assistant, plist: list[dict] | None = None) -> dict:
    p = _protocol_for(m, plist if plist is not None else _protocol_rows(a))
    emails = {}
    if a.p.org:
        emails = {p.name: a.p.org.email_of(p) for p in a.p.org.people}
    return {"uid": m.uid, "start": m.start.isoformat(), "end": m.end.isoformat(), "title": m.title,
            "category": m.category, "attendees": [{"name": n, "email": emails.get(n, "")} for n in m.attendees],
            "protocol_id": p["id"] if p else None, "protocol_status": p["status"] if p else None}


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
    plan = build_day_plan(day, a.meetings, a.tasks)
    plist = _protocol_rows(a)
    return {"day": day.isoformat(), "leader": a.p.name,
            "metrics": compute_metrics(a.j, protocols)["cards"],
            "decisions": [{k: d[k] for k in ("event_id", "at", "from", "subject", "summary", "reason")}
                          for d in a.decisions],
            "risks": _risks(a, day),
            "meetings": [meeting_short(m, a, plist) for m in plan.meetings],
            "overdue": [task_dict(t, day) for t in plan.overdue],
            "due_soon": [task_dict(t, day) for t in plan.due_soon],
            "focus": _focus(a.meetings, day),
            "digest": a.digest[-8:]}


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
    hq = [q for q in hq if q.split(" — ", 1)[0] in names] or hq
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
    groups = topics(mem)
    if q.strip():
        keys = {(f.topic, f.source, f.quote) for f in (mem.history(q) or mem.search(q))}
        groups = [fs for fs in groups if any((f.topic, f.source, f.quote) in keys for f in fs)]
    return {"q": q, "facts": len(mem.all()), "topics": [
        {"topic": fs[-1].topic, "kind": fs[-1].kind, "last_at": fs[-1].when.isoformat(), "last_what": fs[-1].what,
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
