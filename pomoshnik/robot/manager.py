"""Робот Телемоста на стенде (TASK_11): один робот за раз, протокол из полной расшифровки LiveAvatar.

Ссылки Телемоста у встреч хранятся в журнале (`meeting_link`), последняя запись главная.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from yadro.clock import TZ_EKB

from ..avatar import LiveAvatarError, transcript_to_text
from . import RobotState, run_meeting
from .speakers import aliases_from_journal, intervals, label_entries, learn_aliases, resolve_name

log = logging.getLogger("pomoshnik.robot")


def check_link(link: str) -> str:
    link = (link or "").strip()
    u = urlparse(link)
    if u.scheme != "https" or not u.netloc:
        raise ValueError("нужна ссылка https://… на встречу Телемоста")
    return link


def set_link(journal, uid: str, link: str) -> None:
    journal.log_action("meeting_link", actor="human", path="now", summary=f"{uid}: {check_link(link)}",
                       payload={"uid": uid, "link": link.strip()})


def links_from_journal(journal) -> dict[str, str]:
    return {x["payload"]["uid"]: x["payload"]["link"] for x in journal.actions() if x["kind"] == "meeting_link"}


LIVE_EVERY_S = 180       # черновик протокола по ходу встречи — не чаще раза в 3 минуты
LIVE_POLL_S = 20


@dataclass
class Robot:
    """Один робот на одной встрече."""
    key: str
    st: RobotState
    meta: dict
    running: bool = True
    live: dict = field(default_factory=lambda: {"protocol": None, "updated_at": None})
    live_seen: int = 0
    live_at: float = 0.0


def robot_max() -> int:
    try:
        return max(1, int(os.environ.get("ROBOT_MAX", "3")))
    except ValueError:
        return 3


class RobotManager:
    """Роботы на встречах — по одному на встречу, одновременно до ROBOT_MAX (на 2 ядрах — 3)."""

    def __init__(self, assistant, liveavatar, lock, *, avatar_id: str, shots_dir: Path | None = None,
                 runner=run_meeting, threaded: bool = True, transcript_wait_s: float = 15, live: bool = True):
        self._a, self._la, self.lock = assistant, liveavatar, lock
        self.avatar_id, self.shots_dir, self.runner = avatar_id, shots_dir, runner
        self.threaded, self.wait_s, self.live_enabled = threaded, transcript_wait_s, live
        self.robots: dict[str, Robot] = {}
        self.last: Robot | None = None

    # ---- последний запущенный робот (для простых проверок и старых вызовов) ----
    @property
    def running(self) -> bool:
        return any(r.running for r in self.robots.values())

    @property
    def meta(self) -> dict:
        return self.last.meta if self.last else {}

    @property
    def st(self) -> RobotState | None:
        return self.last.st if self.last else None

    def is_running(self, uid: str | None) -> bool:
        r = self.robots.get(uid or "")
        return bool(r and r.running)

    def busy(self) -> int:
        return sum(1 for r in self.robots.values() if r.running)

    def has_room(self) -> bool:
        return self.busy() < robot_max()

    def name(self) -> str:
        a = self._a()
        return f"ИИ-помощник {os.environ.get('LEADER_NAME_GENITIVE') or a.p.name}"

    def start(self, *, link: str, title: str, uid: str | None = None, minutes: int = 60) -> dict:
        from ..studio import prepare_avatar
        link = check_link(link)
        key = uid or link
        if self.is_running(key):
            raise RuntimeError("аватар уже на этой встрече")
        if not self.has_room():
            raise RuntimeError(f"аватар уже ведёт {self.busy()} встречи одновременно — это предел сервера")
        a = self._a()
        robot = Robot(key, RobotState(), {"uid": uid, "title": title, "link": link, "session_id": None,
                                          "started_at": datetime.now(TZ_EKB).isoformat(timespec="seconds"),
                                          "protocol_id": None})
        self.robots[key] = self.last = robot
        a.j.log_action("robot_started", path="now", summary=f"{title}: робот заходит в Телемост",
                       payload={k: robot.meta[k] for k in ("uid", "title", "link")})

        def session_token(present: list[str]) -> str:
            """Вызывает робот после входа: контекст аватара с фактическим составом встречи и известными связками."""
            la = self._la()
            with self.lock:
                a = self._a()
                r = prepare_avatar(a, la, self.avatar_id, title, uid, embed=False,
                                   participants=self._who(a, present) or None)
            tok = la.create_session_token(self.avatar_id, r["context_id"], la.voice_id())
            robot.meta["session_id"] = tok["session_id"]
            return tok["session_token"]

        args = (robot, link, session_token, time.time() + minutes * 60)
        if self.threaded:
            threading.Thread(target=self._run, args=args, daemon=True).start()
            if self.live_enabled:
                threading.Thread(target=self._live_loop, args=(robot,), daemon=True).start()
        else:
            self._run(*args)
        return self.status(key)

    def stop(self, uid: str | None = None) -> bool:
        targets = [self.robots[uid]] if uid in self.robots else ([] if uid else list(self.robots.values()))
        hit = False
        for r in targets:
            if r.running:
                r.st.stop = hit = True
        return hit

    def _run(self, robot: Robot, link: str, token, until: float) -> None:
        st = robot.st
        try:
            asyncio.run(self.runner(link, name=self.name(), token=token, until=until, st=st, shots_dir=self.shots_dir))
        except Exception as e:                                  # браузер упал — протокол всё равно собираем
            st.note(f"робот упал: {type(e).__name__}: {str(e)[:200]}")
            st.end_reason = st.end_reason or "ошибка робота"
        try:
            self._finish(robot)
        except Exception as e:
            log.exception("протокол после робота")
            st.note(f"протокол не собран: {type(e).__name__}: {str(e)[:200]}")
        finally:
            robot.running = False

    # ---------------- протокол по ходу встречи ----------------
    def _live_entries(self, robot: Robot) -> list[dict]:
        st = robot.st
        t0 = st.session_at or st.started
        entries = [{"role": x["role"], "transcript": x["text"], "relative_timestamp": (x["t"] - t0) / 1000}
                   for x in st.transcript]
        return label_entries(entries, st.vad_events, t0, st.people, robot_name=self.name(),
                             aliases=aliases_from_journal(self._a().j), solo=self._solo(st))

    def _solo(self, st: RobotState) -> str | None:
        """Единственный человек во встрече (кроме аватара) — ему принадлежат все реплики участников."""
        people = {n for n in [*st.participants, *st.people.values()] if n and n != self.name()}
        return next(iter(people)) if len(people) == 1 else None

    def live_tick(self, robot: Robot, *, force: bool = False) -> bool:
        """Черновик договорённостей по расшифровке, накопленной к этому моменту. True — обновили."""
        from ..protocol import build_protocol
        labeled = self._live_entries(robot)
        said = sum(1 for e in labeled if e.get("role") != "avatar")
        if not said or said == robot.live_seen:
            return False
        if not force and time.time() - robot.live_at < LIVE_EVERY_S:
            return False
        a = self._a()
        text = transcript_to_text(labeled, merge=True)
        people = ", ".join(sorted(robot.st.participants)) or "не определены"
        p = build_protocol(a.llm, f"Участники встречи в Телемосте: {people}\n\n{text}", title=robot.meta["title"],
                           meeting_end=datetime.now(TZ_EKB), org=a.p.org, rules=a.protocols.leader_rules())
        robot.live = {"protocol": {"decisions": [d.text for d in p.decisions],
                                   "tasks": [{"owner": t.owner, "what": t.what, "due": t.due.isoformat(),
                                              "default_due": t.default_due} for t in p.tasks],
                                   "open_questions": p.open_questions},
                      "updated_at": datetime.now(TZ_EKB).isoformat(timespec="seconds")}
        robot.live_seen, robot.live_at = said, time.time()
        return True

    def _live_loop(self, robot: Robot) -> None:
        while robot.running:
            time.sleep(LIVE_POLL_S)
            try:
                self.live_tick(robot)
            except Exception as e:                  # черновик — не главное, встречу не ломаем
                robot.st.note(f"черновик протокола не обновился: {type(e).__name__}")

    @staticmethod
    def _who(a, present: list[str]) -> list[str]:
        """Подписи в Телемосте → для аватара: кого он знает, кого надо попросить представиться."""
        aliases = aliases_from_journal(a.j)
        out = []
        for n in present:
            person = aliases.get(n) or resolve_name(n, a.p.org)
            if person and person != n:
                out.append(f"{n} (это {person})")
            elif person:
                out.append(n)
            else:
                out.append(f"{n} (пока не знаю, кто это — попроси представиться)")
        return out

    def _entries(self, robot: Robot) -> tuple[list[dict], str]:
        """Полная расшифровка LiveAvatar (подождав, пока сессия закроется), иначе — то, что успел записать мост."""
        la, st = self._la(), robot.st
        for attempt in range(4):
            entries = []
            if robot.meta.get("session_id"):
                try:
                    entries = la.transcript(robot.meta["session_id"])
                except LiveAvatarError as e:
                    st.note(f"расшифровка LiveAvatar недоступна: {e}")
            if entries:
                return entries, "liveavatar"
            if attempt < 3 and self.wait_s:
                time.sleep(self.wait_s)
        t0 = st.session_at or st.started
        return [{"role": x["role"], "transcript": x["text"], "relative_timestamp": (x["t"] - t0) / 1000}
                for x in st.transcript], "bridge"

    def _finish(self, robot: Robot) -> None:
        st, meta = robot.st, robot.meta
        entries, source = self._entries(robot)
        a = self._a()
        # кто говорил: лента vad из Телемоста; новые связки «подпись → человек» запоминаем
        labeled = label_entries(entries, st.vad_events, st.session_at or st.started, st.people,
                                robot_name=self.name(), aliases=aliases_from_journal(a.j), solo=self._solo(st))
        if learn_aliases(a.j, labeled, a.p.org):
            labeled = label_entries(entries, st.vad_events, st.session_at or st.started, st.people,
                                    robot_name=self.name(), aliases=aliases_from_journal(a.j), solo=self._solo(st))
        text = transcript_to_text(labeled, merge=True)
        aliases = aliases_from_journal(a.j)
        st.participants = {aliases.get(n) or resolve_name(n, a.p.org) or n for n in st.participants}
        pid = None
        if any(e.get("role") != "avatar" for e in labeled):   # говорил только аватар — встречи не было
            people = ", ".join(sorted(st.participants)) or "не определены"
            body = f"Участники встречи в Телемосте: {people}\n\n{text}"
            with self.lock:
                pid = a.new_meeting(body, meta["title"], datetime.now(TZ_EKB), meeting_uid=meta["uid"])
        else:
            a.pult.send_text(f"«{meta['title']}»: разговора не было ({st.end_reason or 'робот вышел'}), протокола нет.")
        meta["protocol_id"] = pid
        a.j.log_action("robot_done", path="now",
                       summary=f"{meta['title']}: {st.end_reason or 'завершено'}, протокол {pid or 'нет'}",
                       payload={"uid": meta["uid"], "session_id": meta["session_id"], "protocol_id": pid,
                                "reason": st.end_reason, "source": source, "participants": sorted(st.participants)})

    def status(self, uid: str | None = None) -> dict:
        """Состояние робота встречи (uid) или всех роботов."""
        if uid is None:
            return {**(self._one(self.last) if self.last else {}),
                    "state": "running" if self.running else ("finished" if self.robots else "idle"),
                    "max": robot_max(), "busy": self.busy(), "robots": [self._one(r) for r in self.robots.values()]}
        r = self.robots.get(uid)
        return self._one(r) if r else {"state": "idle"}

    def _one(self, r: Robot) -> dict:
        st = r.st
        labeled = self._live_entries(r) if st.transcript else []
        lines = transcript_to_text(labeled, merge=True).split("\n") if labeled else []
        return {"state": "running" if r.running else "finished", "status": st.status, "reason": st.end_reason,
                "meeting": {k: r.meta.get(k) for k in ("uid", "title", "link", "started_at")},
                "protocol_id": r.meta.get("protocol_id"), "participants": sorted(st.participants),
                "voices": self._voices(st), "replies": len(st.transcript), "log": st.log[-15:],
                "live": {"transcript": lines[-60:], **r.live}}

    @staticmethod
    def _voices(st: RobotState) -> dict[str, float]:
        """Кто сколько секунд говорил — по ленте vad из Телемоста."""
        end = max([t for t, _, _ in st.vad_events], default=0.0)
        return {st.people.get(pid, pid): round(sum(b - a for a, b in iv) / 1000, 1)
                for pid, iv in intervals(st.vad_events, end).items()}
