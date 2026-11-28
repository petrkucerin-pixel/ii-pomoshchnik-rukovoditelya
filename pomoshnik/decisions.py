"""Очередь «ждут только вас»: один вопрос вместо копий письма и карточка с готовым решением.

Модель пишет черновик ответа, совет и факты. Код оставляет только факты со ссылкой на выданный источник,
снимает совет без фактов и отмечает цифры черновика, которых нет ни в письме, ни в источниках.
"""
from __future__ import annotations

import re
from datetime import date

ADVICE = ("согласовать", "отказать", "нет совета")
PREP_VERSION = 6          # подготовка старой версии не показывается и готовится заново

PREP_SYSTEM = """Ты помощник руководителя. По письму нужно решение руководителя, твоя задача — подготовить его так,
чтобы руководитель решил одной кнопкой.
Верни JSON: {"ask": "<одна строка: кто просит, для кого или о чём, что именно, даты и суммы, условия — только из письма.
Пример: «Мария Лебедева просит согласовать отпуск Ирины Т. 20–30 декабря, замену нашли»>",
"blockers": [{"source": "<id источника>", "note": "<что именно мешает или рискует, до 15 слов>"}],
"draft_yes":"<письмо, которое уйдёт, если руководитель нажмёт «Согласовать»: он согласен с просьбой
отправителя, подтверждает то, что просят, или выполняет поручение. Это согласие, а не отказ и не отсрочка>",
"draft_no": "<письмо, которое уйдёт, если руководитель нажмёт «Отказать»: вежливый отказ или «пока не можем»,
по возможности с причиной из источников и следующим шагом>",
Оба письма — 2–4 предложения от первого лица руководителя, без подписи.
"advice": "<согласовать | отказать | нет совета>", "why": "<одна строка: почему такой совет>",
"facts": [{"source": "<id источника, например S2>", "note": "<что в нём говорит в пользу решения, до 12 слов>"}]}
Правила:
- blockers — конкретные препятствия из задач, встреч или памяти: пересечение сроков, задача того же человека или
  команды на эти даты, встреча, которую заденет решение, незакрытый вопрос по той же сделке. Нет таких — [].
- facts — доводы «за» из задач, встреч или памяти, которые прямо касаются этого письма: тот же клиент, сделка,
  человек, задача или срок. Нет таких — [].
- Цели квартала и должность отправителя — не доводы и не препятствия, их не включай.
  Никогда не включай источник, чтобы объяснить, что он не относится к делу.
- Факты — только id из списка источников. Не придумывай цифр, дат, сроков, имён и обещаний, которых нет в письме
  или источниках. Новых сроков от себя не ставь.
- Внешнему отправителю (клиент, партнёр, организаторы) не пересказывай внутренние задачи, цели и встречи.
- Руководителю руководителя не поручай работу и не проси у него отчёта: отвечай ему как подчинённый.
- Если отправитель задаёт вопрос, а фактов для ответа нет: в draft_yes — подтверждение того, о чём спрашивают
  (руководитель решит сам, может ли он это подтвердить), в draft_no — честное «подтвердить пока не можем, вернусь
  с ответом» без даты.
- Совет давай, только если его прямо поддерживает задача, встреча или запись памяти (довод или препятствие).
  Иначе «нет совета»."""

ADVICE_SUPPORT = ("задача", "встреча", "память")    # в карточке показываем только такие источники
# Модель иногда перечисляет источник и сама пишет, что он к делу не относится, — такой факт не показываем.
_UNRELATED = re.compile(r"не относ|не релевант|не связан|не касает|не имеет отношени|не про\b|другая инициатив"
                        r"|другая тема|не по теме|не влияет")

_STOP = {"прошу", "нужно", "нужен", "нужна", "пожал", "подтв", "согла", "колле", "котор", "будет", "может", "также",
         "через", "после", "важно", "иван,"}
_NAME = re.compile(r"\b([А-ЯЁ][а-яё]{2,})\s+([А-ЯЁ])(?:\.|[а-яё]+)")
_QUOTED = re.compile(r"«([^»]{2,60})»")
_NUM = re.compile(r"\d+(?:[.,]\d+)*")


def _norm(s: str) -> str:
    return (s or "").lower().replace("ё", "е").strip()


def group_key(email: str, subject: str) -> str:
    subj = re.sub(r"^((re|fwd?|ответ|пересл)\s*:\s*)+", "", _norm(subject))
    return f"{_norm(email)}|{' '.join(subj.split())}"


def group(decisions: list[dict]) -> list[dict]:
    """Письма с одним вопросом от одного отправителя — одна карточка. Порядок — по первому письму,
    ответ уходит на последнее."""
    out: dict[str, dict] = {}
    for d in decisions:
        k = group_key(d.get("email", ""), d.get("subject", ""))
        g = out.get(k)
        if g is None:
            out[k] = g = {**d, "key": k, "event_ids": [], "count": 0}
        g["event_ids"].append(d["event_id"])
        g["count"] += 1
        g["at"], g["id"] = d.get("at", g.get("at")), d.get("id", g.get("id"))
    return list(out.values())


def names(text: str, exclude=()) -> set[str]:
    """Упоминания людей: «Сергей К.» и «Сергея К.» дают одно и то же. Руководителя не считаем."""
    skip = {w[:4].lower() for n in exclude for w in n.split()[:1]}
    return {f"{first[:4].lower()} {initial.lower()}" for first, initial in _NAME.findall(text or "")
            if first[:4].lower() not in skip}


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"\w+", _norm(text)) if len(w) >= 5 and w[:5] not in _STOP}


def _ranked(items, text_of, stems: set[str], top: int):
    scored = [(len(_stems(text_of(x)) & stems), i, x) for i, x in enumerate(items)]
    return [x for n, _, x in sorted(scored, key=lambda t: (-t[0], t[1])) if n][:top]


def sources(letter: str, *, goals=(), tasks=(), meetings=(), memory=None, org=None, sender_email="",
            today: date | None = None, exclude=()) -> list[dict]:
    """Источники для решения: цели, задачи, встречи, память помощника и кто такой отправитель. У каждого свой id.

    `exclude` — письма самого вопроса (все копии): их факты в памяти — это сам вопрос, а не довод."""
    st = _stems(letter)
    out: list[tuple[str, str]] = []
    sender = org.find_by_email(sender_email) if org and sender_email else None
    if sender:
        out.append(("оргструктура", f"{sender.name} — {sender.position} ({sender.role})"))   # кто пишет — для модели
    picked = _ranked(list(tasks), lambda t: t.task, st, 3)
    if sender:                                     # задачи самого отправителя: на них решение может повлиять
        picked += [t for t in tasks if t.owner == sender.name and t not in picked][:3]
    for t in picked:
        out.append(("задача", f"{t.task} — {t.owner}, срок {t.due:%d.%m}, {t.status}"))
    upcoming = [m for m in meetings if today is None or m.start.date() >= today]
    for m in _ranked(upcoming, lambda m: m.title, st, 2):
        out.append(("встреча", f"{m.title} — {m.start:%d.%m %H:%M}"))
    if memory is not None:
        seen, skip = set(), set(exclude)
        for q in _QUOTED.findall(letter) or [letter.split("\n", 1)[0]]:
            hits = [f for f in (memory.history(q) or memory.search(q)) if f.source not in skip]
            for f in hits[-4:]:
                key = " ".join(re.findall(r"\w+", _norm(f.quote)))
                if key in seen:                  # одна и та же цитата из копий письма — один факт
                    continue
                seen.add(key)
                out.append(("память", f"{f.when:%d.%m} {f.who} — {f.topic}: {f.what} («{f.quote}», {f.source})"))
    return [{"id": f"S{i}", "kind": k, "text": t} for i, (k, t) in enumerate(out, 1)]


def prompt(g: dict, letter: str, srcs: list[dict], style: str, leader: str = "") -> str:
    who = next((s["text"] for s in srcs if s["kind"] == "оргструктура"), "внешний отправитель")
    lines = [f"Письмо (пришло {g['count']} раз): от {g.get('from', '')} — {who}", f"Тема: {g.get('subject', '')}", "",
             letter, "", f"Почему письмо у руководителя: {g.get('reason', '')}", "", "Источники:"]
    lines += [f"{s['id']} [{s['kind']}] {s['text']}" for s in srcs] or ["нет"]
    if leader:
        lines += ["", f"Черновик пишешь от имени руководителя: {leader}. Его задачи в источниках — его собственные, "
                      "о нём не пиши в третьем лице."]
    if style:
        lines += ["", f"Стиль руководителя: {style}"]
    return "\n".join(lines)


def check(raw: dict, letter: str, srcs: list[dict], count: int = 1) -> dict:
    """Проверка ответа модели кодом: факт — дословно выданный источник, совет — только с опорой на задачу, встречу
    или память, цифры черновика сверены с письмом и источниками."""
    by_id = {s["id"]: s for s in srcs}

    def items(key: str) -> list[dict]:
        """Только задачи, встречи и память, только выданные источники, без «к делу не относится»."""
        out, seen = [], set()
        for f in raw.get(key) or []:
            sid = str(f.get("source", "")).strip() if isinstance(f, dict) else ""
            if sid not in by_id or sid in seen or by_id[sid]["kind"] not in ADVICE_SUPPORT:
                continue
            note = str(f.get("note") or f.get("text") or "").strip()[:200]
            if _UNRELATED.search(note.lower()):
                continue
            seen.add(sid)
            s = by_id[sid]
            out.append({"text": s["text"], "note": note, "source": sid, "kind": s["kind"], "source_text": s["text"]})
        return out[:5]

    blockers = items("blockers")
    facts = [f for f in items("facts") if f["source"] not in {b["source"] for b in blockers}]   # одно и то же не «за» и «против»
    advice = str(raw.get("advice", "")).strip().lower()
    if advice not in ADVICE or not (facts or blockers):
        advice = "нет совета"
    ask = str(raw.get("ask") or "").strip()[:300]
    yes = str(raw.get("draft_yes") or raw.get("draft") or "").strip()[:1500]
    no = str(raw.get("draft_no") or "").strip()[:1500]
    known = {str(count)}                     # «пишут 4-й раз» — наш счётчик, не выдумка
    for n in _NUM.findall(letter + " " + " ".join(s["text"] for s in srcs)):
        known |= {n, *re.split(r"[.,]", n), str(int(n.split(".")[0].split(",")[0]))}   # «16.12» → и «16 декабря»
    both = f"{ask} {yes} {no}"
    unverified = sorted({n for n in _NUM.findall(both) if n not in known}, key=both.find)
    return {"ask": ask, "blockers": blockers, "draft_yes": yes, "draft_no": no, "draft": yes, "advice": advice,
            "why": str(raw.get("why") or "").strip()[:300] if advice != "нет совета" else "",
            "facts": facts, "unverified": unverified}


REPLY_SYSTEM = """Ты помощник руководителя. Напиши ответ на письмо от его имени, чтобы он мог отправить его,
слегка поправив. Верни JSON: {"text": "<ответ: 2–5 предложений от первого лица руководителя, без подписи>"}
Правила: отвечай по сути письма. Факты, цифры, даты и имена бери только из письма и источников, новых сроков и
обещаний не придумывай. Если ответить по существу нечем — поблагодари, скажи, что вернёшься с ответом, без даты.
Внешнему отправителю не пересказывай внутренние задачи и встречи."""


def reply_draft(llm, sender: str, letter: str, srcs: list[dict], style: str = "", leader: str = "",
                rules: str = "") -> dict:
    """Готовый ответ на обычное письмо. Цифры ответа, которых нет в письме и источниках, — на проверку."""
    lines = [f"Письмо от: {sender}", "", letter, "", "Источники:"]
    lines += [f"{s['id']} [{s['kind']}] {s['text']}" for s in srcs] or ["нет"]
    if leader:
        lines += ["", f"Пишешь от имени руководителя: {leader}."]
    if style:
        lines += ["", f"Стиль руководителя: {style}"]
    if rules:
        lines += ["", "Правила руководителя для писем:", rules]
    raw = llm.complete_json(REPLY_SYSTEM, "\n".join(lines))
    text = str((raw or {}).get("text") or "").strip()[:2000] if isinstance(raw, dict) else ""
    known = set()
    for n in _NUM.findall(letter + " " + " ".join(s["text"] for s in srcs)):
        known |= {n, *re.split(r"[.,]", n), str(int(n.split(".")[0].split(",")[0]))}
    return {"text": text, "unverified": sorted({n for n in _NUM.findall(text) if n not in known}, key=text.find)}


def prepare(llm, g: dict, letter: str, srcs: list[dict], style: str = "", leader: str = "") -> dict:
    raw = llm.complete_json(PREP_SYSTEM, prompt(g, letter, srcs, style, leader))
    return check(raw if isinstance(raw, dict) else {}, letter, srcs, g.get("count", 1))
