"""Проверки раскладки пересечений и учебной недели в часовом поясе другого браузера."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "pomoshnik/static/cabinet/cabinet.js"


@pytest.fixture(scope="module")
def calendar():
    node = shutil.which("node")
    if not node:
        pytest.skip("Для проверки JavaScript нужен node")
    # Load the classic browser script without starting the page or calling its API.
    script = """
        const fs = require('node:fs'), vm = require('node:vm');
        const page = vm.createContext({
          window: {matchMedia() { return {matches: false}; }},
          document: {addEventListener() {}}
        });
        vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), page);
        const meeting = (id, start, end) => ({
          id, start: '2027-01-01T' + start + ':00+05:00', end: '2027-01-01T' + end + ':00+05:00'
        });
        const events = [meeting('later', '12:00', '13:00'), meeting('long', '09:00', '11:00'),
          meeting('short', '09:30', '10:00'), meeting('third', '09:45', '10:15'),
          meeting('reuse', '10:00', '10:45'), meeting('next', '11:00', '12:00')];
        console.log(JSON.stringify({
          layout: page.meetingLanes(events).map(({item, lane, lanes}) => [item.id, lane, lanes]),
          monday: page.monday('2027-01-03'), tomorrow: page.shiftDay('2026-12-31', 1),
          date: page.dateLabel(events[0].start, {day:'numeric'}), start: page.minute(events[0].start)
        }));
    """
    result = subprocess.run([node, "-e", script, str(SCRIPT)], check=True, capture_output=True,
                            encoding="utf-8", env={**os.environ, "TZ": "America/Los_Angeles"})
    return json.loads(result.stdout)


def test_overlap_clusters_reuse_lanes_and_release_width(calendar):
    assert calendar["layout"] == [
        ["long", 0, 3], ["short", 1, 3], ["third", 2, 3], ["reuse", 1, 3],
        ["next", 0, 1], ["later", 0, 1],
    ]


def test_week_and_hours_use_the_api_calendar_date(calendar):
    assert (calendar["monday"], calendar["tomorrow"]) == ("2026-12-28", "2027-01-01")
    assert (calendar["date"], calendar["start"]) == ("1", 12 * 60)
