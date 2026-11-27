"""Срок из русского текста: «к пятнице», «через 2 рабочих дня», «28.11 в 10:00».

Модель только находит слова, дату считает этот код.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable

END_OF_DAY = time(18, 0)

_NUM_WORDS = {
    "один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
}
_WEEKDAYS = [
    r"понедельник\w*", r"вторник\w*", r"сред[аыуе]", r"четверг\w*",
    r"пятниц\w*", r"суббот\w*", r"воскресень\w*",
]
_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря"]

_RE_RELATIVE = re.compile(r"\b(послезавтра|завтра|сегодня)\b")
_RE_WEEKDAY = re.compile(
    r"\b(?:к|ко|до|в|во|на)\s+(?:(следующ\w*)\s+)?(" + "|".join(f"(?:{w})" for w in _WEEKDAYS) + r")\b")
_RE_IN = re.compile(
    r"\bчерез\s+(?:(\d+|" + "|".join(_NUM_WORDS) + r")\s+)?(рабоч\w+\s+)?(дн\w*|день|недел\w*|час\w*)\b")
_RE_END = re.compile(r"\bдо\s+конца\s+(дня|недели|месяца)\b")
_RE_NUMERIC = re.compile(r"(?<![\w.])(\d{1,2})\.(\d{1,2})(?:\.(\d{4}|\d{2}))?(?!\w)(?!\.\d)")
# «12-го декабря», «12-е декабря» — так пишет распознавание речи
_RE_TEXTUAL = re.compile(r"\b(\d{1,2})(?:[\s-]?(?:го|е|ое|ому|ого))?\s+(" + "|".join(_MONTHS) + r")(?:\s+(\d{4}))?\b")
_RE_TIME = re.compile(r"(?<![\w:])(?:(?:к|до|в|во|на)\s+)?([01]?\d|2[0-3]):([0-5]\d)(?![\w:])")

_TIME_GAP = 12  # сколько символов может разделять дату и время, чтобы считать их одним сроком


@dataclass(frozen=True)
class Deadline:
    due: datetime
    matched: str


def is_working_day(d: date, holidays: Iterable[date] = ()) -> bool:
    return d.weekday() < 5 and d not in set(holidays)


def add_working_days(start: date, n: int, holidays: Iterable[date] = ()) -> date:
    hol = set(holidays)
    d, left = start, n
    while left > 0:
        d += timedelta(days=1)
        if is_working_day(d, hol):
            left -= 1
    return d


def default_deadline(now: datetime, working_days: int = 3, holidays: Iterable[date] = (),
                     end_of_day: time = END_OF_DAY) -> datetime:
    return _at(add_working_days(now.date(), working_days, holidays), end_of_day, now)


def parse_deadline(text: str, now: datetime, holidays: Iterable[date] = (),
                   end_of_day: time = END_OF_DAY) -> Deadline | None:
    if not text:
        return None
    low = text.lower()
    today = now.date()
    hol = set(holidays)
    found: list[tuple[int, int, object]] = []   # (начало, конец, date или datetime)

    for m in _RE_RELATIVE.finditer(low):
        shift = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[m.group(1)]
        found.append((m.start(), m.end(), today + timedelta(days=shift)))

    for m in _RE_WEEKDAY.finditer(low):
        wd = next(i for i, pat in enumerate(_WEEKDAYS) if re.fullmatch(pat, m.group(2)))
        delta = (wd - today.weekday()) % 7
        if m.group(1) and today.weekday() + delta <= 6:   # «следующая» — не на этой неделе
            delta += 7
        found.append((m.start(), m.end(), today + timedelta(days=delta)))

    for m in _RE_IN.finditer(low):
        raw, working, unit = m.group(1), m.group(2), m.group(3)
        n = 1 if raw is None else (int(raw) if raw.isdigit() else _NUM_WORDS[raw])
        if unit.startswith("час"):
            found.append((m.start(), m.end(), now + timedelta(hours=n)))
        elif unit.startswith("недел"):
            found.append((m.start(), m.end(), today + timedelta(weeks=n)))
        elif working:
            found.append((m.start(), m.end(), add_working_days(today, n, hol)))
        else:
            found.append((m.start(), m.end(), today + timedelta(days=n)))

    for m in _RE_END.finditer(low):
        what = m.group(1)
        if what == "дня":
            d = today
        elif what == "недели":
            d = today + timedelta(days=4 - today.weekday()) if today.weekday() <= 4 else today
        else:
            d = today.replace(day=calendar.monthrange(today.year, today.month)[1])
        found.append((m.start(), m.end(), d))

    for m in _RE_NUMERIC.finditer(low):
        d = _make_date(int(m.group(1)), int(m.group(2)), m.group(3), today)
        if d:
            found.append((m.start(), m.end(), d))

    for m in _RE_TEXTUAL.finditer(low):
        d = _make_date(int(m.group(1)), _MONTHS.index(m.group(2)) + 1, m.group(3), today)
        if d:
            found.append((m.start(), m.end(), d))

    times = [(m.start(), m.end(), time(int(m.group(1)), int(m.group(2)))) for m in _RE_TIME.finditer(low)]

    if not found:
        if not times:
            return None
        s, e, t = times[0]
        due = _at(today, t, now)
        if due <= now:
            due += timedelta(days=1)
        return Deadline(due, text[s:e])

    s, e, value = min(found, key=lambda f: (f[0], -(f[1] - f[0])))
    if isinstance(value, datetime):
        return Deadline(value, text[s:e])

    t = end_of_day
    for ts, te, tv in times:
        if 0 <= ts - e <= _TIME_GAP or 0 <= s - te <= _TIME_GAP:
            t, s, e = tv, min(s, ts), max(e, te)
            break
    return Deadline(_at(value, t, now), text[s:e])


def _make_date(day: int, month: int, year_raw: str | None, today: date) -> date | None:
    try:
        if year_raw:
            year = int(year_raw)
            if year < 100:
                year += 2000
            return date(year, month, day)
        d = date(today.year, month, day)
        if d < today:
            d = date(today.year + 1, month, day)
        return d
    except ValueError:
        return None


def _at(d: date, t: time, now: datetime) -> datetime:
    return datetime.combine(d, t, tzinfo=now.tzinfo)
