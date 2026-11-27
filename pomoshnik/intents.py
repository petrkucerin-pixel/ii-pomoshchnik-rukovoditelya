"""Что просит руководитель своими словами (голосом или текстом) — TASK_16.

Модель только разбирает просьбу в одно действие (JSON). Выполняет и проверяет код: кому писать — по оргструктуре,
срок — разбором дат, решение по карточке — одной кнопкой подтверждения.
"""
from __future__ import annotations

INTENTS = ("decision", "recall", "assign", "meeting", "protocol_edit", "plan", "unknown")

SYSTEM = """Ты ИИ-помощник руководителя. Разбери просьбу руководителя в одно действие.
Действия:
- decision — ответ на текущий вопрос, который ждёт его решения: verdict "yes" (согласовать), "no" (отказать),
  "later" (отложить). Если руководитель сказал, что ответить или почему, — напиши в "reply" готовый ответ письмом
  от его имени, коротко и вежливо, на основе черновика; иначе "reply": "".
- recall — спрашивает, что известно о теме: "query" — тема.
- assign — поручить или напомнить человеку: "person" — имя как сказал руководитель, "what" — что сделать
  (в неопределённой форме, без имени), "due_text" — срок словами, как сказал, или "".
- meeting — назначить встречу: "title", "date" (ГГГГ-ММ-ДД), "time" (ЧЧ:ММ), "minutes" (по умолчанию 30),
  "people" — имена участников.
- protocol_edit — поправить протокол встречи: "instruction" — правка его словами.
- plan — показать план, график, сроки на сегодня.
- unknown — непонятно, что просят.
Верни только JSON, например: {"intent": "assign", "person": "Ольга", "what": "прислать КП", "due_text": "к пятнице"}"""


def context_text(*, today, card: dict | None, protocol: dict | None, people: list[str]) -> str:
    lines = [f"Сегодня: {today:%Y-%m-%d}, {['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'][today.weekday()]}."]
    if card:
        p = card.get("prepared") or {}
        lines.append(f"Текущий вопрос: {card['from']} — {card['subject']}. {card.get('summary', '')}")
        if p.get("draft_yes") or p.get("draft"):
            lines.append(f"Черновик, если согласует: {p.get('draft_yes') or p.get('draft')}")
        if p.get("draft_no"):
            lines.append(f"Черновик, если откажет: {p['draft_no']}")
    else:
        lines.append("Вопросов, которые ждут решения, сейчас нет.")
    if protocol:
        lines.append(f"Протокол на утверждении: «{protocol['title']}».")
    lines.append("Люди: " + ", ".join(people))
    return "\n".join(lines)


def parse(llm, text: str, context: str) -> dict:
    try:
        d = llm.complete_json(SYSTEM, f"{context}\n\nРуководитель: {text}") or {}
    except Exception:                           # модель недоступна — честно говорим, что не поняли
        return {"intent": "unknown"}
    if not isinstance(d, dict) or d.get("intent") not in INTENTS:
        return {"intent": "unknown"}
    return d
