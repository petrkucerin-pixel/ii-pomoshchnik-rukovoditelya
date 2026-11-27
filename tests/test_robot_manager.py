"""TASK_11: робот Телемоста на стенде — запуск, протокол из полной расшифровки, ссылки встреч, автопилот."""
import shutil
import threading
from datetime import datetime

import pytest

from pomoshnik.assistant import Assistant
from pomoshnik.llm import StubLLM
from pomoshnik.memory import Memory
from pomoshnik.profile import load_profile
from pomoshnik.protocol import ProtocolStore
from pomoshnik.robot import transcript_text
from pomoshnik.robot.manager import RobotManager, links_from_journal, set_link
from pomoshnik.studio import Autopilot
from yadro.clock import TZ_EKB, FakeClock
from yadro.journal import Journal

from test_assistant import TASK, FakeMail, FakePult

LINK = "https://telemost.yandex.ru/j/11244974075676"
UID = "ev001@company.example"
PROTO = {"topics": [], "decisions": [], "open_questions": [],
         "tasks": [{"owner": "Анна", "what": "Отчёт по e-com", "due_text": "12-го декабря",
                    "quote": "Анна отправит отчёт 12-го декабря"}]}


class FakeLA:
    def __init__(self, entries=None):
        self.entries = entries if entries is not None else [
            {"role": "avatar", "transcript": "Здравствуйте! Кто отвечает за отчёт?", "relative_timestamp": 3},
            {"role": "user", "transcript": "Это Пит. Анна отправит отчёт", "relative_timestamp": 9},
            {"role": "user", "transcript": "12-го декабря.", "relative_timestamp": 12}]
        self.prompts, self.tokens = [], []

    def voice_id(self):
        return "voice-1"

    def create_context(self, name, prompt, opening):
        self.prompts.append(prompt)
        return "ctx-1"

    def create_embed(self, *a, **kw):
        raise AssertionError("роботу окно встраивания не нужно")

    def create_session_token(self, avatar_id, context_id, voice_id, language="ru"):
        self.tokens.append(context_id)
        return {"session_id": "sess-1", "session_token": "tok-1"}

    def transcript(self, session_id):
        assert session_id == "sess-1"
        return self.entries


def fake_runner(bridge_lines=(), participants=("Пит",), reason="встреча завершена"):
    calls = []

    async def run(link, *, name, token, until, st, shots_dir=None, headless=True):
        calls.append({"link": link, "name": name, "token": token, "until": until})
        st.started = 1000.0
        st.transcript = [{"role": r, "text": t, "t": 1000.0 + i * 1000} for i, (r, t) in enumerate(bridge_lines)]
        st.participants |= set(participants)
        st.end_reason = reason
        st.status = "завершено"
        return st.transcript
    run.calls = calls
    return run


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADER_NAME_GENITIVE", "Ивана Петрова")
    monkeypatch.delenv("DEMO_DATE", raising=False)
    d = tmp_path / "ivan"
    d.mkdir()
    shutil.copy(TASK / "leader_profile.md", d / "profile.md")
    clock = FakeClock(datetime(2026, 12, 7, 9, 57, tzinfo=TZ_EKB))
    j, ps, mem = Journal(tmp_path / "j.sqlite", clock), ProtocolStore(tmp_path / "p.sqlite", clock), \
        Memory(tmp_path / "m.sqlite", clock)
    llm = StubLLM(lambda s, u: PROTO if "секретарь" in s else {"facts": []})
    a = Assistant(load_profile(d, org_path=TASK / "org_structure.csv"), llm, FakePult(), FakeMail([]), j, ps,
                  calendar_path=TASK / "calendar.ics", tasks_path=TASK / "tasks.csv", memory=mem)
    yield a, clock, llm, tmp_path
    j.close(), ps.close(), mem.close()


def manager(a, la, runner, tmp_path):
    return RobotManager(lambda: a, lambda: la, threading.RLock(), avatar_id="dff45d08",
                        shots_dir=tmp_path, runner=runner, threaded=False, transcript_wait_s=0)


def kinds(a, kind):
    return [x for x in a.j.actions() if x["kind"] == kind]


def test_robot_meeting_gives_protocol_from_full_liveavatar_transcript(env):
    a, clock, llm, tmp = env
    la, run = FakeLA(), fake_runner(bridge_lines=[("user", "обрывок")])
    rm = manager(a, la, run, tmp)
    rm.start(link=LINK, title="Планёрка направления", uid=UID, minutes=30)
    assert run.calls[0]["token"] == "tok-1" and run.calls[0]["name"] == "ИИ-помощник Ивана Петрова"
    assert "Сергей Морозов" in la.prompts[0] and "не видишь, кто говорит" in la.prompts[0]   # участники и правило
    sent = [u for s, u in llm.calls if "секретарь" in s][0]
    assert "Участники встречи в Телемосте: Пит" in sent
    assert "Участник: Это Пит. Анна отправит отчёт 12-го декабря." in sent      # обрывки склеены, полный текст
    done = kinds(a, "robot_done")[0]["payload"]
    p = a.protocols.get(done["protocol_id"])
    assert p["meeting_uid"] == UID and done["source"] == "liveavatar" and done["reason"] == "встреча завершена"
    assert a.pult.sent[-1][2][0] == f"prot:ok:{done['protocol_id']}"
    st = rm.status()
    assert st["state"] == "finished" and st["protocol_id"] == done["protocol_id"] and st["participants"] == ["Пит"]


def test_bridge_transcript_is_fallback(env):
    a, clock, llm, tmp = env
    la = FakeLA(entries=[])
    rm = manager(a, la, fake_runner(bridge_lines=[("avatar", "Начнём?"), ("user", "Анна отправит отчёт 12-го декабря")]), tmp)
    rm.start(link=LINK, title="Планёрка направления", uid=UID)
    done = kinds(a, "robot_done")[0]["payload"]
    assert done["source"] == "bridge" and done["protocol_id"]


def test_no_talk_no_protocol(env):
    a, clock, llm, tmp = env
    rm = manager(a, FakeLA(entries=[]), fake_runner(reason="никто не пришёл", participants=()), tmp)
    rm.start(link=LINK, title="Планёрка направления", uid=UID)
    done = kinds(a, "robot_done")[0]["payload"]
    assert done["protocol_id"] is None and "никто не пришёл" in a.pult.sent[-1][1] and a.protocols.ids() == []


def test_only_one_robot_and_link_checked(env):
    a, clock, llm, tmp = env
    rm = manager(a, FakeLA(), fake_runner(), tmp)
    with pytest.raises(ValueError):
        rm.start(link="http://evil.example/x", title="x")
    rm.running = True
    with pytest.raises(RuntimeError):
        rm.start(link=LINK, title="x")


def test_links_live_in_journal(env):
    a, clock, llm, tmp = env
    set_link(a.j, UID, LINK)
    set_link(a.j, UID, LINK + "0")
    assert links_from_journal(a.j) == {UID: LINK + "0"}
    with pytest.raises(ValueError):
        set_link(a.j, UID, "javascript:alert(1)")


def test_transcript_text_of_bridge():
    t = transcript_text([{"role": "avatar", "text": "Привет", "t": 2000.0}, {"role": "user", "text": "Да", "t": 65000.0}], 1000.0)
    assert t.splitlines() == ["00:00:01", "ИИ-помощник: Привет", "00:01:04", "Участник: Да"]


def test_autopilot_sends_robot_when_meeting_has_link(env):
    a, clock, llm, tmp = env
    la, run = FakeLA(), fake_runner()
    rm = manager(a, la, run, tmp)
    set_link(a.j, UID, LINK)
    ap = Autopilot(lambda: a, lambda: la, avatar_id="x", base_url="http://stand", categories={"операционная"}, robot=rm)
    clock.advance(minutes=2)                     # 09:59
    ap.tick()
    assert run.calls and run.calls[0]["link"] == LINK
    ready = kinds(a, "avatar_auto_ready")[0]["payload"]
    assert ready["robot"] and ready["url"] == LINK and "Телемост" in a.pult.sent[0][1]
    clock.advance(minutes=40)
    ap.tick()
    done = kinds(a, "avatar_auto_done")
    assert len(done) == 1 and done[0]["payload"]["result"] == "protocol"


def test_only_avatar_spoke_means_no_protocol(env):
    a, clock, llm, tmp = env
    la = FakeLA(entries=[{"role": "avatar", "transcript": "Здравствуйте! Начнём?", "relative_timestamp": 3}])
    rm = manager(a, la, fake_runner(participants=(), reason="остановлен"), tmp)
    rm.start(link=LINK, title="Планёрка направления", uid=UID)
    assert kinds(a, "robot_done")[0]["payload"]["protocol_id"] is None and a.protocols.ids() == []
