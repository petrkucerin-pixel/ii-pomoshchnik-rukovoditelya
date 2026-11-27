"""Журнал из нескольких потоков: роботы встреч, веб и бот пишут и читают одновременно."""
import threading
from datetime import datetime, timezone

from yadro.clock import FakeClock
from yadro.journal import Journal


def test_parallel_writes_and_reads_do_not_mix(tmp_path):
    j = Journal(tmp_path / "j.sqlite", FakeClock(datetime(2026, 11, 27, 9, tzinfo=timezone.utc)))
    errors = []

    def work(n):
        try:
            for i in range(150):
                j.log_action("robot_live", path="now", payload={"robot": n, "i": i})
                assert all(isinstance(a["payload"], dict) for a in j.actions())
        except Exception as e:          # noqa: BLE001 — собираем, чтобы показать в assert
            errors.append(repr(e))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(j.actions()) == 600
