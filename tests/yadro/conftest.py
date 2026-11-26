from datetime import datetime

import pytest

from yadro.clock import TZ_EKB, FakeClock
from yadro.journal import Journal

# Вторник, 24 ноября 2026, 10:00 по Екатеринбургу. Пятница той же недели — 27.11.
TUESDAY = datetime(2026, 11, 24, 10, 0, tzinfo=TZ_EKB)


@pytest.fixture
def clock():
    return FakeClock(TUESDAY)


@pytest.fixture
def journal(tmp_path, clock):
    j = Journal(tmp_path / "journal.sqlite", clock)
    yield j
    j.close()
