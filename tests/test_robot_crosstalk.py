"""Говорят сразу несколько человек — робот просит аватара сказать «давайте по очереди». Решает код, не модель."""
from pomoshnik.robot import CROSSTALK_TEXT, Crosstalk

BOT = "ИИ-помощник Ивана Петрова"
PEOPLE = {"p1": "Ольга Смирнова", "p2": "Алексей Новиков", "p3": "Петр Кучерин", "p0": BOT, "px": ""}


def test_two_people_talking_for_a_while_triggers_once_per_minute():
    c = Crosstalk(BOT)
    both = {"p1": True, "p2": True}
    assert c.check(both, PEOPLE, now=100) is False          # только началось
    assert c.check(both, PEOPLE, now=103) is False
    assert c.check(both, PEOPLE, now=105) is True           # 5 секунд вместе — просим по очереди
    assert c.check(both, PEOPLE, now=110) is False          # уже попросили — не повторяем подряд
    assert c.check(both, PEOPLE, now=170) is True           # через минуту снова можно
    assert "по очереди" in CROSSTALK_TEXT


def test_avatar_and_unknown_voices_do_not_count():
    c = Crosstalk(BOT)
    for t in (100, 103, 106, 109):                            # аватар и безымянный id (свой же робот) + один человек
        assert c.check({"p0": True, "px": True, "p3": True}, PEOPLE, now=t) is False


def test_pause_resets_the_counter():
    c = Crosstalk(BOT)
    assert c.check({"p1": True, "p2": True}, PEOPLE, now=100) is False
    assert c.check({"p1": True, "p2": False}, PEOPLE, now=103) is False
    assert c.check({"p1": True, "p2": True}, PEOPLE, now=104) is False
    assert c.check({"p1": True, "p2": True}, PEOPLE, now=107) is False   # с паузы прошло 3 с, мало
