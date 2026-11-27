"""Говорят сразу несколько человек — робот просит аватара сказать «давайте по очереди». Решает код, не модель."""
from pomoshnik.robot import CROSSTALK_TEXT, Crosstalk, talking_by_vad

BOT = "ИИ-помощник Ивана Петрова"
PEOPLE = {"p1": "Ольга Смирнова", "p2": "Алексей Новиков", "p3": "Петр Кучерин", "p0": BOT, "px": ""}


def test_two_talking_for_a_while_triggers_once_per_minute():
    c = Crosstalk(BOT)
    assert c.check(2, now=100) is False          # только началось
    assert c.check(2, now=103) is False
    assert c.check(2, now=106) is True           # 6 секунд вместе — просим по очереди
    assert c.check(2, now=109) is False          # уже попросили — не повторяем подряд
    assert [c.check(3, now=t) for t in (150, 160, 163)] == [False] * 3   # минута с прошлой просьбы не прошла
    assert c.check(3, now=166) is True                                     # через минуту снова можно
    assert "по очереди" in CROSSTALK_TEXT


def test_blinking_indicator_does_not_reset():
    """Диагностика 27.11: значок голоса у говорящего гаснет на одну проверку — это всё равно перебивание."""
    c = Crosstalk(BOT)
    assert [c.check(n, now=t) for t, n in [(100, 2), (103, 1), (106, 2)]] == [False, False, True]


def test_short_overlap_is_not_crosstalk():
    c = Crosstalk(BOT)
    assert [c.check(n, now=t) for t, n in [(100, 1), (103, 2), (106, 1), (109, 1), (112, 2)]] == [False] * 5


def test_vad_counts_only_named_people_without_robot():
    assert talking_by_vad({"p0": True, "px": True, "p3": True}, PEOPLE, BOT) == 1
    assert talking_by_vad({"p1": True, "p2": True, "p3": False}, PEOPLE, BOT) == 2
