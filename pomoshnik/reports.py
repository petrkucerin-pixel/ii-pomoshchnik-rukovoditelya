"""Ревизор отчётов: проверяет отчёты и статусы команды сам и пишет исполнителям, а руководителю — одну строку.

Все проверки в коде, модель не нужна: повтор без движения, расхождение со сроком задачи,
сверка цифр из разных источников, расплывчатые риски, пропущенные отчёты, риск для цели руководителя.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from .org import Org
from .plan import TaskRow

STALE_DAYS = 3


@dataclass(frozen=True)
class Report:
    id: str
    from_name: str
    from_email: str
    received: datetime
    subject: str
    body: str


@dataclass(frozen=True)
class Finding:
    kind: str            # нет_движения | срок_задачи | цифры | нет_деталей | нет_отчёта | риск_цели | сверено
    topic: str
    to_email: str | None  # кому вопрос; None — только в сводку
    to_name: str
    question: str        # письмо исполнителю
    digest: str          # строка в сводку руководителю
    leader: str = ""     # строка руководителю в «ждут только вас»/риски; пусто — не беспокоить
    sources: tuple[str, ...] = ()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower().replace("ё", "е").replace("−", "-")).strip()


def _entities(text: str) -> set[str]:
    return {m.lower() for m in re.findall(r"«([^»]+)»", text)}


def _date_in(text: str, year: int) -> date | None:
    m = re.search(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", text)
    if not m:
        return None
    y = int(m.group(3)) if m.group(3) else year
    if y < 100:
        y += 2000
    try:
        return date(y, int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def plan_percent(text: str) -> float | None:
    """Выполнение плана в процентах: «прогноз 94% от плана» → 94, «отклонение от плана −6%» → 94."""
    t = _norm(text)
    m = re.search(r"отклонени\w* от плана\s*([+-]?\d+(?:[.,]\d+)?)\s*%", t)
    if m:
        return 100 + float(m.group(1).replace(",", "."))
    m = re.search(r"(?:прогноз|выполнени\w*)\D{0,20}?(\d+(?:[.,]\d+)?)\s*%\s*(?:от )?плана", t)
    if m:
        return float(m.group(1).replace(",", "."))
    return None


def review(reports: list[Report], tasks: list[TaskRow], org: Org | None, goals: list[str], today: date) -> list[Finding]:
    out: list[Finding] = []
    person = (lambda e: org.find_by_email(e)) if org else (lambda e: None)

    def email_of_name(name: str) -> str | None:
        if not org:
            return None
        p = next((x for x in org.people if x.name == name), None)
        return org.email_of(p) if p else None

    # 1. нет движения: одинаковый текст от одного человека на разных днях
    groups: dict[tuple[str, str], list[Report]] = defaultdict(list)
    for r in reports:
        groups[(r.from_email.lower(), _norm(r.body))].append(r)
    for (email, _), rs in groups.items():
        days = sorted({r.received.date() for r in rs})
        if len(days) >= STALE_DAYS:
            r0 = rs[0]
            span = (days[-1] - days[0]).days + 1
            first = r0.from_name.split()[0]
            out.append(Finding(
                "нет_движения", r0.subject, email, r0.from_name,
                f"{first}, по «{r0.subject}» статус не меняется {span} дн. ({len(rs)} одинаковых отчётов). "
                f"Что мешает и чем помочь? Если всё по плану — назовите следующий шаг и дату.",
                f"{r0.from_name}: «{r0.subject}» без движения {span} дн. — спросил, что мешает",
                sources=tuple(r.id for r in rs)))

    # 2. срок в отчёте против срока задачи
    for (email, _), rs in groups.items():
        r = rs[-1]
        ents = _entities(r.subject + " " + r.body)
        said = _date_in(r.body, r.received.year)
        for t in tasks:
            if not ents & _entities(t.task) or said is None:
                continue
            if said != t.due:
                late = " Срок задачи уже прошёл." if t.due < today else ""
                out.append(Finding(
                    "срок_задачи", t.task, email, r.from_name,
                    f"{r.from_name.split()[0]}, в задаче «{t.task}» срок {t.due:%d.%m}, а в отчёте — {said:%d.%m}.{late} "
                    f"Обновляю срок на {said:%d.%m}? Если нет — какой срок верный?",
                    f"«{t.task}»: срок в задаче {t.due:%d.%m}, в отчёте {said:%d.%m} — уточняю у автора отчёта ({r.from_name})",
                    sources=(r.id, t.id)))

    # 3. сверка выполнения плана из разных источников
    latest: dict[str, tuple[float, Report]] = {}
    for r in sorted(reports, key=lambda x: x.received):
        v = plan_percent(r.body)
        if v is not None:
            latest[r.from_email.lower()] = (v, r)
    if len(latest) >= 2:
        vals = list(latest.values())
        spread = max(v for v, _ in vals) - min(v for v, _ in vals)
        names = ", ".join(f"{r.from_name} — {v:g} %" for v, r in vals)
        if spread <= 1:
            out.append(Finding("сверено", "выполнение плана", None, "", "",
                               f"Выполнение плана сверено по {len(vals)} источникам: {names} — сходится",
                               sources=tuple(r.id for _, r in vals)))
        else:
            for v, r in vals:
                out.append(Finding(
                    "цифры", "выполнение плана", r.from_email, r.from_name,
                    f"{r.from_name.split()[0]}, у нас расходятся цифры по выполнению плана: {names}. "
                    f"Сверьте, пожалуйста, источник и пришлите верную цифру.",
                    f"Выполнение плана расходится: {names} — попросил сверить", sources=(r.id,)))

    # 4. риск без деталей
    for (email, _), rs in groups.items():
        r = rs[-1]
        if re.search(r"риск\w*\s+по\s+(двум|трем|трём|нескольким|\d+)\s+сделк", _norm(r.body)) and not _entities(r.body):
            out.append(Finding(
                "нет_деталей", r.subject, email, r.from_name,
                f"{r.from_name.split()[0]}, в «{r.subject}» риск по сделкам без названий. "
                f"Какие сделки, суммы и что нужно, чтобы их закрыть?",
                f"{r.from_name}: риск по сделкам без деталей — запросил названия и суммы", sources=(r.id,)))

    # 5. пропущенный отчёт
    for t in tasks:
        if "отчет" not in _norm(t.task) and "отчёт" not in t.task.lower():
            continue
        if not (t.status == "просрочена" or t.due < today):
            continue
        email = email_of_name(t.owner)
        got = any(email and r.from_email.lower() == email.lower() and
                  (_entities(t.task) & _entities(r.subject + r.body) or "отчет" in _norm(r.subject)) for r in reports)
        if not got:
            out.append(Finding(
                "нет_отчёта", t.task, email, t.owner,
                f"{t.owner.split()[0]}, «{t.task}» должен был прийти до {t.due:%d.%m}. Когда пришлёте? Если что-то мешает — напишите, помогу.",
                f"«{t.task}» ({t.owner}) не пришёл — напомнил", sources=(t.id,)))

    # 6. риск для цели руководителя
    wants_plan = any("план продаж" in _norm(g) for g in goals)
    if wants_plan and latest:
        v, r = min(latest.values(), key=lambda x: x[0])
        if v < 100:
            reason = "; ".join(f.question.split(". ")[0] for f in out if f.kind == "нет_деталей")
            out.append(Finding(
                "риск_цели", "план продаж", None, "", "",
                f"Риск по цели «план продаж»: прогноз {v:g} %",
                leader=f"План продаж под риском: прогноз {v:g} % ({r.from_name}). Детали по сделкам уже запросил.",
                sources=(r.id,)))
    return out
