"""Утренний план дня: встречи, задачи со сроками, решения только для руководителя, свободные окна под стратегию."""
from __future__ import annotations

import csv
import html
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from icalendar import Calendar

from yadro.clock import TZ_EKB

WORK_START, WORK_END = time(9, 0), time(19, 0)
MIN_FOCUS = timedelta(minutes=90)


@dataclass(frozen=True)
class Meeting:
    uid: str
    start: datetime
    end: datetime
    title: str
    category: str
    attendees: tuple[str, ...] = ()
    avatar: bool = False          # встречу ведёт аватар — автопилот идёт на неё при любой категории

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


@dataclass(frozen=True)
class TaskRow:
    id: str
    task: str
    owner: str
    due: date
    status: str


@dataclass
class DayPlan:
    day: date
    meetings: list[Meeting] = field(default_factory=list)
    overdue: list[TaskRow] = field(default_factory=list)
    due_soon: list[TaskRow] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    digest: list[str] = field(default_factory=list)
    focus_windows: list[tuple[datetime, datetime]] = field(default_factory=list)


def _dt(v) -> datetime:
    v = v.dt
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=TZ_EKB)
    return datetime.combine(v, time(0), tzinfo=TZ_EKB)


def load_calendar(path: str | Path) -> list[Meeting]:
    cal = Calendar.from_ical(Path(path).read_bytes())
    out = []
    for ev in cal.walk("VEVENT"):
        att = ev.get("ATTENDEE") or []
        if not isinstance(att, list):
            att = [att]
        cats = ev.get("CATEGORIES")
        cat = ""
        if cats is not None:
            c = cats[0] if isinstance(cats, list) else cats
            cat = str(c.cats[0]) if getattr(c, "cats", None) else str(c)
        out.append(Meeting(str(ev.get("UID")), _dt(ev["DTSTART"]), _dt(ev["DTEND"]), str(ev.get("SUMMARY", "")), cat,
                           tuple(str(a.params.get("CN", a)) for a in att)))
    return sorted(out, key=lambda m: m.start)


def load_tasks(path: str | Path) -> list[TaskRow]:
    return [TaskRow(r["id"], r["task"], r["owner"], date.fromisoformat(r["due"]), r["status"])
            for r in csv.DictReader(open(path, encoding="utf-8"))]


def focus_windows(meetings: list[Meeting], day: date) -> list[tuple[datetime, datetime]]:
    cur = datetime.combine(day, WORK_START, tzinfo=TZ_EKB)
    end = datetime.combine(day, WORK_END, tzinfo=TZ_EKB)
    out = []
    for m in sorted((m for m in meetings if m.start.date() == day), key=lambda m: m.start):
        if m.start - cur >= MIN_FOCUS:
            out.append((cur, m.start))
        cur = max(cur, m.end)
    if end - cur >= MIN_FOCUS:
        out.append((cur, end))
    return out


def build_day_plan(day: date, meetings: list[Meeting], tasks: list[TaskRow], *, decisions=(), digest=()) -> DayPlan:
    todays = [m for m in meetings if m.start.date() == day]
    open_tasks = [t for t in tasks if t.status not in ("готово", "закрыта", "выполнена")]
    return DayPlan(
        day=day, meetings=todays,
        overdue=[t for t in open_tasks if t.status == "просрочена" or t.due < day],
        due_soon=[t for t in open_tasks if day <= t.due <= day + timedelta(days=2) and t.status != "просрочена"],
        decisions=list(decisions), digest=list(digest), focus_windows=focus_windows(meetings, day))


WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def render_plan(p: DayPlan, leader: str = "") -> str:
    e = html.escape
    lines = [f"<b>План на {p.day.strftime('%d.%m')}, {WEEKDAYS[p.day.weekday()]}</b>" + (f" · {e(leader)}" if leader else "")]
    if p.decisions:
        lines += ["", f"<b>Ждут только вас ({len(p.decisions)})</b>"] + [f"• {e(d)}" for d in p.decisions]
    lines += ["", f"<b>Встречи ({len(p.meetings)})</b>"]
    lines += [f"• {m.start.strftime('%H:%M')}–{m.end.strftime('%H:%M')} {e(m.title)} <i>{e(m.category)}</i>"
              for m in p.meetings] or ["• встреч нет"]
    if p.focus_windows:
        lines += ["", "<b>Окна под стратегию и один на один</b>"]
        lines += [f"• {a.strftime('%H:%M')}–{b.strftime('%H:%M')}" for a, b in p.focus_windows]
    if p.overdue:
        lines += ["", f"<b>Просрочено ({len(p.overdue)})</b>"]
        lines += [f"• {e(t.task)} — {e(t.owner)}, срок {t.due.strftime('%d.%m')}" for t in p.overdue]
    if p.due_soon:
        lines += ["", "<b>Сроки в ближайшие 2 дня</b>"]
        lines += [f"• {e(t.task)} — {e(t.owner)}, {t.due.strftime('%d.%m')}" for t in p.due_soon]
    if p.digest:
        lines += ["", f"<b>Сводка без вашего участия ({len(p.digest)})</b>"] + [f"• {e(d)}" for d in p.digest]
    return "\n".join(lines)
