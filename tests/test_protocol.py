from datetime import datetime
from pathlib import Path

import pytest

from pomoshnik.llm import StubLLM
from pomoshnik.org import Org
from pomoshnik.protocol import ProtocolStore, build_protocol, questions_from_previous, render_markdown
from yadro.clock import TZ_EKB, FakeClock

ORG = Org.load(Path(__file__).resolve().parent.parent / "data" / "task" / "org_structure.csv")
END = datetime(2026, 12, 8, 11, 0, tzinfo=TZ_EKB)   # вторник

TRANSCRIPT = """00:00:01
Коллеги, по партнёрскому каналу: Сергей, договор с «Облако Плюс» нужно подписать к пятнице.
00:00:20
Ольга подготовит коммерческое предложение для «ТехноСтрой» до конца месяца.
00:00:45
По бюджету рекламы пока не решили, вернёмся позже.
00:01:10
Маша из подрядчика пришлёт смету."""

GOOD = {
    "topics": [{"title": "Партнёрский канал", "points": ["Договор с «Облако Плюс» — к пятнице"]}],
    "decisions": [{"text": "Подписать договор с «Облако Плюс»", "quote": "договор с «Облако Плюс» нужно подписать к пятнице"}],
    "tasks": [
        {"owner": "Сергей Морозов", "what": "Подписать договор с «Облако Плюс»", "due_text": "к пятнице",
         "quote": "договор с «Облако Плюс» нужно подписать к пятнице"},
        {"owner": "Ольга", "what": "КП для «ТехноСтрой»", "due_text": "до конца месяца",
         "quote": "Ольга подготовит коммерческое предложение для «ТехноСтрой»"},
        {"owner": "Маша", "what": "Прислать смету", "due_text": "", "quote": "Маша из подрядчика пришлёт смету"},
        {"owner": "Дмитрий Козлов", "what": "Запустить рекламу", "due_text": "завтра",
         "quote": "Дмитрий запустит рекламу завтра"},
    ],
    "open_questions": ["Бюджет рекламы не решён"],
}


def make(llm_answer=GOOD, rules=()):
    llm = StubLLM(default=llm_answer)
    p = build_protocol(llm, TRANSCRIPT, title="Еженедельная встреча", meeting_end=END, org=ORG, rules=list(rules))
    return p, llm


def test_case1_tasks_resolved_with_dates():
    p, _ = make()
    t = p.tasks[0]
    assert t.owner_id == "p05" and t.quote_ok and t.due.date().isoformat() == "2026-12-11" and not t.default_due
    assert p.tasks[1].owner_id == "p02"                          # «Ольга» → Ольга Смирнова
    assert p.tasks[1].due.date().isoformat() == "2026-12-31"
    assert p.decisions[0].quote_ok


def test_case2_invented_quote_flagged_not_dropped():
    p, _ = make()
    t = p.tasks[3]
    assert not t.quote_ok and "не подтверждена" in " ".join(t.flags)
    assert len(p.tasks) == 4


def test_case3_unknown_owner_flagged():
    p, _ = make()
    t = p.tasks[2]
    assert t.owner_id is None and t.owner == "Маша" and "уточнить исполнителя" in t.flags


def test_case4_default_deadline():
    p, _ = make()
    t = p.tasks[2]
    assert t.default_due and t.due.date().isoformat() == "2026-12-11" and "срок по умолчанию" in t.flags


def test_render_has_customer_sections():
    p, _ = make()
    md = render_markdown(p)
    for part in ("Еженедельная встреча", "Заметки о встрече", "Дальнейшие шаги", "Предложения ИИ",
                 "Сергей Морозов", "11.12.2026", "уточнить исполнителя"):
        assert part in md


def test_case7_questions_from_previous():
    p, _ = make()
    q = questions_from_previous(p)
    assert len(q) == len(p.tasks) + len(p.open_questions)
    assert any("Облако Плюс" in x and "11.12" in x for x in q)
    assert any("Бюджет рекламы" in x for x in q)


@pytest.fixture
def store(tmp_path):
    clock = FakeClock(END)
    s = ProtocolStore(tmp_path / "p.sqlite", clock)
    yield s, clock
    s.close()


def test_store_timing_and_idempotent_approve(store):
    s, clock = store
    p, _ = make()
    clock.advance(minutes=3)
    pid = s.save_draft(p)
    rec = s.get(pid)
    assert rec["status"] == "черновик" and rec["minutes_to_draft"] == pytest.approx(3)
    clock.advance(minutes=2)
    assert s.approve(pid)
    assert s.get(pid)["minutes_to_approved"] == pytest.approx(5)
    assert not s.approve(pid)                                     # случай 6


def test_case5_edit_applied_and_reused_as_rule(store):
    s, clock = store
    p, _ = make()
    pid = s.save_draft(p)
    edited = dict(GOOD, tasks=[dict(GOOD["tasks"][2], owner="Ольга Смирнова", due_text="к пятнице")] + GOOD["tasks"][:2])
    llm = StubLLM(default=edited)
    new = s.apply_edit(pid, "Смету пришлёт Ольга, срок — пятница", llm, ORG)
    assert new.tasks[0].owner_id == "p02" and new.tasks[0].due.date().isoformat() == "2026-12-11"
    assert s.get(pid)["status"] == "черновик" and s.get(pid)["edits"] == 1
    assert s.leader_rules() == ["Смету пришлёт Ольга, срок — пятница"]
    p2, llm2 = make(rules=s.leader_rules())
    assert "Смету пришлёт Ольга" in llm2.calls[0][0] + llm2.calls[0][1]


def test_reject(store):
    s, _ = store
    pid = s.save_draft(make()[0])
    assert s.reject(pid)
    assert s.get(pid)["status"] == "отклонён"
    assert not s.approve(pid)


def test_leader_alias_and_spoken_date_in_tasks():
    from datetime import datetime
    from pomoshnik.org import Org
    from pomoshnik.protocol import _from_json
    from yadro.clock import TZ_EKB
    org = Org.load(Path(__file__).resolve().parent.parent / "data" / "task" / "org_structure.csv")
    tr = "Участник: Анна отправит отчёт 12-го декабря. ИИ-помощник: вынесу вопрос по договору руководителю."
    p = _from_json({"tasks": [
        {"owner": "Анна", "what": "Отчёт по e-com", "due_text": "12-го декабря", "quote": "Анна отправит отчёт 12-го декабря"},
        {"owner": "Руководитель", "what": "Решить срок договора", "due_text": "", "quote": "вынесу вопрос по договору руководителю"}]},
        tr, title="Планёрка", meeting_end=datetime(2026, 11, 27, 9, 0, tzinfo=TZ_EKB), org=org)
    assert p.tasks[0].due.date().isoformat() == "2026-12-12" and not p.tasks[0].default_due
    assert p.tasks[1].owner == "Иван Петров" and "уточнить исполнителя" not in p.tasks[1].flags
