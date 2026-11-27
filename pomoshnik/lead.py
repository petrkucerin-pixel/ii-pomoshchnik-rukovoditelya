"""Кто ведёт встречу: руководитель сам или его ИИ-аватар.

По умолчанию аватар ведёт встречи категорий из `AVATAR_CATEGORIES` и встречи, назначенные с галочкой «ведёт аватар».
Выбор руководителя в карточке встречи («Я сам» / «Мой ИИ-аватар») хранится в журнале и главнее правила.
"""
from __future__ import annotations

import os


def default_categories() -> set[str]:
    return {c.strip() for c in os.environ.get("AVATAR_CATEGORIES", "операционная").split(",") if c.strip()}


def lead_overrides(journal) -> dict[str, bool]:
    return {x["payload"]["uid"]: bool(x["payload"]["avatar"]) for x in journal.actions() if x["kind"] == "meeting_lead"}


def set_lead(journal, uid: str, avatar: bool) -> None:
    journal.log_action("meeting_lead", actor="human", path="now",
                       summary=f"{uid}: ведёт {'ИИ-аватар' if avatar else 'руководитель'}",
                       payload={"uid": uid, "avatar": avatar})


def avatar_leads(m, overrides: dict[str, bool], categories: set[str]) -> bool:
    if m.uid in overrides:
        return overrides[m.uid]
    return bool(m.avatar) or m.category in categories
