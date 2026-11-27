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


# ---------------- утро в Telegram блоками (TASK_15) ----------------
# Пит 27.11: «Целая портянка. Давай будет присылаться блоками… сообщения приятные для чтения и для действия».
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def when(d: date, day: date) -> str:
    """Срок словами: сегодня, завтра, послезавтра, иначе дата."""
    return {0: "сегодня", 1: "завтра", 2: "послезавтра"}.get((d - day).days, d.strftime("%d.%m"))


def render_morning(p: DayPlan, leader: str, decisions: int, risks: list[str] = ()) -> str:
    """Блок 1: приветствие и сколько решений ждут. Сразу за ним — первая карточка решения."""
    e = html.escape
    first = (leader.split() or ["!"])[0]
    lines = [f"☀️ <b>Доброе утро, {e(first)}!</b>",
             f"<i>{WEEKDAYS[p.day.weekday()].capitalize()}, {p.day.day} {MONTHS[p.day.month - 1]}</i>", ""]
    n = len(p.meetings)
    lines.append(f"Сегодня {n} {plural(n, 'встреча', 'встречи', 'встреч')}, первая в {p.meetings[0].start:%H:%M}."
                 if n else "Встреч сегодня нет.")
    if decisions:
        lines.append(f"<b>Ждут только вас: {decisions}.</b> Пришлю по одному — отвечайте кнопками.")
    else:
        lines.append("Вопросов, которые ждут только вас, нет.")
    if risks:
        lines += ["", "<b>Важно</b>"] + [f"⚠️ {e(r)}" for r in list(risks)[:3]]
    lines += ["", "<i>График и сроки пришлю, как разберём решения, или через полчаса.</i>" if decisions
              else "<i>График и сроки — следующим сообщением.</i>"]
    return "\n".join(lines)


def render_schedule(p: DayPlan, avatar_uids: set[str]) -> str:
    """Блок 2: встречи по времени и сроки словами."""
    e = html.escape
    lines = [f"🗓 <b>График · {WEEKDAYS[p.day.weekday()]}, {p.day:%d.%m}</b>", ""]
    if p.meetings:
        lines += [f"<b>{m.start:%H:%M}–{m.end:%H:%M}</b>  {e(m.title)}" + ("  🤖" if m.uid in avatar_uids else "")
                  for m in p.meetings]
        if any(m.uid in avatar_uids for m in p.meetings):
            lines.append("<i>🤖 — ведёт ваш ИИ-аватар, протокол пришлю после встречи</i>")
    else:
        lines.append("Встреч нет.")
    lines += ["", "⏰ <b>Сроки</b>"]
    if p.overdue:
        lines.append("<b>Просрочено</b>")
        lines += [f"• {e(t.task)} — {e(t.owner)}, " + (f"с {t.due:%d.%m}" if t.due < p.day else f"срок {t.due:%d.%m}")
                  for t in p.overdue]
    if p.due_soon:
        lines.append("<b>Скоро</b>")
        lines += [f"• {e(t.task)} — {e(t.owner)}, {when(t.due, p.day)}" for t in p.due_soon]
    if not p.overdue and not p.due_soon:
        lines.append("Просроченных и близких сроков нет.")
    return "\n".join(lines)


def render_digest(items: list[str], url: str = "") -> str:
    """Блок 3: что помощник сделал без руководителя."""
    e = html.escape
    lines = ["✅ <b>Сделал без вас</b>", ""] + [f"• {e(x)}" for x in items]
    if url:
        lines += ["", f'<i>Подробности — в <a href="{e(url)}">кабинете</a></i>']
    return "\n".join(lines)
