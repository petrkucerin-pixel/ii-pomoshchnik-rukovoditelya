"""SQLite для нескольких потоков: веб, Telegram-бот и роботы встреч пишут в одну базу.

Одно соединение на базу, каждый запрос под общим замком; строки забираем сразу, пока замок взят.
Без этого курсор одного потока читает результат чужого запроса (у нас ловилось как payload = None).
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class Result:
    """Результат запроса: строки уже прочитаны, поэтому курсор можно отпустить."""

    def __init__(self, cur: sqlite3.Cursor):
        self.rows = cur.fetchall() if cur.description else []
        self.rowcount, self.lastrowid = cur.rowcount, cur.lastrowid
        self._i = 0

    def fetchone(self):
        if self._i >= len(self.rows):
            return None
        self._i += 1
        return self.rows[self._i - 1]

    def fetchall(self) -> list:
        rest, self._i = self.rows[self._i:], len(self.rows)
        return rest

    def __iter__(self):
        return iter(self.fetchall())


class SafeConnection:
    def __init__(self, path: str | Path):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()

    def execute(self, sql: str, params=()) -> Result:
        with self.lock:
            return Result(self._conn.execute(sql, params))

    def executescript(self, sql: str) -> None:
        with self.lock:
            self._conn.executescript(sql)

    def commit(self) -> None:
        with self.lock:
            self._conn.commit()

    def close(self) -> None:
        with self.lock:
            self._conn.close()


def connect(path: str | Path) -> SafeConnection:
    return SafeConnection(path)
