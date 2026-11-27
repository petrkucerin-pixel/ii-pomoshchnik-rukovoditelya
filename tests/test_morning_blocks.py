"""TASK_15: утро в Telegram блоками. Пит: «Целая портянка… по каждому пункту отказать/согласовать, через какое-то время
следующий блок… сообщения приятные для чтения и для действия»."""
from datetime import timedelta

from test_assistant import a, cb  # noqa: F401 — a — фикстура помощника на учебных данных


def texts(asst, start=0):
    return [t for _, t, _ in asst.pult.sent[start:]]


def blocks(asst):
    return [x["payload"]["block"] for x in asst.j.actions() if x["kind"] == "tg_block"]


def test_nothing_before_plan_time(a):
    asst, clock, _ = a
    clock.set(clock.now().replace(hour=7, minute=59))
    asst.morning_tick()
    assert asst.pult.sent == []


def test_first_block_greets_and_brings_first_decision(a):
    asst, clock, _ = a
    asst.process_inbox()
    n = len(asst.pult.sent)
    asst.morning_tick()                                         # 08:30 понедельника
    greet, card = asst.pult.sent[n], asst.pult.sent[n + 1]
    assert "Доброе утро, Иван" in greet[1] and "понедельник" in greet[1].lower()
    assert f"Ждут только вас: {len(asst.groups())}" in greet[1] and "первая в" in greet[1]
    assert "Встречи (" not in greet[1] and "Сводка" not in greet[1]       # не портянка: график и сводка — позже
    assert card[2] and card[2][0].startswith("dec:yes:")                  # сразу первая карточка с кнопками
    assert blocks(asst) == ["morning"]
    asst.morning_tick()
    assert blocks(asst) == ["morning"]                                    # второй раз не шлёт


def test_schedule_comes_after_30_minutes_if_decisions_wait(a):
    asst, clock, _ = a
    asst.process_inbox()
    asst.morning_tick()
    clock.advance(minutes=29)
    asst.morning_tick()
    assert blocks(asst) == ["morning"]
    clock.advance(minutes=1)
    n = len(asst.pult.sent)
    asst.morning_tick()
    sched = texts(asst, n)[0]
    assert blocks(asst) == ["morning", "schedule"]
    assert "График" in sched and "Сроки" in sched and "Окна" not in sched
    assert "10:00" in sched or "09:" in sched or "11:" in sched


def test_schedule_comes_right_after_last_decision(a):
    asst, clock, _ = a
    asst.process_inbox()
    asst.morning_tick()
    for _ in range(len(asst.groups())):
        eid = asst.groups()[0]["event_id"]
        asst.handle(cb(f"dec:no:{eid}"))
    clock.advance(minutes=1)
    asst.morning_tick()
    assert blocks(asst) == ["morning", "schedule"]


def test_digest_30_minutes_after_schedule_and_only_if_not_empty(a):
    asst, clock, _ = a
    asst.process_inbox()
    asst.morning_tick()
    clock.advance(minutes=30)
    asst.morning_tick()
    clock.advance(minutes=29)
    asst.morning_tick()
    assert blocks(asst) == ["morning", "schedule"]
    clock.advance(minutes=1)
    n = len(asst.pult.sent)
    asst.morning_tick()
    assert blocks(asst) == ["morning", "schedule", "digest"]
    assert "Сделал без вас" in texts(asst, n)[0]


def test_next_day_starts_again_and_weekend_is_quiet(a):
    asst, clock, _ = a
    asst.morning_tick()
    clock.advance(days=5)                                       # суббота
    asst.morning_tick()
    assert blocks(asst).count("morning") == 1
    clock.advance(days=2)                                       # понедельник
    asst.morning_tick()
    assert blocks(asst).count("morning") == 2


def test_relative_due_words_and_escaping():
    from datetime import date

    from pomoshnik.plan import TaskRow, render_schedule, build_day_plan
    day = date(2026, 12, 7)
    tasks = [TaskRow("t1", "Скидка <20%>", "Ольга Смирнова", day + timedelta(days=1), "в работе"),
             TaskRow("t2", "Прогноз", "Алексей Новиков", day - timedelta(days=3), "в работе")]
    s = render_schedule(build_day_plan(day, [], tasks), set())
    assert "&lt;20%&gt;" in s and "завтра" in s and "с 04.12" in s and "встреч нет" in s.lower()


def test_plan_command_gives_schedule_and_decisions_button(a):
    asst, clock, _ = a
    asst.process_inbox()
    from pomoshnik.channels.telegram import Update
    asst.handle(Update(1, "text", 555, "/plan", 10))
    kind, text, buttons = asst.pult.sent[-1]
    assert "График" in text and buttons == ["dec:next"]


# ---------------- «Позже»: живая проверка Пита 27.11 — отложенные вопросы шли по кругу ----------------
def test_deferred_questions_come_back_once_after_two_hours(a):
    asst, clock, _ = a
    asst.process_inbox()
    asst.morning_tick()
    eid = asst.groups()[0]["event_id"]
    asst.handle(cb(f"dec:later:{eid}"))
    clock.advance(minutes=119)
    n = len(asst.pult.sent)
    for _ in range(3):
        asst.morning_tick()
    assert not any("Вы просили напомнить" in t for t in texts(asst, n))
    clock.advance(minutes=2)
    for _ in range(3):                                          # бот опрашивает часто — напоминание одно
        asst.morning_tick()
    reminders = [t for t in texts(asst, n) if "Вы просили напомнить" in t]
    assert len(reminders) == 1
    assert asst.pult.sent[-1][2] and asst.pult.sent[-1][2][0].startswith("dec:yes:")   # и сразу карточка


def test_morning_counts_only_questions_that_are_not_deferred(a):
    asst, clock, _ = a
    asst.process_inbox()
    total = len(asst.groups())
    asst.handle(cb(f"dec:later:{asst.groups()[0]['event_id']}"))
    asst.morning_tick()
    greet = next(t for t in texts(asst) if "Доброе утро" in t)
    assert f"Ждут только вас: {total - 1}." in greet
