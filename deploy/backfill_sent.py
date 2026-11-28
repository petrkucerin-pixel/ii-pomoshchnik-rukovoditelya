"""Вернуть в журнал письма, которые ушли через заглушку Outlook, но записей о них в журнале нет.

Так бывает, если журнал пересоздавали после первого разбора почты (стенд, 26.11). Скрипт идемпотентный:
письмо с тем же message_id второй раз не добавляется. Время — от разбора входящего, на которое письмо отвечает,
или от вопроса ревизора по той же теме. Перед запуском сделайте бэкап базы.

Запуск: python deploy/backfill_sent.py runtime/ivan_petrov.sqlite runtime/ivan_petrov_outlook_sent.jsonl
"""
from __future__ import annotations

import json
import sqlite3
import sys


def kind_of(subject: str) -> tuple[str, str]:
    if subject.startswith("Fwd:"):
        return "передача", "delegate"
    if subject.startswith("Вопрос по отчёту"):
        return "вопрос_ревизора", "review_question"
    if subject.startswith("Протокол:"):
        return "протокол", "protocol_mail"
    if subject.startswith("Приглашение:"):
        return "приглашение", "invite"
    return "автоответ", "auto_reply"


def main(db: str, outbox: str) -> int:
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    known = {json.loads(r["payload"]).get("message_id")
             for r in c.execute("SELECT payload FROM actions WHERE kind = 'mail_sent'")}
    events = {r["external_id"]: r["id"] for r in c.execute("SELECT id, external_id FROM events WHERE channel = 'mail'")}
    route_at = {}
    for r in c.execute("SELECT event_id, at FROM actions WHERE kind LIKE 'mail_%' AND kind != 'mail_sent' ORDER BY id"):
        route_at.setdefault(r["event_id"], r["at"])
    review_at = {}
    for r in c.execute("SELECT at, payload FROM actions WHERE kind = 'report_review' ORDER BY id"):
        review_at.setdefault(json.loads(r["payload"]).get("topic", ""), r["at"])
    first_at = c.execute("SELECT MIN(at) FROM actions").fetchone()[0]
    added = 0
    for line in open(outbox, encoding="utf-8"):
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec["id"] in known:
            continue
        kind, template = kind_of(rec.get("subject", ""))
        eid = events.get(rec.get("in_reply_to") or "")
        at = route_at.get(eid) if eid else None
        if not at and kind == "вопрос_ревизора":
            at = review_at.get(rec["subject"].removeprefix("Вопрос по отчёту: ").strip())
        c.execute("INSERT INTO actions (event_id, at, actor, kind, path, summary, payload) VALUES (?,?,?,?,?,?,?)",
                  (eid, at or first_at, "assistant", "mail_sent", "now", f"{rec['to']}: {rec['subject']}",
                   json.dumps({"to": rec["to"], "subject": rec["subject"], "body": rec.get("body", ""),
                               "in_reply_to": rec.get("in_reply_to"), "kind": kind, "template": template,
                               "message_id": rec["id"], "restored": True}, ensure_ascii=False)))
        known.add(rec["id"])
        added += 1
    c.commit()
    print(f"добавлено в журнал: {added}")
    return added


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
