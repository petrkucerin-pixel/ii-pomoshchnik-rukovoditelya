from datetime import date
from pathlib import Path

from pomoshnik.plan import build_day_plan, load_calendar, load_tasks, render_plan

TASK = Path(__file__).resolve().parent.parent / "data" / "task"
MEET = load_calendar(TASK / "calendar.ics")
TASKS = load_tasks(TASK / "tasks.csv")


def test_calendar_loaded_with_categories():
    assert len(MEET) == 50
    assert MEET[0].title == "Планёрка направления" and MEET[0].category == "операционная"
    assert MEET[0].start.utcoffset().total_seconds() == 5 * 3600
    assert "Ольга Смирнова" in MEET[0].attendees


def test_day_plan_monday():
    p = build_day_plan(date(2026, 12, 7), MEET, TASKS, decisions=["Оффер аналитику — согласовать"],
                       digest=["Отчёт e-com за неделю"])
    assert p.meetings and all(m.start.date() == date(2026, 12, 7) for m in p.meetings)
    assert any(t.id == "t001" for t in p.overdue)                 # статус «просрочена»
    assert all(t.due >= date(2026, 12, 7) for t in p.due_soon)


def test_overdue_by_date():
    p = build_day_plan(date(2026, 12, 14), MEET, TASKS)
    assert any(t.id == "t004" for t in p.overdue)                  # срок 12.12 прошёл


def test_focus_windows_do_not_overlap_meetings():
    day = date(2026, 12, 7)
    p = build_day_plan(day, MEET, TASKS)
    for a, b in p.focus_windows:
        assert (b - a).total_seconds() >= 90 * 60
        assert not any(m.start < b and a < m.end for m in p.meetings)


def test_render_html_escapes_and_sections():
    p = build_day_plan(date(2026, 12, 7), MEET, TASKS, decisions=["Скидка <20%> для «Ромашки»"])
    s = render_plan(p, "Иван Петров")
    assert "Ждут только вас (1)" in s and "&lt;20%&gt;" in s and "Встречи" in s and "понедельник" in s
