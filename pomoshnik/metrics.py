"""Метрики для питча: освобождённые часы, доля писем без руководителя, время от встречи до протокола, стоимость."""
from __future__ import annotations

from yadro.journal import Journal

# Доли для круговых диаграмм «Профиль → Метрики». Порядок фиксирован: цвет закреплён за долей, а не за размером.
ACTIONS = [("делегировать", "Передал ответственному"), ("спросить_руководителя", "Пришло к вам"),
           ("авто", "Ответил сам"), ("в_сводку", "Прочитал, в сводку"), ("игнорировать", "Пропустил")]
HOURS_GOAL = 8                       # ТЗ: освободить 8–12 часов в неделю; нижняя граница — цель для кольца


def _activity(kind: str) -> str:
    if kind.startswith("mail_"):
        return "Почта"
    if kind.startswith("protocol"):
        return "Протоколы встреч"
    if kind.startswith("task") or kind == "report_review":
        return "Задачи и сроки"
    return "Прочее"


def compute_metrics(j: Journal, protocols=None) -> dict:
    acts = j.actions()
    last: dict = {}                  # входящие, не исходящие; после пересмотра очереди считаем последний разбор письма
    for a in acts:
        if a["kind"].startswith("mail_") and a["kind"] != "mail_sent":
            last[a["event_id"] if a["event_id"] is not None else ("action", a["id"])] = a
    mail = list(last.values())
    without = [a for a in mail if a["kind"] != "mail_спросить_руководителя"]
    saved_min = sum(a["payload"].get("minutes_saved", 0) for a in acts)
    # Отрицательное время бывает, когда встреча из учебного календаря «в будущем» относительно часов стенда — не считаем.
    drafts = [a["payload"]["minutes_to_draft"] for a in acts
              if a["kind"] == "protocol_draft" and a["payload"]["minutes_to_draft"] >= 0]
    approved = [a["payload"]["minutes_to_approved"] for a in acts
                if a["kind"] == "protocol_sent" and a["payload"].get("minutes_to_approved") is not None]
    edits = sum(1 for a in acts if a["kind"] in ("protocol_edit", "decision_draft_edit"))
    by_action = {k: 0 for k, _ in ACTIONS}
    for a in mail:
        k = a["kind"][5:]
        if k in by_action:
            by_action[k] += 1
    by_activity = {"Почта": 0, "Протоколы встреч": 0, "Задачи и сроки": 0, "Прочее": 0}
    for a in acts:
        m = a["payload"].get("minutes_saved", 0)
        if m:
            by_activity[_activity(a["kind"])] += m
    cost = j.cost()
    res = {
        "letters": len(mail),
        "letters_without_leader": len(without),
        "share_without_leader": round(len(without) / len(mail), 3) if mail else None,
        "hours_saved": round(saved_min / 60, 2),
        "protocol_minutes_to_draft": round(sum(drafts) / len(drafts), 1) if drafts else None,
        "protocol_minutes_to_approved": round(sum(approved) / len(approved), 1) if approved else None,
        "leader_edits": edits,
        "llm_cost_usd": round(cost, 4),
        "letters_by_action": [{"key": k, "label": label, "value": by_action[k]} for k, label in ACTIONS],
        "hours_by_activity": [{"label": k, "value": round(v / 60, 2)} for k, v in by_activity.items()],
        "hours_goal": HOURS_GOAL,
    }
    res["cards"] = {
        "писем разобрано": res["letters"],
        "без руководителя": f"{round(100 * res['share_without_leader'])} %" if mail else "—",
        "часов освобождено": res["hours_saved"],
        "встреча → протокол, мин": res["protocol_minutes_to_draft"] if drafts else "—",
        "правок руководителя": edits,
        "расходы на модель, $": res["llm_cost_usd"],
    }
    return res
