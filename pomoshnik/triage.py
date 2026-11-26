"""Разбор письма: модель определяет категорию и суть, код решает действие по правилам профиля."""
from __future__ import annotations

from dataclasses import dataclass

from .org import BOSS
from .profile import CATEGORIES, Profile

MIN_CONFIDENCE = 0.6

CLASSIFY_SYSTEM = """Ты помощник руководителя и разбираешь входящее письмо.
Верни JSON: {"category": "<одна категория из списка>", "summary": "<суть в одной строке до 120 знаков>", "confidence": <0..1>}
Категории:
деньги — бюджеты, скидки, закупки, перераспределение денег;
кадры — найм, офферы, отпуска, увольнения, оценка сотрудников;
обязательства_клиентам — клиент просит сроки, условия, гарантии;
руководство — письмо от вышестоящего руководителя или для него;
календарь — перенос, подтверждение внутренних встреч;
рассылка — новости, дайджесты, маркетинговые рассылки;
приглашение — внешние мероприятия, выступления;
отчёт — регулярные отчёты команды;
статус — статус задачи или проекта;
продажи — клиент запрашивает КП или цены;
клиенты — жалобы и обращения клиентов, переданные командой;
доступы — доступы к системам;
спам — мошенничество, выигрыши, «срочно подтвердите счёт», продажа баз, сомнительные инвестиции.
Если не уверен — ставь confidence ниже 0.6."""


@dataclass(frozen=True)
class Decision:
    action: str
    category: str
    reason: str
    delegate_to: str | None = None
    summary: str = ""
    confidence: float = 1.0
    sender_role: str = ""


def classify(llm, subject: str, body: str, from_name: str) -> tuple[str, str, float]:
    data = llm.complete_json(CLASSIFY_SYSTEM, f"От: {from_name}\nТема: {subject}\n\n{body[:4000]}")
    cat = str(data.get("category", "")).strip()
    try:
        conf = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    if cat not in CATEGORIES:
        return "", str(data.get("summary", ""))[:200], 0.0
    return cat, str(data.get("summary", ""))[:200], conf


def decide(profile: Profile, category: str, from_email: str, *, summary: str = "", confidence: float = 1.0) -> Decision:
    sender = profile.org.find_by_email(from_email) if profile.org else None
    role = sender.role if sender else "внешний"
    if category == "спам" and not sender:      # спам игнорируем при любых правилах профиля
        return Decision("игнорировать", category, "спам — руководителю не показываем", summary=summary,
                        confidence=confidence, sender_role=role)
    if sender and sender.role == BOSS:
        return Decision("спросить_руководителя", category, f"письмо от руководителя руководителя ({sender.name})",
                        summary=summary, confidence=confidence, sender_role=role)
    if not category:
        return Decision("спросить_руководителя", "", "категорию определить не удалось", summary=summary,
                        confidence=confidence, sender_role=role)
    if confidence < MIN_CONFIDENCE:
        return Decision("спросить_руководителя", category, f"низкая уверенность разбора: {confidence:.2f}",
                        summary=summary, confidence=confidence, sender_role=role)
    rule = profile.action_for(category)
    if rule.action == "делегировать" and not rule.delegate_to:
        return Decision("спросить_руководителя", category, "некому делегировать по оргструктуре",
                        summary=summary, confidence=confidence, sender_role=role)
    reason = rule.source or rule.action
    return Decision(rule.action, category, reason, rule.delegate_to, summary, confidence, role)


def triage(profile: Profile, llm, *, subject: str, body: str, from_name: str, from_email: str) -> Decision:
    cat, summary, conf = classify(llm, subject, body, from_name)
    return decide(profile, cat, from_email, summary=summary, confidence=conf)
