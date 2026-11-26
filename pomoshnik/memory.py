"""Память помощника: факты из писем, отчётов и протоколов. Руководитель сам ничего не загружает.

Модель извлекает факты, код сверяет цитату с исходным текстом. Факт без подтверждённой цитаты не сохраняется.
Поиск — SQLite FTS5 по началу слова, поэтому «облака» находит «Облако». У каждого руководителя своя база.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from yadro.journal import from_db, to_db
from yadro.quotes import check_quote

KINDS = ("сделка", "партнёр", "метрика", "задача", "клиент", "решение")

SYSTEM = """Ты ведёшь память помощника руководителя. Из текста выпиши факты, которые пригодятся руководителю позже:
статус сделки или партнёра, цифру, срок, договорённость, проблему, решение.
Верни JSON: {"facts": [{"topic": "<о чём: название сделки, партнёра, клиента, метрики или задачи>",
"kind": "<сделка | партнёр | метрика | задача | клиент | решение>", "what": "<суть в одной строке: статус, цифра, срок>",
"who": "<кто отвечает или сообщил, если назван>", "quote": "<дословный фрагмент текста, 3–20 слов>"}]}
Не придумывай фактов, которых нет в тексте. Нет полезных фактов — верни {"facts": []}."""

_WORD = re.compile(r"\w+")
_QUOTES = "«»\"'„“”"


@dataclass(frozen=True)
class Fact:
    topic: str
    kind: str
    what: str
    who: str
    when: datetime
    source: str
    quote: str


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")


def topic_key(topic: str) -> str:
    return " ".join(_WORD.findall(_norm(topic)))


def extract_facts(llm, text: str, *, source: str, who: str, when: datetime, title: str = "") -> list[Fact]:
    data = llm.complete_json(SYSTEM, (f"Тема: {title}\n" if title else "") + f"Автор: {who}\n\n{text[:6000]}")
    raw = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    out = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        topic = str(f.get("topic") or "").strip().strip(_QUOTES).strip()
        quote = str(f.get("quote") or "").strip()
        if not topic or not check_quote(quote, text, min_words=3).found:
            continue
        kind = str(f.get("kind") or "").strip().lower()
        out.append(Fact(topic=topic, kind=kind if kind in KINDS else "прочее", what=str(f.get("what") or "").strip(),
                        who=str(f.get("who") or "").strip() or who, when=when, source=source, quote=quote))
    return out


_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    topic TEXT NOT NULL, topic_key TEXT NOT NULL, kind TEXT NOT NULL, what TEXT NOT NULL, who TEXT NOT NULL,
    at TEXT NOT NULL, source TEXT NOT NULL, quote TEXT NOT NULL, saved_at TEXT NOT NULL,
    UNIQUE (source, topic_key, quote)
);
CREATE INDEX IF NOT EXISTS facts_topic ON facts (topic_key, at);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(body, tokenize = 'unicode61');
"""


class Memory:
    def __init__(self, path: str | Path, clock):
        self.clock = clock
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    def close(self):
        self.conn.close()

    def add(self, facts: list[Fact]) -> int:
        """Сохраняет новые факты, повтор того же факта из того же источника пропускает. Возвращает число новых."""
        n = 0
        for f in facts:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO facts (topic, topic_key, kind, what, who, at, source, quote, saved_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (f.topic, topic_key(f.topic), f.kind, f.what, f.who, to_db(f.when), f.source, f.quote,
                 to_db(self.clock.now())))
            if cur.rowcount:
                body = _norm(" ".join((f.topic, f.what, f.who, f.quote)))
                self.conn.execute("INSERT INTO facts_fts (rowid, body) VALUES (?, ?)", (cur.lastrowid, body))
                n += 1
        self.conn.commit()
        return n

    def _rows(self, where: str, args: tuple, limit: int) -> list[Fact]:
        rows = self.conn.execute(
            f"SELECT * FROM (SELECT * FROM facts WHERE {where} ORDER BY at DESC, id DESC LIMIT ?) ORDER BY at, id",
            (*args, limit)).fetchall()
        tz = self.clock.now().tzinfo
        return [Fact(r["topic"], r["kind"], r["what"], r["who"], from_db(r["at"], tz), r["source"], r["quote"])
                for r in rows]

    def history(self, topic: str, limit: int = 20) -> list[Fact]:
        """История одной темы по времени: последние `limit` фактов."""
        return self._rows("topic_key = ?", (topic_key(topic),), limit)

    def search(self, query: str, limit: int = 20) -> list[Fact]:
        """Поиск по словам: все слова запроса, по началу слова. Если так ничего нет — хотя бы одно слово."""
        words = [w for w in _WORD.findall(_norm(query)) if len(w) >= 2]
        if not words:
            return []
        terms = [f'"{w[:max(4, len(w) - 2)]}"*' for w in words]
        for op in (" AND ", " OR "):
            hits = self._rows("id IN (SELECT rowid FROM facts_fts WHERE facts_fts MATCH ?)", (op.join(terms),), limit)
            if hits:
                return hits
        return []
