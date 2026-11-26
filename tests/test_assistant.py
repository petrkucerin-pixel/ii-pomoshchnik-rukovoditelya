import csv
import shutil
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from pomoshnik.assistant import Assistant
from pomoshnik.channels.telegram import Update
from pomoshnik.llm import StubLLM
from pomoshnik.profile import load_profile
from pomoshnik.protocol import ProtocolStore
from yadro.clock import TZ_EKB, FakeClock
from yadro.journal import Journal

ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "data" / "task"
ROWS = list(csv.DictReader(open(TASK / "inbox.csv", encoding="utf-8")))
BY_SUBJECT = {r["subject"]: r["category"] for r in ROWS}
START = datetime(2026, 12, 7, 8, 30, tzinfo=TZ_EKB)

TRANSCRIPT = "Сергей, договор с «Облако Плюс» нужно подписать к пятнице. Ольга подготовит КП для «ТехноСтрой» до конца месяца."
PROTO = {"topics": [{"title": "Партнёры", "points": ["Договор"]}], "decisions": [],
         "tasks": [{"owner": "Сергей Морозов", "what": "Подписать договор", "due_text": "к пятнице",
                    "quote": "договор с «Облако Плюс» нужно подписать к пятнице"},
                   {"owner": "Ольга", "what": "КП для «ТехноСтрой»", "due_text": "до конца месяца",
                    "quote": "Ольга подготовит КП для «ТехноСтрой»"}],
         "open_questions": ["Бюджет не решён"]}


class FakePult:
    def __init__(self):
        self.sent = []
        self.n = 0

    def send_text(self, text):
        self.n += 1
        self.sent.append(("text", text, None))
        return self.n

    def send_buttons(self, text, buttons):
        self.n += 1
        self.sent.append(("buttons", text, [b.data for row in buttons for b in row]))
        return self.n

    def answer_callback(self, cid, text=""):
        pass


class FakeMail:
    def __init__(self, rows):
        self.rows, self.out, self.given = rows, [], False

    def fetch_new(self):
        if self.given:
            return []
        self.given = True
        return [SimpleNamespace(id=r["id"], from_name=r["from_name"], from_email=r["from_email"], subject=r["subject"],
                                body=r["body"],
                                received=datetime.fromisoformat(r["received"]).replace(tzinfo=TZ_EKB))
                for r in self.rows]

    def send(self, to, subject, body, in_reply_to=None):
        self.out.append({"to": to, "subject": subject, "body": body, "in_reply_to": in_reply_to})
        return f"<{len(self.out)}@test>"


def llm_answer(system, user):
    if "секретарь" in system:
        return PROTO
    subj = user.split("Тема: ", 1)[1].split("\n", 1)[0]
    return {"category": BY_SUBJECT.get(subj, ""), "summary": subj, "confidence": 0.9}


@pytest.fixture
def a(tmp_path):
    d = tmp_path / "ivan"
    d.mkdir()
    shutil.copy(TASK / "leader_profile.md", d / "profile.md")
    clock = FakeClock(START)
    j = Journal(tmp_path / "j.sqlite", clock)
    ps = ProtocolStore(tmp_path / "p.sqlite", clock)
    mail = FakeMail(ROWS)
    asst = Assistant(load_profile(d, org_path=TASK / "org_structure.csv"), StubLLM(llm_answer), FakePult(), mail, j, ps,
                     calendar_path=TASK / "calendar.ics", tasks_path=TASK / "tasks.csv")
    yield asst, clock, mail
    j.close()
    ps.close()


def cb(data):
    return Update(1, "callback", 555, data, 10, "cb1")


def txt(text):
    return Update(2, "text", 555, text, 11)


def test_inbox_actions_and_idempotent(a):
    asst, _, mail = a
    c = asst.process_inbox()
    assert sum(v for k, v in c.items() if k != "ревизор") == 80 and c["спросить_руководителя"] >= 40
    delegated = [m for m in mail.out if m["subject"].startswith("Fwd:")]
    assert delegated and all(m["to"] in ("p02@company.example", "p06@company.example") for m in delegated)
    assert any(m["subject"].startswith("Re:") for m in mail.out)            # авто: перенос встречи
    assert asst.process_inbox() == {}                                        # повтор не обрабатывается


def test_guarded_letters_never_answered_automatically(a):
    asst, _, mail = a
    asst.process_inbox()
    guarded = {r["from_email"] for r in ROWS if r["category"] in ("деньги", "кадры", "обязательства_клиентам", "руководство")}
    guarded_subjects = {r["subject"] for r in ROWS if r["category"] in ("деньги", "кадры", "обязательства_клиентам", "руководство")}
    assert not [m for m in mail.out if m["subject"].removeprefix("Re: ").removeprefix("Fwd: ") in guarded_subjects]
    assert {x["subject"] for x in asst.decisions} >= guarded_subjects


def test_morning_plan_and_decision_by_buttons(a):
    asst, _, mail = a
    asst.process_inbox()
    asst.morning_plan(date(2026, 12, 7))
    kind, text, buttons = asst.pult.sent[-1]
    assert "Ждут только вас" in text and buttons == ["dec:next"]
    asst.handle(cb("dec:next"))
    first = asst.decisions[0]
    assert asst.pult.sent[-1][2][0] == f"dec:yes:{first['event_id']}"
    before = len(mail.out)
    asst.handle(cb(f"dec:yes:{first['event_id']}"))
    assert len(mail.out) == before + 1 and mail.out[-1]["to"] == first["email"]
    assert first not in asst.decisions
    assert any(x["actor"] == "human" for x in asst.j.actions())


def test_protocol_draft_edit_approve_send(a):
    asst, clock, mail = a
    end = clock.now()
    clock.advance(minutes=1)
    pid = asst.new_meeting(TRANSCRIPT, "Планёрка", end)
    assert asst.pult.sent[-1][2] == [f"prot:ok:{pid}", f"prot:edit:{pid}", f"prot:no:{pid}"]
    assert "Сергей Морозов" in asst.pult.sent[-1][1]
    asst.handle(cb(f"prot:edit:{pid}"))
    asst.handle(txt("Срок по договору — пятница, без изменений"))
    assert asst.pult.sent[-1][2][0] == f"prot:ok:{pid}"                   # протокол пришёл заново
    assert asst.protocols.leader_rules() == ["Срок по договору — пятница, без изменений"]
    clock.advance(minutes=2)
    asst.handle(cb(f"prot:ok:{pid}"))
    sent = [m for m in mail.out if m["subject"] == "Протокол: Планёрка"]
    assert {m["to"] for m in sent} == {"p05@company.example", "p02@company.example"}
    assert asst.protocols.get(pid)["minutes_to_approved"] == pytest.approx(3)
    assert sum(1 for x in asst.j.actions() if x["kind"] == "task_assigned") == 2
    assert asst.approve_protocol(pid) == 0                                    # повторно не рассылается


def test_questions_from_last_approved(a):
    asst, clock, _ = a
    pid = asst.new_meeting(TRANSCRIPT, "Планёрка", clock.now())
    asst.handle(txt("/voprosy"))
    assert "пока нет" in asst.pult.sent[-1][1]
    asst.handle(cb(f"prot:ok:{pid}"))
    asst.handle(txt("/voprosy"))
    assert "Подписать договор" in asst.pult.sent[-1][1] and "Бюджет не решён" in asst.pult.sent[-1][1]


def test_demo_date_moves_plan_to_task_calendar(a, monkeypatch):
    asst, _, _ = a
    monkeypatch.setenv("DEMO_DATE", "2026-12-08")
    asst.handle(txt("/plan"))
    assert "08.12" in asst.pult.sent[-1][1] and "Встречи (0)" not in asst.pult.sent[-1][1]


def test_reviser_runs_after_inbox_writes_people_not_leader(a, monkeypatch):
    asst, _, mail = a
    monkeypatch.setenv("DEMO_DATE", "2026-12-11")
    c = asst.process_inbox()
    questions = [m for m in mail.out if m["subject"].startswith("Вопрос по отчёту")]
    assert c["ревизор"] >= 6 and questions
    assert any("Облако Плюс" in m["subject"] for m in questions)
    assert any("риск" in r.lower() or "под риском" in r for r in asst.risks)
    assert all(m["to"] != "p01@company.example" for m in questions)
    assert asst.review_reports() == 0                 # повторно в тот же день не спрашивает


# ---------------- TASK_06: память помощника ----------------
def llm_with_memory(system, user):
    if "память помощника" in system:
        if "ждём до 12.12" in user:
            return {"facts": [{"topic": "Облако Плюс", "kind": "партнёр", "what": "договор у юристов, ждём до 12.12",
                               "quote": "Договор на подписи у юристов"}]}
        if "ТехноСтрой" in user:
            return {"facts": [{"topic": "ТехноСтрой", "kind": "сделка", "what": "Ольга готовит КП до конца месяца",
                               "who": "Ольга", "quote": "Ольга подготовит КП для «ТехноСтрой»"}]}
        return {"facts": []}
    return llm_answer(system, user)


@pytest.fixture
def am(tmp_path):
    from pomoshnik.memory import Memory
    d = tmp_path / "ivan"
    d.mkdir()
    shutil.copy(TASK / "leader_profile.md", d / "profile.md")
    clock = FakeClock(START)
    j, ps, mem = Journal(tmp_path / "j.sqlite", clock), ProtocolStore(tmp_path / "p.sqlite", clock), \
        Memory(tmp_path / "m.sqlite", clock)
    llm = StubLLM(llm_with_memory)
    asst = Assistant(load_profile(d, org_path=TASK / "org_structure.csv"), llm, FakePult(), FakeMail(ROWS), j, ps,
                     calendar_path=TASK / "calendar.ics", tasks_path=TASK / "tasks.csv", memory=mem)
    yield asst, clock, llm
    j.close()
    ps.close()
    mem.close()


def test_inbox_goes_to_memory_and_leader_asks_by_topic(am):
    asst, _, llm = am
    asst.process_inbox()
    asst.handle(txt("что по Облако Плюс"))
    reply = asst.pult.sent[-1][1]
    assert "12.12" in reply and "Сергей Морозов" in reply and "m022" in reply and "Договор на подписи у юристов" in reply
    assert reply.count("•") == 1 and "08.12–11.12, 7 раз без изменений" in reply     # семь одинаковых статусов — одна строка
    newsletters = {r["subject"] for r in ROWS if r["category"] == "рассылка"}
    memory_calls = [u for s, u in llm.calls if "память помощника" in s]
    assert memory_calls and not [u for u in memory_calls if u.split("Тема: ", 1)[1].split("\n", 1)[0] in newsletters]
    assert any(x["kind"] == "memory_saved" for x in asst.j.actions())


def test_memory_command_without_hits_and_without_topic(am):
    asst, _, _ = am
    asst.handle(txt("/chto Марс"))
    assert "ничего нет" in asst.pult.sent[-1][1]
    asst.handle(txt("/chto"))
    assert "Напишите тему" in asst.pult.sent[-1][1]


def test_approved_protocol_goes_to_memory_draft_does_not(am):
    asst, clock, _ = am
    pid = asst.new_meeting(TRANSCRIPT, "Планёрка", clock.now())
    assert not asst.memory.search("ТехноСтрой")
    asst.handle(cb(f"prot:ok:{pid}"))
    f = asst.memory.search("ТехноСтрой")
    assert f and f[0].source == f"протокол {pid}" and f[0].who == "Ольга"


def test_meeting_brief_has_history_and_revisor_questions_and_survives_restart(am, monkeypatch, tmp_path):
    asst, clock, llm = am
    monkeypatch.setenv("DEMO_DATE", "2026-12-11")
    asst.process_inbox()
    history, questions = asst.meeting_brief()
    assert "Облако Плюс" in history and "12.12" in history and "m022" in history
    assert any("Облако Плюс" in q and q.startswith("Сергей Морозов") for q in questions)
    again = Assistant(asst.p, llm, FakePult(), FakeMail([]), asst.j, asst.protocols,          # перезапуск стенда
                      calendar_path=TASK / "calendar.ics", tasks_path=TASK / "tasks.csv", memory=asst.memory)
    assert again.meeting_brief() == (history, questions)


def test_meeting_brief_old_revisor_questions_drop_off(am, monkeypatch):
    asst, clock, _ = am
    monkeypatch.setenv("DEMO_DATE", "2026-12-11")
    asst.process_inbox()
    monkeypatch.setenv("DEMO_DATE", "2026-12-25")
    assert asst.meeting_brief()[1] == []


# ---------------- ревизор переживает перезапуск ----------------
def test_reviser_reports_survive_restart(am, monkeypatch):
    asst, clock, llm = am
    monkeypatch.setenv("DEMO_DATE", "2026-12-11")
    asst.process_inbox()
    n = len(asst.reports)
    assert n >= 15
    again = Assistant(asst.p, llm, FakePult(), FakeMail([]), asst.j, asst.protocols,
                      calendar_path=TASK / "calendar.ics", tasks_path=TASK / "tasks.csv", memory=asst.memory)
    assert len(again.reports) == n
    assert {r.id for r in again.reports} == {r.id for r in asst.reports}
    assert again.review_reports() == 0                     # тот же день — повторно не спрашивает
    monkeypatch.setenv("DEMO_DATE", "2026-12-12")
    assert again.review_reports() > 0                      # новый день — видит отчёты, разобранные до перезапуска
