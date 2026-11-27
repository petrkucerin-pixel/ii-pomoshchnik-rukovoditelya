"""Аватар стартует, когда во встрече появился человек: состав по подписям Телемоста уже известен.
Живой прогон 27.11: аватар стартовал в пустой комнате и просил вошедшего Пита представиться."""
import asyncio
import time

import pomoshnik.robot as robot
from pomoshnik.robot import RobotState, present_people

BOT = "ИИ-помощник Ивана Петрова"


class FakeRoom:
    """Телемост по шагам: каждый опрос — следующее состояние комнаты."""

    def __init__(self, frames):
        self.frames, self.i = frames, 0

    async def evaluate(self, script, arg=None):
        f = self.frames[min(self.i, len(self.frames) - 1)]
        self.i += 1
        return {"state": "", "log": [], "transcript": [], "sessionAt": None, "signals": [], **f}


def run(frames, monkeypatch, until_s=60):
    monkeypatch.setattr(robot.asyncio, "sleep", _fast_sleep)
    clock = {"t": 1000.0}
    monkeypatch.setattr(robot.time, "time", lambda: clock["t"])
    _fast_sleep.clock = clock
    st = RobotState()
    return asyncio.run(robot._wait_people(FakeRoom(frames), st, BOT, clock["t"] + until_s)), st


async def _fast_sleep(s):
    _fast_sleep.clock["t"] += s


def test_present_people_by_labels_without_robot():
    st = RobotState()
    st.people.update({"p1": "Петр Кучерин", "p0": BOT})
    assert present_people(st, {"names": [BOT, "Петр Кучерин", "Ольга Смирнова"]}, BOT) == ["Петр Кучерин", "Ольга Смирнова"]


def test_waits_for_first_person_and_returns_labels(monkeypatch):
    frames = [{"names": [BOT], "count": 1}] * 5 + [{"names": [BOT, "Петр Кучерин"], "count": 2}]
    people, st = run(frames, monkeypatch)
    assert people == ["Петр Кучерин"] and st.status == "ждёт участников"


def test_nobody_came_means_no_avatar(monkeypatch):
    monkeypatch.setattr(robot, "NOBODY_S", 30)
    people, st = run([{"names": [BOT], "count": 1}], monkeypatch, until_s=600)
    assert people is None and st.end_reason == "никто не пришёл"


def test_stop_while_waiting(monkeypatch):
    monkeypatch.setattr(robot.asyncio, "sleep", _fast_sleep)
    clock = {"t": 0.0}
    _fast_sleep.clock = clock
    monkeypatch.setattr(robot.time, "time", lambda: clock["t"])
    st = RobotState()
    st.stop = True
    assert asyncio.run(robot._wait_people(FakeRoom([{"names": [BOT], "count": 1}]), st, BOT, 100)) is None
