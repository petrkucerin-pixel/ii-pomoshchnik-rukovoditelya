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


def fake_runner(bridge_lines=(), participants=("Пит",), reason="встреча завершена", people=None, vad=None):
    calls = []

    async def run(link, *, name, token, until, st, shots_dir=None, headless=True):
        st.people.update(people or {})
        if callable(token):                      # сессия аватара создаётся после входа, с составом встречи
            token = token([n for n in st.people.values() if n != name])
        calls.append({"link": link, "name": name, "token": token, "until": until})
        st.started = 1000.0
        st.session_at = 1000.0
        st.vad_events = list(vad or [])
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


def test_link_checked(env):
    a, clock, llm, tmp = env
    rm = manager(a, FakeLA(), fake_runner(), tmp)
    with pytest.raises(ValueError):
        rm.start(link="http://evil.example/x", title="x")


def blocking_runner():
    """Робот «сидит» на встрече, пока его не остановят; по ходу копит реплики."""
    import asyncio as aio

    async def run(link, *, name, token, until, st, shots_dir=None, headless=True):
        st.people.update({"p1": "Пит"})
        if callable(token):
            token([n for n in st.people.values()])
        st.started = st.session_at = 1000.0
        st.status = "аватар во встрече"
        st.transcript = [{"role": "avatar", "text": "Начнём?", "t": 2000.0},
                         {"role": "user", "text": "Это Иван Петров. Анна отправит отчёт 12-го декабря.", "t": 6000.0}]
        st.vad_events = [(5500.0, "p1", True), (9000.0, "p1", False)]
        while not st.stop:
            await aio.sleep(0.02)
        st.end_reason = "остановлен"
        return st.transcript
    return run


def wait_until(cond, timeout=5.0):
    import time as t
    end = t.time() + timeout
    while t.time() < end:
        if cond():
            return True
        t.sleep(0.02)
    return False


def test_several_meetings_at_once_up_to_the_limit(env, monkeypatch):
    a, clock, llm, tmp = env
    monkeypatch.setenv("ROBOT_MAX", "2")
    rm = RobotManager(lambda: a, lambda: FakeLA(), threading.RLock(), avatar_id="x", shots_dir=tmp,
                      runner=blocking_runner(), threaded=True, transcript_wait_s=0, live=False)
    rm.start(link=LINK, title="Планёрка", uid="m1")
    rm.start(link=LINK + "1", title="Встреча с клиентом", uid="m2")
    assert wait_until(lambda: rm.is_running("m1") and rm.is_running("m2")) and rm.busy() == 2
    with pytest.raises(RuntimeError, match="уже на этой встрече"):
        rm.start(link=LINK, title="Планёрка", uid="m1")
    with pytest.raises(RuntimeError, match="предел"):
        rm.start(link=LINK + "2", title="Третья", uid="m3")
    assert rm.stop("m1") and wait_until(lambda: not rm.is_running("m1"))
    assert rm.is_running("m2")                                         # вторая встреча идёт дальше
    st = rm.status()
    assert st["busy"] == 1 and {r["meeting"]["uid"] for r in st["robots"]} == {"m1", "m2"}
    rm.stop("m2")
    assert wait_until(lambda: not rm.running)


def test_protocol_draft_fills_during_meeting(env):
    a, clock, llm, tmp = env
    rm = RobotManager(lambda: a, lambda: FakeLA(), threading.RLock(), avatar_id="x", shots_dir=tmp,
                      runner=blocking_runner(), threaded=True, transcript_wait_s=0, live=False)
    rm.start(link=LINK, title="Планёрка направления", uid=UID)
    assert wait_until(lambda: rm.robots[UID].st.transcript)
    robot = rm.robots[UID]
    live = rm.status(UID)["live"]
    assert "Пит: Это Иван Петров. Анна отправит отчёт 12-го декабря." in live["transcript"]   # кто сказал — уже по ходу
    assert live["protocol"] is None
    assert rm.live_tick(robot, force=True)                              # черновик по накопленной расшифровке
    draft = rm.status(UID)["live"]["protocol"]
    assert draft["tasks"][0]["owner"] == "Анна Волкова" and draft["tasks"][0]["due"].startswith("2026-12-12")
    assert not rm.live_tick(robot, force=True)                         # нового ничего не сказали — модель не зовём
    assert a.protocols.ids() == []                                     # черновик по ходу не создаёт протокол
    rm.stop(UID)
    assert wait_until(lambda: not rm.running) and a.protocols.ids()     # итоговый — после встречи


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



def test_speakers_labeled_and_alias_learned_then_used_next_time(env):
    a, clock, llm, tmp = env
    la = FakeLA(entries=[
        {"role": "avatar", "transcript": "Здравствуйте! Представьтесь, пожалуйста.", "relative_timestamp": 2},
        {"role": "user", "transcript": "Это Иван Петров, всё по плану.", "relative_timestamp": 8},
        {"role": "user", "transcript": "Анна отправит отчёт 12-го декабря.", "relative_timestamp": 20}])
    people = {"p1": "Пит", "p2": "Оля С.", "pb": "ИИ-помощник Ивана Петрова"}
    vad = [(1000 + 7000, "p1", True), (1000 + 11000, "p1", False), (1000 + 19000, "p2", True), (1000 + 23000, "p2", False)]
    rm = manager(a, la, fake_runner(people=people, vad=vad, participants=("Пит", "Оля С.")), tmp)
    rm.start(link=LINK, title="Планёрка направления", uid=UID)
    assert "Пит (пока не знаю" in la.prompts[0] and "Оля С. (пока не знаю" in la.prompts[0]
    sent = [u for s, u in llm.calls if "секретарь" in s][0]
    assert "Иван Петров: Это Иван Петров, всё по плану." in sent and "Оля С.: Анна отправит" in sent  # не представилась
    assert "Участники встречи в Телемосте: Иван Петров, Оля С." in sent
    assert any(x["kind"] == "person_alias" and x["payload"] == {"display": "Пит", "person": "Иван Петров"}
               for x in a.j.actions())
    rm.start(link=LINK, title="Планёрка направления", uid=UID)               # вторая встреча: уже знает «Пит»
    assert "Пит (это Иван Петров)" in la.prompts[1] and "Пит (пока не знаю" not in la.prompts[1]
