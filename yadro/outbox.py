"""Исходящие с тремя путями: сразу, через окно отмены, после решения человека.

Само отправление делает канал (почта, Telegram): берёт `due()`, отправляет и зовёт `mark_sent()`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .journal import Journal, from_db, to_db

PATHS = {"now", "undo", "decision"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    path TEXT NOT NULL,
    status TEXT NOT NULL,
    send_at TEXT,
    created_at TEXT NOT NULL,
    event_id INTEGER,
    options TEXT NOT NULL DEFAULT '[]',
    external_id TEXT
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status, send_at);
"""


@dataclass
class Message:
    id: int
    channel: str
    recipient: str
    subject: str
    body: str
    path: str
    status: str
    send_at: datetime | None
    created_at: datetime
    event_id: int | None
    options: list[str] = field(default_factory=list)
    external_id: str | None = None


class Outbox:
    def __init__(self, journal: Journal, undo_minutes: int = 5):
        self.j = journal
        self.undo = timedelta(minutes=undo_minutes)
        self.j.conn.executescript(_SCHEMA)
        self.j.conn.commit()

    def _now(self) -> datetime:
        return self.j.clock.now()

    def _row(self, msg_id: int):
        return self.j.conn.execute("SELECT * FROM outbox WHERE id = ?", (msg_id,)).fetchone()

    def _msg(self, r) -> Message:
        tz = self.j.tz
        return Message(id=r["id"], channel=r["channel"], recipient=r["recipient"], subject=r["subject"],
                       body=r["body"], path=r["path"], status=r["status"], send_at=from_db(r["send_at"], tz),
                       created_at=from_db(r["created_at"], tz), event_id=r["event_id"],
                       options=json.loads(r["options"]), external_id=r["external_id"])

    def _log(self, kind: str, r, actor: str) -> None:
        self.j.log_action(kind, event_id=r["event_id"], actor=actor, path=r["path"],
                          payload={"message_id": r["id"], "channel": r["channel"]})

    def get(self, msg_id: int) -> Message:
        r = self._row(msg_id)
        if r is None:
            raise KeyError(msg_id)
        return self._msg(r)

    def put(self, channel: str, recipient: str, body: str, path: str, *, subject: str = "",
            event_id: int | None = None, options: list[str] | None = None) -> Message:
        if path not in PATHS:
            raise ValueError(f"неизвестный путь: {path}")
        options = list(options or [])
        now = self._now()
        if path == "decision":
            if not options and not body:
                raise ValueError("для решения нужны варианты или текст")
            status, send_at = "awaiting_decision", None
        else:
            status, send_at = "scheduled", (now if path == "now" else now + self.undo)
        cur = self.j.conn.execute(
            "INSERT INTO outbox (channel, recipient, subject, body, path, status, send_at, created_at, event_id, options)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (channel, recipient, subject, body, path, status, send_at and to_db(send_at), to_db(now), event_id,
             json.dumps(options, ensure_ascii=False)))
        self.j.conn.commit()
        r = self._row(cur.lastrowid)
        self._log("outbox_put", r, "assistant")
        return self._msg(r)

    def _before_send(self, r) -> bool:
        return r is not None and r["status"] == "scheduled" and to_db(self._now()) < r["send_at"]

    def cancel(self, msg_id: int) -> bool:
        r = self._row(msg_id)
        if not self._before_send(r):
            return False
        self.j.conn.execute("UPDATE outbox SET status = 'cancelled' WHERE id = ?", (msg_id,))
        self.j.conn.commit()
        self._log("outbox_cancel", r, "human")
        return True

    def edit(self, msg_id: int, body: str, send_now: bool = False) -> bool:
        r = self._row(msg_id)
        if not self._before_send(r):
            return False
        send_at = to_db(self._now()) if send_now else r["send_at"]
        self.j.conn.execute("UPDATE outbox SET body = ?, send_at = ? WHERE id = ?", (body, send_at, msg_id))
        self.j.conn.commit()
        self._log("outbox_edit", r, "human")
        return True

    def decide(self, msg_id: int, choice: int | None = None, body: str | None = None, reject: bool = False) -> bool:
        r = self._row(msg_id)
        if r is None or r["status"] != "awaiting_decision":
            return False
        if reject:
            self.j.conn.execute("UPDATE outbox SET status = 'rejected' WHERE id = ?", (msg_id,))
            self.j.conn.commit()
            self._log("outbox_reject", r, "human")
            return True
        options = json.loads(r["options"])
        if body is None:
            if choice is None or not 0 <= choice < len(options):
                raise ValueError("нет такого варианта")
            body = options[choice]
        self.j.conn.execute("UPDATE outbox SET body = ?, status = 'scheduled', send_at = ? WHERE id = ?",
                            (body, to_db(self._now()), msg_id))
        self.j.conn.commit()
        self._log("outbox_decide", r, "human")
        return True

    def due(self) -> list[Message]:
        rows = self.j.conn.execute(
            "SELECT * FROM outbox WHERE status = 'scheduled' AND send_at <= ? ORDER BY send_at, id",
            (to_db(self._now()),)).fetchall()
        return [self._msg(r) for r in rows]

    def awaiting_decision(self) -> list[Message]:
        rows = self.j.conn.execute("SELECT * FROM outbox WHERE status = 'awaiting_decision' ORDER BY id").fetchall()
        return [self._msg(r) for r in rows]

    def mark_sent(self, msg_id: int, external_id: str) -> bool:
        cur = self.j.conn.execute(
            "UPDATE outbox SET status = 'sent', external_id = ? WHERE id = ? AND status = 'scheduled'",
            (external_id, msg_id))
        self.j.conn.commit()
        if cur.rowcount != 1:
            return False
        self._log("outbox_sent", self._row(msg_id), "system")
        return True
