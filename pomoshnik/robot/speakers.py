"""Кто говорит на встрече (TASK_12). Телемост сам определяет голос участника и присылает по WebSocket
`slotsConfig` с флагом `vad` и `participantId`; имя участника — в `upsertDescription`. Робот записывает
ленту «кто когда говорил», а после встречи подписывает реплики расшифровки по этой ленте.

Подпись в Телемосте («Пит», «Оля С.») не всегда совпадает с оргструктурой. Когда человек представился
(«Это Иван Петров»), помощник запоминает связку и использует её на следующих встречах.
"""
from __future__ import annotations

import json
import re

PRE_S = 1.5          # реплика в расшифровке начинается чуть позже, чем человек открыл рот
DEFAULT_S = 10.0     # окно последней реплики


def _find_pid(obj) -> str | None:
    if isinstance(obj, dict):
        if isinstance(obj.get("participantId"), str):
            return obj["participantId"]
        for v in obj.values():
            pid = _find_pid(v)
            if pid:
                return pid
    elif isinstance(obj, list):
        for v in obj:
            pid = _find_pid(v)
            if pid:
                return pid
    return None


def parse_signal(raw: str, state: dict, t: float) -> list[tuple[float, str, bool]]:
    """Одно сообщение Телемоста → изменения «говорит / молчит». state: {"people": {id: имя}, "vad": {id: bool}}."""
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(msg, dict):
        return []
    for d in (msg.get("upsertDescription") or {}).get("description") or []:
        name = ((d or {}).get("meta") or {}).get("name")
        if d.get("id") and name:
            state["people"][d["id"]] = name
    events = []
    for slot in (msg.get("slotsConfig") or {}).get("slots") or []:
        pid = _find_pid(slot)
        if not pid:
            continue
        vad = bool(slot.get("vad"))
        if state["vad"].get(pid, False) != vad:
            state["vad"][pid] = vad
            events.append((t, pid, vad))
    return events


def intervals(events, end: float) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list] = {}
    open_: dict[str, float] = {}
    for t, pid, on in sorted(events):
        if on and pid not in open_:
            open_[pid] = t
        elif not on and pid in open_:
            out.setdefault(pid, []).append((open_.pop(pid), t))
    for pid, t in open_.items():
        out.setdefault(pid, []).append((t, end))
    return out


def label_entries(entries: list[dict], events, t0_ms: float, people: dict, *, robot_name: str,
                  aliases: dict | None = None, solo: str | None = None) -> list[dict]:
    """Реплики LiveAvatar (relative_timestamp от начала сессии) + лента vad → у реплик участников поле speaker.

    solo — единственный человек во встрече кроме аватара: все реплики участников его, даже без имени в сигналах."""
    aliases = aliases or {}
    order = sorted(entries, key=lambda e: e.get("relative_timestamp", 0))
    starts = [t0_ms + (e.get("relative_timestamp", 0) - PRE_S) * 1000 for e in order]
    end_all = (max(starts) if starts else t0_ms) + DEFAULT_S * 1000
    spans = {pid: iv for pid, iv in intervals(events, end_all).items() if people.get(pid) != robot_name}
    out = []
    for i, e in enumerate(order):
        e = dict(e)
        if e.get("role") != "avatar" and solo:
            e["speaker_raw"] = solo
            e["speaker"] = aliases.get(solo, solo)
        elif e.get("role") != "avatar":
            a = starts[i]
            b = starts[i + 1] if i + 1 < len(order) else a + DEFAULT_S * 1000
            best, best_pid = 0.0, None
            for pid, iv in spans.items():
                ov = sum(max(0.0, min(b, y) - max(a, x)) for x, y in iv)
                if ov > best:
                    best, best_pid = ov, pid
            if best_pid:
                raw = people.get(best_pid, "Участник")
                e["speaker_raw"] = raw
                e["speaker"] = aliases.get(raw, raw)
        out.append(e)
    return out


# ---------------- связки «подпись в Телемосте → человек из оргструктуры» ----------------
_INTRO = re.compile(r"(?i:\b(?:это|я|меня\s+зовут))\s+([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)")


def resolve_name(name: str, org) -> str | None:
    if not org or not name:
        return None
    n = name.lower().replace("ё", "е").strip()
    for p in org.people:
        if p.name.lower().replace("ё", "е") == n:
            return p.name
    hits = [p for p in org.people if p.name.lower().replace("ё", "е").split()[0] == n.split()[0]]
    return hits[0].name if len(hits) == 1 else None


def aliases_from_journal(journal) -> dict[str, str]:
    return {x["payload"]["display"]: x["payload"]["person"] for x in journal.actions() if x["kind"] == "person_alias"}


def learn_aliases(journal, labeled: list[dict], org) -> dict[str, str]:
    """Человек представился под своей подписью — запоминаем «подпись → человек». Возвращает новые связки."""
    known = aliases_from_journal(journal)
    new = {}
    for e in labeled:
        raw = e.get("speaker_raw")
        if not raw or raw in known or raw in new or resolve_name(raw, org) == raw:
            continue
        for m in _INTRO.finditer(e.get("transcript") or ""):
            person = resolve_name(m.group(1), org)
            if person:
                new[raw] = person
                journal.log_action("person_alias", path="now", summary=f"«{raw}» в Телемосте — {person}",
                                   payload={"display": raw, "person": person})
                break
    return new
