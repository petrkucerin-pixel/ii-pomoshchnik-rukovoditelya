"""Часы: настоящие и учебные. Всё время в ядре — с часовым поясом."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

# Екатеринбург без перехода на летнее время. Фиксированный сдвиг не требует базы часовых поясов на Windows.
TZ_EKB = timezone(timedelta(hours=5), "Екатеринбург")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def __init__(self, tz: timezone = TZ_EKB):
        self.tz = tz

    def now(self) -> datetime:
        return datetime.now(self.tz)


class FakeClock:
    """Учебные часы: время стоит, пока его не перемотают."""

    def __init__(self, start: datetime):
        self._now = _aware(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta) -> datetime:
        self._now += timedelta(**delta)
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = _aware(moment)


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("время без часового пояса")
    return moment
