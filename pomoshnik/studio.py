"""Аватар на встрече сам: подготовка по календарю, студия для Телемоста, протокол после встречи без кнопки (TASK_09).

Правила — в коде: какие встречи, когда готовить, когда считать встречу законченной. Модель здесь не участвует,
кроме сборки протокола из расшифровки (как и при ручной кнопке).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from urllib.parse import quote

from yadro.clock import TZ_EKB

from . import cabinet
from .assistant import Assistant, meeting_brief
from .avatar import LiveAvatarError, build_context_prompt, end_time, latest_finished, opening_text, transcript_to_text
from .memory import brief

log = logging.getLogger("pomoshnik.studio")
TAIL_MIN = 10          # окно аватара живёт дольше встречи на столько минут
GRACE_MIN = 20         # столько ждём завершённую сессию после конца встречи


def leader_genitive(a: Assistant) -> str:
    return os.environ.get("LEADER_NAME_GENITIVE") or a.p.name


def prepare_avatar(a: Assistant, la, avatar_id: str, title: str, uid: str | None, *, max_seconds: int = 1200,
                   embed: bool = True, participants: list[str] | None = None) -> dict:
    """Контекст аватара для встречи: прошлый протокол, вопросы ревизора, вся история, задачи участников."""
    card = cabinet.meeting(a, uid) if uid else None
    history = brief(a.memory) if a.memory is not None else ""
    if card:
        questions, history_questions = card["previous_questions"], card["history_questions"]
        tasks = card["tasks"]
    else:
        pid = a.protocols.last_approved()
        questions = a.meeting_questions() if pid else []
        _, history_questions = meeting_brief(a.j, None, a.today())
        today = a.today()
        tasks = [cabinet.task_dict(t, today, src) for t, src in cabinet.all_tasks(a) if t.status not in cabinet.DONE]
    task_lines = [f"{t['task']} — {t['owner']}, до {t['due'][8:10]}.{t['due'][5:7]}" for t in tasks]
    if participants is None:       # по приглашению; робот передаёт фактический состав встречи из Телемоста
        participants = [x["name"] for x in card["meeting"]["attendees"]] if card else []
    gen = leader_genitive(a)
    ctx = la.create_context(f"{title} {a.today():%d.%m}",
                            build_context_prompt(leader=gen, title=title, questions=questions, open_tasks=task_lines,
                                                 style=a.p.style, history=history, history_questions=history_questions,
                                                 participants=participants),
                            opening_text(gen))
    out = {"context_id": ctx, "embed_id": None, "url": "", "questions": questions, "participants": participants,
           "history_questions": history_questions, "history_chars": len(history)}
    if embed:                       # окно для браузера; роботу Телемоста оно не нужно
        emb = la.create_embed(avatar_id, ctx, la.voice_id(), max_seconds=max_seconds)
        out |= {"embed_id": emb.get("embed_id") or emb.get("id"), "url": emb.get("url") or emb.get("embed_url") or ""}
    return out


def finish_session(a: Assistant, la, embed_id: str, title: str, uid: str | None):
    """Завершённая сессия аватара → черновик протокола на утверждение. None — сессии ещё нет, 0 — разговора не было."""
    s = latest_finished(la.sessions(embed_id=embed_id))
    if not s:
        return None
    text = transcript_to_text(la.transcript(s.id), merge=True)
    if not text.strip():
        return 0
    return a.new_meeting(text, title, end_time(s, TZ_EKB), meeting_uid=uid)


class Autopilot:
    def __init__(self, assistant, liveavatar, *, avatar_id: str, base_url: str, categories: set[str],
                 lead_min: int = 1, robot=None):
        self.robot = robot                                  # робот Телемоста; без него — окно для студии
        self._a, self._la = assistant, liveavatar          # функции: помощник и клиент LiveAvatar берутся при каждом шаге
        self.avatar_id, self.base_url = avatar_id, base_url.rstrip("/")
        self.categories, self.lead = set(categories), timedelta(minutes=lead_min)

    # время стенда на рабочий день (DEMO_DATE переносит учебный календарь на сегодня)
    def _now(self, a: Assistant) -> datetime:
        return datetime.combine(a.today(), a.clock.now().astimezone(TZ_EKB).timetz())

    def _meetings(self, a: Assistant):
        day = a.today()
        return [m for m in a.meetings if m.start.date() == day and m.category in self.categories]

    def _marks(self, a: Assistant) -> tuple[dict, dict]:
        day = a.today().isoformat()
        ready, done = {}, {}
        for x in a.j.actions():
            pl = x["payload"] or {}
            if pl.get("day") != day:
                continue
            if x["kind"] == "avatar_auto_ready":
                ready[pl["uid"]] = pl
            elif x["kind"] == "avatar_auto_done":
                done[pl["uid"]] = pl
        return ready, done

    def tick(self) -> None:
        a = self._a()
        now = self._now(a)
        ready, done = self._marks(a)
        for m in self._meetings(a):
            try:
                if m.uid not in ready and m.start - self.lead <= now < m.end:
                    self._prepare(a, m)
                elif m.uid in ready and m.uid not in done and now >= m.end:
                    self._finish(a, m, ready[m.uid], now)
            except LiveAvatarError as e:
                log.warning("автопилот аватара: %s", e)
                a.j.log_action("avatar_auto_error", summary=f"{m.title}: {e}"[:200],
                               payload={"uid": m.uid, "day": a.today().isoformat(), "error": str(e)[:200]})

    def _link(self, a: Assistant, uid: str) -> str | None:
        if self.robot is None:
            return None
        from .robot.manager import links_from_journal
        return links_from_journal(a.j).get(uid)

    def _prepare(self, a: Assistant, m) -> None:
        minutes = int((m.end - m.start).total_seconds() // 60) + TAIL_MIN
        link = self._link(a, m.uid)
        if link:                       # у встречи есть комната Телемоста — туда идёт робот с аватаром
            if self.robot.running:
                return                 # робот ещё на прошлой встрече — попробуем на следующем шаге
            a.pult.send_text(f"Через минуту «{m.title}» ({m.start:%H:%M}). Аватар заходит в Телемост сам: {link}")
            self.robot.start(link=link, title=m.title, uid=m.uid, minutes=minutes)
            a.j.log_action("avatar_auto_ready", path="now", summary=f"{m.title} {m.start:%H:%M}: робот ушёл в Телемост",
                           payload={"uid": m.uid, "day": a.today().isoformat(), "title": m.title, "robot": True,
                                    "start": m.start.isoformat(), "end": m.end.isoformat(), "embed_id": None,
                                    "url": link, "minutes_saved": 0})
            return
        r = prepare_avatar(a, self._la(), self.avatar_id, m.title, m.uid, max_seconds=minutes * 60)
        a.j.log_action("avatar_auto_ready", path="now",
                       summary=f"{m.title} {m.start:%H:%M}: аватар готов, вопросов {len(r['questions'])} + "
                               f"{len(r['history_questions'])} из истории",
                       payload={"uid": m.uid, "day": a.today().isoformat(), "title": m.title,
                                "start": m.start.isoformat(), "end": m.end.isoformat(), "embed_id": r["embed_id"],
                                "url": r["url"], "minutes_saved": 0})
        a.pult.send_text(f"Через минуту «{m.title}» ({m.start:%H:%M}). Аватар готов и подключится сам.\n"
                         f"Студия: {self.base_url}/studio\nЭкран аватара: {self.screen_url(m.uid)}")

    def _finish(self, a: Assistant, m, r: dict, now: datetime) -> None:
        if r.get("robot"):             # протокол собирает робот — ждём его отчёт
            if self.robot.running and self.robot.meta.get("uid") == m.uid:
                return
            rd = [x["payload"] for x in a.j.actions() if x["kind"] == "robot_done" and x["payload"].get("uid") == m.uid]
            pid = rd[-1].get("protocol_id") if rd else None
            a.j.log_action("avatar_auto_done", path="now", summary=f"{m.title}: {'protocol' if pid else 'no_session'}",
                           payload={"uid": m.uid, "day": a.today().isoformat(),
                                    "result": "protocol" if pid else "no_session", "protocol_id": pid})
            return
        pid = finish_session(a, self._la(), r["embed_id"], m.title, m.uid)
        if pid is None and now < m.end + timedelta(minutes=GRACE_MIN):
            return                                          # сессия может закрыться позже — ждём
        result = "protocol" if pid else ("empty" if pid == 0 else "no_session")
        a.j.log_action("avatar_auto_done", path="now", summary=f"{m.title}: {result}",
                       payload={"uid": m.uid, "day": a.today().isoformat(), "result": result,
                                "protocol_id": pid or None})
        if not pid:
            a.pult.send_text(f"«{m.title}» прошла без аватара: разговора не было, протокола нет.")

    def screen_url(self, uid: str) -> str:
        return f"{self.base_url}/avatar/screen?uid={quote(uid, safe='@')}"

    def state(self) -> dict:
        """Для страницы студии: что делать с окном аватара прямо сейчас."""
        a = self._a()
        now = self._now(a)
        ready, done = self._marks(a)
        meetings = self._meetings(a)
        upcoming = [m for m in meetings if m.end > now]
        base = {"now": now.isoformat(), "categories": sorted(self.categories),
                "upcoming": [{"uid": m.uid, "title": m.title, "start": m.start.isoformat(), "end": m.end.isoformat()}
                             for m in upcoming]}
        for m in meetings:
            r = ready.get(m.uid)
            if r and m.uid not in done and now < m.end + timedelta(minutes=TAIL_MIN):
                return base | {"state": "ready", "open_now": m.start - self.lead <= now,
                               "meeting": {"uid": m.uid, "title": m.title, "start": r["start"], "end": r["end"]},
                               "url": r["url"], "screen_url": self.screen_url(m.uid)}
        last = [m for m in meetings if m.uid in done and now < m.end + timedelta(minutes=30)]
        if last:
            m = last[-1]
            return base | {"state": "done", "open_now": False,
                           "meeting": {"uid": m.uid, "title": m.title, "start": m.start.isoformat(),
                                       "end": m.end.isoformat()}, "result": done[m.uid].get("result")}
        return base | {"state": "idle", "open_now": False}
