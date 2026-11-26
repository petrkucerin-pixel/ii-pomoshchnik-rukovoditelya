from datetime import datetime, timedelta

import pytest

from yadro.clock import TZ_EKB, FakeClock, SystemClock


def test_system_clock_is_timezone_aware_ekb():
    now = SystemClock().now()
    assert now.utcoffset() == timedelta(hours=5)


def test_fake_clock_advances():
    c = FakeClock(datetime(2026, 11, 24, 10, 0, tzinfo=TZ_EKB))
    c.advance(minutes=5)
    assert c.now() == datetime(2026, 11, 24, 10, 5, tzinfo=TZ_EKB)
    c.advance(days=1, hours=2)
    assert c.now() == datetime(2026, 11, 25, 12, 5, tzinfo=TZ_EKB)


def test_fake_clock_set():
    c = FakeClock(datetime(2026, 11, 24, 10, 0, tzinfo=TZ_EKB))
    c.set(datetime(2026, 12, 1, 9, 0, tzinfo=TZ_EKB))
    assert c.now().day == 1


def test_fake_clock_rejects_naive_time():
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 11, 24, 10, 0))
