"""Метрики для питча: освобождённые часы, доля писем без руководителя, время от встречи до протокола, стоимость."""
from __future__ import annotations

from yadro.journal import Journal


def compute_metrics(j: Journal, protocols=None) -> dict:
    acts = j.actions()
    mail = [a for a in acts if a["kind"].startswith("mail_")]
    without = [a for a in mail if a["kind"] != "mail_спросить_руководителя"]
    saved_min = sum(a["payload"].get("minutes_saved", 0) for a in acts)
    drafts = [a["payload"]["minutes_to_draft"] for a in acts if a["kind"] == "protocol_draft"]
    approved = [a["payload"]["minutes_to_approved"] for a in acts
                if a["kind"] == "protocol_sent" and a["payload"].get("minutes_to_approved") is not None]
    edits = sum(1 for a in acts if a["kind"] == "protocol_edit")
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
