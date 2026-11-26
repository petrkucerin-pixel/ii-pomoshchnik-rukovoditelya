from datetime import date, datetime

import pytest

from yadro.clock import TZ_EKB
from yadro.deadlines import add_working_days, default_deadline, parse_deadline

TUE = datetime(2026, 11, 24, 10, 0, tzinfo=TZ_EKB)   # вторник
THU = datetime(2026, 11, 26, 10, 0, tzinfo=TZ_EKB)   # четверг
SAT = datetime(2026, 11, 28, 10, 0, tzinfo=TZ_EKB)   # суббота


def at(d, m, h=18, mi=0, y=2026):
    return datetime(y, m, d, h, mi, tzinfo=TZ_EKB)


def due(text, now=TUE, **kw):
    r = parse_deadline(text, now, **kw)
    return None if r is None else r.due


# --- относительные дни ---------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("сегодня", at(24, 11)),
    ("завтра", at(25, 11)),
    ("послезавтра", at(26, 11)),
    ("Завтра", at(25, 11)),
])
def test_relative_days(text, expected):
    assert due(text) == expected


def test_poslezavtra_is_not_read_as_zavtra():
    r = parse_deadline("сделать послезавтра", TUE)
    assert r.matched == "послезавтра"


# --- дни недели ----------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("к пятнице", at(27, 11)),
    ("до пятницы", at(27, 11)),
    ("в пятницу", at(27, 11)),
    ("К Пятнице", at(27, 11)),
    ("в среду", at(25, 11)),
    ("к четвергу", at(26, 11)),
    ("в понедельник", at(30, 11)),
    ("до понедельника", at(30, 11)),
    ("в субботу", at(28, 11)),
    ("в воскресенье", at(29, 11)),
    ("ко вторнику", at(24, 11)),          # сегодня вторник — считается сегодня
])
def test_weekdays(text, expected):
    assert due(text) == expected


def test_next_friday_skips_this_week():
    assert due("к следующей пятнице") == at(4, 12)
    assert due("в следующую среду") == at(2, 12)


def test_next_monday_from_tuesday_is_next_week_anyway():
    # ближайший понедельник уже на следующей неделе — «следующий» его и означает
    assert due("в следующий понедельник") == at(30, 11)


# --- через N -------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("через 3 дня", at(27, 11)),
    ("через 1 день", at(25, 11)),
    ("через два дня", at(26, 11)),
    ("через пять дней", at(29, 11)),
    ("через неделю", at(1, 12)),
    ("через 2 недели", at(8, 12)),
    ("через две недели", at(8, 12)),
])
def test_in_n_days_weeks(text, expected):
    assert due(text) == expected


def test_in_hours():
    assert due("через 3 часа") == at(24, 11, 13, 0)
    assert due("через час") == at(24, 11, 11, 0)


def test_in_working_days_skips_weekend():
    assert due("через 2 рабочих дня") == at(26, 11)            # вт → чт
    assert due("через 2 рабочих дня", now=THU) == at(30, 11)   # чт → пн
    assert due("через 1 рабочий день", now=SAT) == at(30, 11)  # сб → пн


def test_in_working_days_skips_holidays():
    assert due("через 2 рабочих дня", now=THU, holidays={date(2026, 11, 30)}) == at(1, 12)


# --- до конца ------------------------------------------------------------

def test_end_of_day_week_month():
    assert due("до конца дня") == at(24, 11)
    assert due("до конца недели") == at(27, 11)
    assert due("до конца месяца") == at(30, 11)


def test_end_of_week_on_saturday_is_today():
    assert due("до конца недели", now=SAT) == at(28, 11)


# --- явные даты ----------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("28.11", at(28, 11)),
    ("28.11.2026", at(28, 11)),
    ("28.11.26", at(28, 11)),
    ("1.12", at(1, 12)),
    ("01.02", at(1, 2, y=2027)),          # прошедшая дата без года — следующий год
    ("28 ноября", at(28, 11)),
    ("1 декабря", at(1, 12)),
    ("15 марта", at(15, 3, y=2027)),
    ("24.11", at(24, 11)),                # сегодняшняя дата остаётся сегодня
])
def test_explicit_dates(text, expected):
    assert due(text) == expected


@pytest.mark.parametrize("text", ["31.02", "32.12", "30 февраля", "00.11"])
def test_impossible_dates_are_not_deadlines(text):
    assert parse_deadline(text, TUE) is None


def test_version_numbers_like_dates_inside_words_are_ignored():
    assert parse_deadline("обновите до v1.12a", TUE) is None


# --- время ---------------------------------------------------------------

def test_time_with_relative_day():
    assert due("завтра к 15:00") == at(25, 11, 15, 0)
    assert due("28.11 в 10:30") == at(28, 11, 10, 30)
    assert due("в пятницу до 12:00") == at(27, 11, 12, 0)


def test_time_only_today_or_tomorrow():
    assert due("к 15:00") == at(24, 11, 15, 0)
    late = datetime(2026, 11, 24, 16, 0, tzinfo=TZ_EKB)
    assert due("к 15:00", now=late) == at(25, 11, 15, 0)


def test_invalid_time_is_ignored():
    assert due("завтра в 25:00") == at(25, 11)


# --- в тексте ------------------------------------------------------------

def test_deadline_inside_sentence():
    r = parse_deadline("Анна, подготовь, пожалуйста, лендинг к пятнице. Спасибо!", TUE)
    assert r.due == at(27, 11)
    assert r.matched.lower() == "к пятнице"


def test_first_deadline_in_text_wins():
    r = parse_deadline("черновик завтра, финал к пятнице", TUE)
    assert r.due == at(25, 11)


def test_no_deadline():
    assert parse_deadline("подготовь лендинг акции", TUE) is None
    assert parse_deadline("", TUE) is None


def test_result_keeps_timezone():
    assert parse_deadline("завтра", TUE).due.tzinfo is not None


def test_custom_end_of_day():
    from datetime import time
    assert due("завтра", end_of_day=time(20, 0)) == at(25, 11, 20, 0)


# --- помощники ------------------------------------------------------------

def test_add_working_days():
    assert add_working_days(date(2026, 11, 27), 1) == date(2026, 11, 30)
    assert add_working_days(date(2026, 11, 24), 0) == date(2026, 11, 24)
    assert add_working_days(date(2026, 11, 27), 1, holidays={date(2026, 11, 30)}) == date(2026, 12, 1)


def test_default_deadline():
    assert default_deadline(TUE, 3) == at(27, 11)
    assert default_deadline(THU, 3) == at(1, 12)
