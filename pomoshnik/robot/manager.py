"""Робот Телемоста на стенде (TASK_11): один робот за раз, протокол из полной расшифровки LiveAvatar.

Ссылки Телемоста у встреч хранятся в журнале (`meeting_link`), последняя запись главная.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
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


class RobotManager:
    def __init__(self, assistant, liveavatar, lock, *, avatar_id: str, shots_dir: Path | None = None,
                 runner=run_meeting, threaded: bool = True, transcript_wait_s: float = 15):
        self._a, self._la, self.lock = assistant, liveavatar, lock
        self.avatar_id, self.shots_dir, self.runner = avatar_id, shots_dir, runner
        self.threaded, self.wait_s = threaded, transcript_wait_s
        self.running = False
        self.st: RobotState | None = None
        self.meta: dict = {}

    def name(self) -> str:
        a = self._a()
        return f"ИИ-помощник {os.environ.get('LEADER_NAME_GENITIVE') or a.p.name}"

    def start(self, *, link: str, title: str, uid: str | None = None, minutes: int = 60) -> dict:
        from ..studio import prepare_avatar
        link = check_link(link)
        if self.running:
            raise RuntimeError("робот уже на встрече")
        a = self._a()
        self.st = RobotState()
        self.meta = {"uid": uid, "title": title, "link": link, "session_id": None,
                     "started_at": datetime.now(TZ_EKB).isoformat(timespec="seconds"), "protocol_id": None}
        self.running = True
        a.j.log_action("robot_started", path="now", summary=f"{title}: робот заходит в Телемост",
                       payload={k: self.meta[k] for k in ("uid", "title", "link")})

        def session_token(present: list[str]) -> str:
            """Вызывает робот после входа: контекст аватара с фактическим составом встречи и известными связками."""
            la = self._la()
            with self.lock:
                a = self._a()
                r = prepare_avatar(a, la, self.avatar_id, title, uid, embed=False,
                                   participants=self._who(a, present) or None)
            tok = la.create_session_token(self.avatar_id, r["context_id"], la.voice_id())
            self.meta["session_id"] = tok["session_id"]
            return tok["session_token"]

        args = (link, session_token, time.time() + minutes * 60)
        if self.threaded:
            threading.Thread(target=self._run, args=args, daemon=True).start()
        else:
            self._run(*args)
        return self.status()

    def stop(self) -> bool:
        if self.running and self.st:
            self.st.stop = True
            return True
        return False

    def _run(self, link: str, token: str, until: float) -> None:
        st = self.st
        try:
            asyncio.run(self.runner(link, name=self.name(), token=token, until=until, st=st, shots_dir=self.shots_dir))
        except Exception as e:                                  # браузер упал — протокол всё равно собираем
            st.note(f"робот упал: {type(e).__name__}: {str(e)[:200]}")
            st.end_reason = st.end_reason or "ошибка робота"
        try:
            self._finish(st)
        except Exception as e:
            log.exception("протокол после робота")
            st.note(f"протокол не собран: {type(e).__name__}: {str(e)[:200]}")
        finally:
            self.running = False

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

    def _entries(self, st: RobotState) -> tuple[list[dict], str]:
        """Полная расшифровка LiveAvatar (подождав, пока сессия закроется), иначе — то, что успел записать мост."""
        la = self._la()
        for attempt in range(4):
            entries = []
            if self.meta.get("session_id"):
                try:
                    entries = la.transcript(self.meta["session_id"])
                except LiveAvatarError as e:
                    st.note(f"расшифровка LiveAvatar недоступна: {e}")
            if entries:
                return entries, "liveavatar"
            if attempt < 3 and self.wait_s:
                time.sleep(self.wait_s)
        t0 = st.session_at or st.started
        return [{"role": x["role"], "transcript": x["text"], "relative_timestamp": (x["t"] - t0) / 1000}
                for x in st.transcript], "bridge"

    def _finish(self, st: RobotState) -> None:
        entries, source = self._entries(st)
        a = self._a()
        # кто говорил: лента vad из Телемоста; новые связки «подпись → человек» запоминаем
        labeled = label_entries(entries, st.vad_events, st.session_at or st.started, st.people,
                                robot_name=self.name(), aliases=aliases_from_journal(a.j))
        if learn_aliases(a.j, labeled, a.p.org):
            labeled = label_entries(entries, st.vad_events, st.session_at or st.started, st.people,
                                    robot_name=self.name(), aliases=aliases_from_journal(a.j))
        text = transcript_to_text(labeled, merge=True)
        aliases = aliases_from_journal(a.j)
        st.participants = {aliases.get(n) or resolve_name(n, a.p.org) or n for n in st.participants}
        pid = None
        if any(e.get("role") != "avatar" for e in labeled):   # говорил только аватар — встречи не было
            people = ", ".join(sorted(st.participants)) or "не определены"
            body = f"Участники встречи в Телемосте: {people}\n\n{text}"
            with self.lock:
                pid = a.new_meeting(body, self.meta["title"], datetime.now(TZ_EKB), meeting_uid=self.meta["uid"])
        else:
            a.pult.send_text(f"«{self.meta['title']}»: разговора не было ({st.end_reason or 'робот вышел'}), протокола нет.")
        self.meta["protocol_id"] = pid
        a.j.log_action("robot_done", path="now",
                       summary=f"{self.meta['title']}: {st.end_reason or 'завершено'}, протокол {pid or 'нет'}",
                       payload={"uid": self.meta["uid"], "session_id": self.meta["session_id"], "protocol_id": pid,
                                "reason": st.end_reason, "source": source, "participants": sorted(st.participants)})

    def status(self) -> dict:
        if not self.st:
            return {"state": "idle"}
        st = self.st
        return {"state": "running" if self.running else "finished", "status": st.status, "reason": st.end_reason,
                "meeting": {k: self.meta.get(k) for k in ("uid", "title", "link", "started_at")},
                "protocol_id": self.meta.get("protocol_id"), "participants": sorted(st.participants),
                "voices": self._voices(st), "replies": len(st.transcript), "log": st.log[-15:]}

    @staticmethod
    def _voices(st: RobotState) -> dict[str, float]:
        """Кто сколько секунд говорил — по ленте vad из Телемоста."""
        end = max([t for t, _, _ in st.vad_events], default=0.0)
        return {st.people.get(pid, pid): round(sum(b - a for a, b in iv) / 1000, 1)
                for pid, iv in intervals(st.vad_events, end).items()}
