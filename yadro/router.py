"""Выбор пути для предложения агента: сразу, окно отмены, решение человека или на правку.

Правило в коде, а не в модели: модель описывает предложение, код решает, как оно уйдёт.
"""
from __future__ import annotations

from dataclasses import dataclass, field

AUDIENCES = {"internal", "external"}


@dataclass(frozen=True)
class Proposal:
    audience: str                     # internal — своя команда и смежники, external — партнёры и клиенты
    recipient_known: bool             # адресат есть в справочнике или в переписке
    quotes_ok: bool                   # все цитаты-основания нашлись в источниках
    commitments: tuple[str, ...] = () # обещания сверх правил: деньги, скидки, сроки, которых нет в источниках
    needs_decision: bool = False      # агент сам отметил, что нужно решение человека
    confidence: float = 1.0
    attempt: int = 1                  # номер попытки после возврата на правку


@dataclass(frozen=True)
class Policy:
    min_confidence: float = 0.6
    auto_external: bool = False       # доверие выросло: внешние письма без окна отмены
    max_revise_attempts: int = 1


@dataclass(frozen=True)
class Route:
    path: str                         # now | undo | decision | revise
    reasons: list[str] = field(default_factory=list)


def route(p: Proposal, policy: Policy = Policy()) -> Route:
    if p.audience not in AUDIENCES:
        raise ValueError(f"неизвестная аудитория: {p.audience}")

    decision: list[str] = []
    if p.needs_decision:
        decision.append("нужно решение руководителя")
    if not p.recipient_known:
        decision.append("получатель не из справочника и не из переписки")
    decision += [f"обещание сверх правил: {c}" for c in p.commitments]
    if p.confidence < policy.min_confidence:
        decision.append(f"низкая уверенность агента: {p.confidence:.2f}")

    revise: list[str] = []
    if not p.quotes_ok:
        if p.attempt > policy.max_revise_attempts:
            decision.append("цитаты не сошлись с источником и после правки")
        else:
            revise.append("цитаты не сошлись с источником")

    if decision:
        return Route("decision", decision + revise)
    if revise:
        return Route("revise", revise)
    if p.audience == "external" and not policy.auto_external:
        return Route("undo")
    return Route("now")
