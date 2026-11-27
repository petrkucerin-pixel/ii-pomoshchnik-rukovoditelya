"""TASK_13: кому рассылать протокол, сверка с прошлым протоколом, порядок задач (ответ заказчика 27.11, РАЙДЕР п. 22)."""
import csv
import shutil
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from pomoshnik.assistant import Assistant, protocol_tasks
from pomoshnik.channels.telegram import Update
from pomoshnik.llm import StubLLM
from pomoshnik.org import Org
from pomoshnik.profile import load_profile
from pomoshnik.protocol import ProtocolStore, build_protocol, questions_from_previous, render_markdown
from yadro.clock import TZ_EKB, FakeClock
from yadro.journal import Journal

ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "data" / "task"
ORG = Org.load(TASK / "org_structure.csv")
END = datetime(2026, 12, 7, 10, 30, tzinfo=TZ_EKB)          # конец планёрки ev001, понедельник

T1 = """Сергей, договор с «Облако Плюс» нужно подписать к пятнице.
Ольга подготовит коммерческое предложение для «ТехноСтрой» до 20 декабря.
Анна, отчёт по e-com за ноябрь к среде."""
P1 = {"topics": [], "decisions": [], "open_questions": [],
      "tasks": [{"owner": "Сергей Морозов", "what": "Подписать договор с «Облако Плюс»", "due_text": "к пятнице",
                 "quote": "договор с «Облако Плюс» нужно подписать к пятнице"},
                {"owner": "Ольга", "what": "КП для «ТехноСтрой»", "due_text": "до 20 декабря",
                 "quote": "Ольга подготовит коммерческое предложение для «ТехноСтрой»"},
                {"owner": "Анна", "what": "Отчёт по e-com за ноябрь", "due_text": "к среде",
                 "quote": "Анна, отчёт по e-com за ноябрь к среде"}]}

T2 = """Сергей: договор с «Облако Плюс» подписали в четверг.
Ольга: по «ТехноСтрой» клиент просит КП не на 50, а на 30 лицензий, сделаю до 18 декабря.
Анна: отчёт ещё собираю, данные пришли вчера."""
P2 = {"topics": [], "decisions": [], "open_questions": [], "tasks": [],
      "previous": [{"n": 1, "status": "выполнена", "quote": "договор с «Облако Плюс» подписали в четверг"},
                   {"n": 2, "status": "изменилась", "new_what": "КП для «ТехноСтрой» на 30 лицензий",
                    "new_due_text": "до 18 декабря", "quote": "клиент просит КП не на 50, а на 30 лицензий"},
                   {"n": 9, "status": "выполнена", "quote": "нет такой задачи"},          # чужой номер — отброшен
                   {"n": 3, "status": "куда-то делась", "quote": "отчёт ещё собираю"}]}  # неизвестный статус

# Пример заказчика: выделить вендоров к Q3 — аналитик, витрина конкурентов, переговоры, часть параллельно.
T3 = """Алексей, зафиксируй метрики по вендорам до 15 декабря.
Дмитрий, проанализируй витрину конкурентов до 15 декабря.
Ольга, после метрик и витрины проведи переговоры с вендорами об акции до 22 декабря.
Сергей, подготовь список вендоров к продвижению в третьем квартале до 12 декабря."""
P3 = {"topics": [], "decisions": [], "open_questions": [],
      "tasks": [{"owner": "Алексей", "what": "Зафиксировать метрики по вендорам", "due_text": "до 15 декабря",
                 "quote": "зафиксируй метрики по вендорам до 15 декабря"},
                {"owner": "Дмитрий", "what": "Анализ витрины конкурентов", "due_text": "до 15 декабря",
                 "quote": "проанализируй витрину конкурентов до 15 декабря"},
                {"owner": "Ольга", "what": "Переговоры с вендорами об акции", "due_text": "до 22 декабря",
                 "quote": "после метрик и витрины проведи переговоры с вендорами", "after": [1, 2]},
                {"owner": "Сергей", "what": "Список вендоров к Q3", "due_text": "до 12 декабря",
                 "quote": "подготовь список вендоров к продвижению", "after": [3, 4, 0, "x", [2], None]}]}  # срок раньше; мусор


# ---------------- протокол: сверка с прошлым ----------------
def test_previous_tasks_go_to_model_and_answers_are_checked():
    prev = build_protocol(StubLLM(default=P1), T1, title="Планёрка", meeting_end=END, org=ORG)
    llm = StubLLM(default=P2)
    p = build_protocol(llm, T2, title="Планёрка", meeting_end=END.replace(day=14), org=ORG, previous=prev, previous_id=7)
    user = llm.calls[0][1]
    assert "1. Сергей Морозов: Подписать договор с «Облако Плюс»" in user and "3. Анна Волкова" in user
    by_n = {x["n"]: x for x in p.previous}
    assert set(by_n) == {1, 2, 3}                                    # номер 9 отброшен, каждая старая задача есть
    assert by_n[1]["status"] == "выполнена" and by_n[1]["quote_ok"]
    assert by_n[2]["status"] == "изменилась" and by_n[2]["new_what"].endswith("30 лицензий")
    assert by_n[2]["new_due"].date().isoformat() == "2026-12-18" and by_n[2]["was_what"] == "КП для «ТехноСтрой»"
    assert by_n[3]["status"] == "не обсуждали"                       # неизвестный статус не выдумываем
    assert p.previous_id == 7 and p.changes() == [by_n[2]]


def test_previous_section_in_markdown_and_questions_ask_about_changes():
    prev = build_protocol(StubLLM(default=P1), T1, title="Планёрка", meeting_end=END, org=ORG)
    p = build_protocol(StubLLM(default=P2), T2, title="Планёрка", meeting_end=END.replace(day=14), org=ORG,
                       previous=prev, previous_id=7)
    md = render_markdown(p)
    assert "## По прошлому протоколу" in md and "выполнена" in md
    assert "было «КП для «ТехноСтрой»» до 20.12.2026 → стало «КП для «ТехноСтрой» на 30 лицензий» до 18.12.2026" in md
    q = questions_from_previous(prev)
    assert all("поменял" in x for x in q[:3])                       # статус, решено ли, не поменялась ли задача


def test_previous_survives_store_and_edit(tmp_path):
    clock = FakeClock(END)
    s = ProtocolStore(tmp_path / "p.sqlite", clock)
    prev = build_protocol(StubLLM(default=P1), T1, title="Планёрка", meeting_end=END, org=ORG)
    p = build_protocol(StubLLM(default=P2), T2, title="Планёрка", meeting_end=END, org=ORG, previous=prev, previous_id=1)
    pid = s.save_draft(p, T2)
    assert s.protocol(pid, ORG).changes()[0]["n"] == 2
    edited = {k: v for k, v in P2.items() if k != "previous"}       # модель при правке потеряла сверку
    q = s.apply_edit(pid, "без изменений", StubLLM(default=edited), ORG)
    assert q.previous_id == 1 and q.changes()[0]["n"] == 2
    s.close()


# ---------------- протокол: порядок задач ----------------
def test_dependencies_checked_and_rendered_as_steps():
    p = build_protocol(StubLLM(default=P3), T3, title="Вендоры Q3", meeting_end=END, org=ORG)
    assert [t.after for t in p.tasks] == [[], [], [1, 2], [3]]      # вперёд, ноль, себя и мусор — отброшены
    assert "срок раньше, чем у задачи 3" in p.tasks[3].flags
    assert any("Задача 4" in q and "после задачи 3" in q for q in p.open_questions)
    md = render_markdown(p)
    assert "Ольга Смирнова: Переговоры с вендорами об акции — до 22.12.2026 (после 1, 2)" in md
    assert "Шаг 1: задачи 1, 2 — параллельно" in md and "Шаг 2: задача 3" in md and "Шаг 3: задача 4" in md


# ---------------- помощник: рассылка, сверка, «можно начинать» ----------------
class Pult:
    def __init__(self):
        self.sent = []

    def send_text(self, text):
        self.sent.append(("text", text, None))
        return len(self.sent)

    def send_buttons(self, text, buttons):
        self.sent.append(("buttons", text, [b.data for row in buttons for b in row]))
        return len(self.sent)

    def answer_callback(self, *a):
        pass


class Mail:
    def __init__(self):
        self.out = []

    def fetch_new(self):
        return []

    def send(self, to, subject, body, in_reply_to=None):
        self.out.append({"to": to, "subject": subject, "body": body})
        return f"<{len(self.out)}>"


@pytest.fixture
def a(tmp_path):
    d = tmp_path / "ivan"
    d.mkdir()
    shutil.copy(ROOT / "profiles" / "ivan_petrov" / "profile.md", d / "profile.md")
    clock = FakeClock(END)
    j = Journal(tmp_path / "j.sqlite", clock)
    ps = ProtocolStore(tmp_path / "p.sqlite", clock)
    answers = {}
    llm = StubLLM(lambda s, u: answers["next"] if "секретарь" in s else {"facts": []})
    asst = Assistant(load_profile(d, org_path=TASK / "org_structure.csv"), llm, Pult(), Mail(), j, ps,
                     calendar_path=TASK / "calendar.ics", tasks_path=TASK / "tasks.csv")
    asst._answers = answers
    yield asst, clock
    j.close()
    ps.close()


def meeting(asst, clock, transcript, answer, *, title="Планёрка направления", uid="ev001@company.example", approve=True):
    asst._answers["next"] = answer
    pid = asst.new_meeting(transcript, title, clock.now(), meeting_uid=uid)
    if approve:
        asst.handle(Update(1, "callback", 555, f"prot:ok:{pid}", 10, "cb"))
    return pid


def sent_protocols(asst, pid):
    title = asst.protocols.get(pid)["title"]
    return {m["to"] for m in asst.send_mail.out if m["subject"] == f"Протокол: {title}"}


def test_protocol_goes_to_all_attendees_and_owners_not_leader(a):
    asst, clock = a
    pid = meeting(asst, clock, T1, P1)
    to = sent_protocols(asst, pid)
    attendees = {"p02", "p03", "p04", "p05", "p06", "p07", "p08"}    # участники ev001 по календарю
    assert to == {f"{x}@company.example" for x in attendees}
    assert "p01@company.example" not in to
    pid2 = meeting(asst, clock, T3, P3, title="Вендоры Q3", uid=None)  # встреча не из календаря — исполнители
    assert sent_protocols(asst, pid2) == {"p07@company.example", "p03@company.example", "p02@company.example",
                                          "p05@company.example"}


def test_next_meeting_reconciles_previous_and_leader_approves_changes(a):
    asst, clock = a
    meeting(asst, clock, T1, P1)
    clock.advance(days=7)
    pid2 = meeting(asst, clock, T2, P2, approve=False)
    card = asst.pult.sent[-1][1]
    assert "Изменились задачи прошлого протокола: 1" in card and "30 лицензий" in card
    assert asst.pult.sent[-1][2][0] == f"prot:ok:{pid2}"
    asst.handle(Update(1, "callback", 555, f"prot:ok:{pid2}", 10, "cb"))
    tasks = {t["what"]: t for t in protocol_tasks(asst.j)}
    assert tasks["Подписать договор с «Облако Плюс»"]["status"] == "выполнена"
    kp = tasks["КП для «ТехноСтрой» на 30 лицензий"]
    assert kp["due"].isoformat() == "2026-12-18" and kp["status"] == "изменена"
    assert tasks["Отчёт по e-com за ноябрь"]["status"] == "новая"
    mail = [m for m in asst.send_mail.out if m["subject"] == "Протокол: Планёрка направления"][-1]["body"]
    assert "По прошлому протоколу" in mail


def test_previous_protocol_is_same_meeting_series(a):
    asst, clock = a
    meeting(asst, clock, T1, P1)
    clock.advance(days=1)
    meeting(asst, clock, T3, P3, title="Вендоры Q3", uid=None)
    clock.advance(days=6)
    meeting(asst, clock, T2, P2, approve=False)
    user = asst.llm.calls[-1][1]
    assert "Подписать договор с «Облако Плюс»" in user and "витрин" not in user


def test_dependent_task_waits_then_owner_gets_go(a):
    asst, clock = a
    pid = meeting(asst, clock, T3, P3, title="Вендоры Q3", uid=None)
    st = {t["n"]: t for t in protocol_tasks(asst.j) if t["protocol_id"] == pid}
    assert st[3]["status"] == "ждёт задачи 1, 2" and st[1]["status"] == "новая"
    clock.advance(days=7)
    done = {"topics": [], "decisions": [], "open_questions": [], "tasks": [],
            "previous": [{"n": 1, "status": "выполнена", "quote": "метрики по вендорам зафиксировал"},
                         {"n": 2, "status": "выполнена", "quote": "витрину конкурентов разобрал полностью"}]}
    before = len(asst.send_mail.out)
    meeting(asst, clock, "Алексей: метрики по вендорам зафиксировал. Дмитрий: витрину конкурентов разобрал полностью.",
            done, title="Вендоры Q3", uid=None)
    go = [m for m in asst.send_mail.out[before:] if m["subject"].startswith("Можно начинать")]
    assert [m["to"] for m in go] == ["p02@company.example"]
    assert "Переговоры с вендорами об акции" in go[0]["subject"]
    st = {t["n"]: t for t in protocol_tasks(asst.j) if t["protocol_id"] == pid}
    assert st[3]["status"] == "новая" and st[4]["status"] == "ждёт задачи 3"
