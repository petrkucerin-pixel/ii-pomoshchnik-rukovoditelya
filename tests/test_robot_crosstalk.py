"""Говорят сразу несколько человек — робот просит аватара сказать «давайте по очереди». Решает код, не модель."""
from pomoshnik.robot import CROSSTALK_TEXT, Crosstalk, talking_by_vad

BOT = "ИИ-помощник Ивана Петрова"
PEOPLE = {"p1": "Ольга Смирнова", "p2": "Алексей Новиков", "p3": "Петр Кучерин", "p0": BOT, "px": ""}


def test_two_talking_for_a_while_triggers_once_per_minute():
    c = Crosstalk(BOT)
    assert c.check(2, now=100) is False          # только началось
    assert c.check(2, now=103) is False
    assert c.check(2, now=106) is True           # 5+ секунд вместе — просим по очереди
    assert c.check(2, now=109) is False          # уже попросили — не повторяем подряд
    assert c.check(3, now=150) is False          # говорят дальше, но минута с прошлой просьбы не прошла
    assert c.check(3, now=170) is True           # через минуту снова можно
    assert "по очереди" in CROSSTALK_TEXT


def test_pause_resets_the_counter():
    c = Crosstalk(BOT)
    assert c.check(2, now=100) is False
    assert c.check(1, now=103) is False
    assert c.check(2, now=104) is False
    assert c.check(2, now=107) is False          # с паузы прошло 3 с, мало


def test_vad_counts_only_named_people_without_robot():
    assert talking_by_vad({"p0": True, "px": True, "p3": True}, PEOPLE, BOT) == 1
    assert talking_by_vad({"p1": True, "p2": True, "p3": False}, PEOPLE, BOT) == 2
