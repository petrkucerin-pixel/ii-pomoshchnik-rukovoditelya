"""TASK_09: аватар включается сам по календарю, студия, протокол после встречи без кнопки."""
import shutil
from datetime import datetime

import pytest

from pomoshnik.assistant import Assistant
from pomoshnik.avatar import LiveAvatarError, Session
from pomoshnik.llm import StubLLM
from pomoshnik.memory import Memory
from pomoshnik.profile import load_profile
from pomoshnik.protocol import ProtocolStore
from pomoshnik.studio import Autopilot
from yadro.clock import TZ_EKB, FakeClock
from yadro.journal import Journal

from test_assistant import PROTO, TASK, FakeMail, FakePult

START = datetime(2026, 12, 7, 9, 57, tzinfo=TZ_EKB)       # планёрка направления 07.12 10:00–10:30
UID = "ev001@company.example"


class FakeLA:
    def __init__(self):
        self.contexts, self.embeds, self.finished, self.fail = [], [], [], False
        self.lines = [{"role": "user", "transcript": "Сергей, договор с «Облако Плюс» нужно подписать к пятнице.",
                       "relative_timestamp": 5}]

    def _check(self):
        if self.fail:
            raise LiveAvatarError("LiveAvatar недоступен (ConnectError)")

    def voice_id(self):
        return "voice-1"

    def create_context(self, name, prompt, opening):
        self._check()
        self.contexts.append(prompt)
        return f"ctx-{len(self.contexts)}"

    def create_embed(self, avatar_id, context_id, voice_id, **kw):
        self._check()
        self.embeds.append(kw)
        n = len(self.embeds)
        return {"embed_id": f"emb-{n}", "url": f"https://embed.liveavatar.com/v1/emb-{n}"}

    def sessions(self, *, embed_id=None, historic=True):
        self._check()
        return [s for s in self.finished if embed_id in s.id] if historic else []

    def transcript(self, session_id):
        return self.lines


@pytest.fixture
def env(tmp_path):
    d = tmp_path / "ivan"
    d.mkdir()
    shutil.copy(TASK / "leader_profile.md", d / "profile.md")
    clock = FakeClock(START)
    j, ps, mem = Journal(tmp_path / "j.sqlite", clock), ProtocolStore(tmp_path / "p.sqlite", clock), \
        Memory(tmp_path / "m.sqlite", clock)
    llm = StubLLM(lambda s, u: PROTO if "секретарь" in s else {"facts": []})
    a = Assistant(load_profile(d, org_path=TASK / "org_structure.csv"), llm, FakePult(), FakeMail([]), j, ps,
                  calendar_path=TASK / "calendar.ics", tasks_path=TASK / "tasks.csv", memory=mem)
    la = FakeLA()
    ap = Autopilot(lambda: a, lambda: la, avatar_id="dff45d08", base_url="http://stand", categories={"операционная"})
    yield ap, a, la, clock
    j.close(), ps.close(), mem.close()


def kinds(a, kind):
    return [x for x in a.j.actions() if x["kind"] == kind]


def test_prepares_one_minute_before_once_and_tells_leader(env):
    ap, a, la, clock = env
    ap.tick()
    assert not la.contexts and ap.state()["state"] == "idle"
    clock.advance(minutes=2)                                    # 09:59
    ap.tick()
    ap.tick()
    assert len(la.contexts) == 1 and len(kinds(a, "avatar_auto_ready")) == 1
    assert "ИИ-помощник" in la.contexts[0] and "Сергей Морозов" in la.contexts[0]   # задачи участников в контексте
    assert la.embeds[0]["max_seconds"] == (30 + 10) * 60
    msg = a.pult.sent[-1][1]
    assert "Планёрка направления" in msg and "http://stand/studio" in msg and "screen" in msg
    st = ap.state()
    assert st["state"] == "ready" and st["open_now"] and st["url"].startswith("https://embed.liveavatar.com/")
    assert st["meeting"]["uid"] == UID and st["screen_url"].endswith(UID)


def test_other_categories_are_skipped(env):
    ap, a, la, clock = env
    ap.categories = {"клиент"}
    clock.advance(minutes=2)
    ap.tick()
    assert not la.contexts


def test_protocol_made_itself_after_meeting(env):
    ap, a, la, clock = env
    clock.advance(minutes=2)
    ap.tick()
    clock.advance(minutes=32)                                   # 10:31, встреча закончилась
    la.finished.append(Session("sess-emb-1", "2026-12-07T05:00:00", "2026-12-07T05:30:30", 1830, "embed"))
    ap.tick()
    ap.tick()
    pid = kinds(a, "avatar_auto_done")[0]["payload"]["protocol_id"]
    assert len(kinds(a, "avatar_auto_done")) == 1 and a.protocols.get(pid)["meeting_uid"] == UID
    assert a.pult.sent[-1][2][0] == f"prot:ok:{pid}"            # черновик ушёл на утверждение
    assert ap.state()["state"] == "done" and not ap.state()["open_now"]


def test_no_session_gives_up_after_grace(env):
    ap, a, la, clock = env
    clock.advance(minutes=2)
    ap.tick()
    clock.advance(minutes=35)
    ap.tick()
    assert not kinds(a, "avatar_auto_done")                     # ещё ждём: сессия может закрыться позже
    clock.advance(minutes=20)
    ap.tick()
    done = kinds(a, "avatar_auto_done")
    assert len(done) == 1 and done[0]["payload"]["result"] == "no_session"
    assert "без аватара" in a.pult.sent[-1][1] and a.protocols.ids() == []


def test_liveavatar_failure_is_logged_and_retried(env):
    ap, a, la, clock = env
    clock.advance(minutes=2)
    la.fail = True
    ap.tick()
    assert kinds(a, "avatar_auto_error") and not kinds(a, "avatar_auto_ready")
    la.fail = False
    clock.advance(minutes=1)
    ap.tick()
    assert len(kinds(a, "avatar_auto_ready")) == 1


def test_state_survives_restart(env):
    ap, a, la, clock = env
    clock.advance(minutes=2)
    ap.tick()
    again = Autopilot(lambda: a, lambda: la, avatar_id="x", base_url="http://stand", categories={"операционная"})
    again.tick()
    assert len(la.contexts) == 1 and again.state()["state"] == "ready"
