"""Журнал: события, действия, вызовы модели. SQLite, одно хранилище на продукт.

Время храним в UTC строкой ISO одинаковой длины — так сравнение строк совпадает со сравнением времени.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .clock import Clock
from .db import connect

ACTORS = {"assistant", "human", "system"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    external_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (channel, external_id)
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY,
    event_id INTEGER REFERENCES events(id),
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT,
    summary TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY,
    event_id INTEGER REFERENCES events(id),
    at TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    duration_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS actions_event ON actions(event_id);
CREATE INDEX IF NOT EXISTS actions_at ON actions(at);
CREATE INDEX IF NOT EXISTS llm_at ON llm_calls(at);
"""


def to_db(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("время без часового пояса")
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def from_db(raw: str | None, tz) -> datetime | None:
    return None if raw is None else datetime.fromisoformat(raw).astimezone(tz)


class Journal:
    def __init__(self, path: str | Path, clock: Clock):
        self.clock = clock
        self.conn = connect(path)  # общий замок: веб, бот и роботы в разных потоках
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    @property
    def tz(self):
        return self.clock.now().tzinfo

    def close(self) -> None:
        self.conn.close()

    # --- события -----------------------------------------------------------
    def record_event(self, channel: str, external_id: str, payload: dict) -> tuple[int, bool]:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO events (channel, external_id, received_at, payload) VALUES (?, ?, ?, ?)",
            (channel, str(external_id), to_db(self.clock.now()), json.dumps(payload, ensure_ascii=False)))
        new = cur.rowcount == 1
        row = self.conn.execute("SELECT id FROM events WHERE channel = ? AND external_id = ?",
                                (channel, str(external_id))).fetchone()
        self.conn.commit()
        return row["id"], new

    def event(self, event_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        d["received_at"] = from_db(d["received_at"], self.tz)
        return d

    # --- действия ----------------------------------------------------------
    def log_action(self, kind: str, *, event_id: int | None = None, actor: str = "assistant",
                   path: str | None = None, summary: str = "", payload: dict | None = None) -> int:
        if actor not in ACTORS:
            raise ValueError(f"неизвестный исполнитель: {actor}")
        cur = self.conn.execute(
            "INSERT INTO actions (event_id, at, actor, kind, path, summary, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, to_db(self.clock.now()), actor, kind, path, summary,
             json.dumps(payload or {}, ensure_ascii=False)))
        self.conn.commit()
        return cur.lastrowid

    def actions(self, event_id: int | None = None) -> list[dict]:
        if event_id is None:
            rows = self.conn.execute("SELECT * FROM actions ORDER BY id").fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM actions WHERE event_id = ? ORDER BY id", (event_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["at"] = from_db(d["at"], self.tz)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    # --- вызовы модели -----------------------------------------------------
    def log_llm_call(self, provider: str, model: str, input_tokens: int, output_tokens: int,
                     cost_usd: float, duration_ms: int, event_id: int | None = None) -> int:
        if min(input_tokens, output_tokens, duration_ms) < 0 or cost_usd < 0:
            raise ValueError("токены, длительность и стоимость не могут быть отрицательными")
        cur = self.conn.execute(
            "INSERT INTO llm_calls (event_id, at, provider, model, input_tokens, output_tokens, cost_usd, duration_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, to_db(self.clock.now()), provider, model, input_tokens, output_tokens, cost_usd, duration_ms))
        self.conn.commit()
        return cur.lastrowid

    def cost(self, since: datetime | None = None) -> float:
        if since is None:
            row = self.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM llm_calls").fetchone()
        else:
            row = self.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM llm_calls WHERE at >= ?",
                                    (to_db(since),)).fetchone()
        return float(row["s"])

    # --- сводка ------------------------------------------------------------
    def day_summary(self, day: date) -> dict:
        start = datetime.combine(day, time(0), tzinfo=self.tz)
        a, b = to_db(start), to_db(start + timedelta(days=1))
        q = self.conn.execute
        events = q("SELECT COUNT(*) AS n FROM events WHERE received_at >= ? AND received_at < ?", (a, b)).fetchone()["n"]
        by_path = {r["path"]: r["n"] for r in q(
            "SELECT path, COUNT(*) AS n FROM actions WHERE at >= ? AND at < ? AND path IS NOT NULL GROUP BY path",
            (a, b))}
        human = q("SELECT COUNT(*) AS n FROM actions WHERE at >= ? AND at < ? AND actor = 'human'", (a, b)).fetchone()["n"]
        cost = q("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM llm_calls WHERE at >= ? AND at < ?", (a, b)).fetchone()["s"]
        return {"events": events, "by_path": by_path, "human_actions": human, "cost_usd": float(cost)}
