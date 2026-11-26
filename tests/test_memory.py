"""TASK_06, память помощника: факты с подтверждённой цитатой, поиск по теме и словам, своя база у профиля."""
from datetime import datetime

import pytest

from pomoshnik.llm import StubLLM
from pomoshnik.memory import Memory, extract_facts
from yadro.clock import TZ_EKB, FakeClock

T1 = datetime(2026, 12, 8, 14, 10, tzinfo=TZ_EKB)
T2 = datetime(2026, 12, 10, 9, 0, tzinfo=TZ_EKB)

LETTER = "Договор на подписи у юристов, ждём до 12.12."
LETTER2 = "Юристы вернули договор с правками, подписание сдвигается на 15.12."


def facts_answer(*facts):
    return StubLLM(default={"facts": list(facts)})


GOOD = {"topic": "«Облако Плюс»", "kind": "партнёр", "what": "договор у юристов, ждём до 12.12",
        "who": "Сергей Морозов", "quote": "Договор на подписи у юристов"}
FAKE = {"topic": "Облако Плюс", "kind": "сделка", "what": "договор подписан",
        "who": "Сергей Морозов", "quote": "договор уже подписан обеими сторонами"}


@pytest.fixture
def mem(tmp_path):
    m = Memory(tmp_path / "ivan_memory.sqlite", FakeClock(T2))
    yield m
    m.close()


def test_fact_without_confirmed_quote_is_not_saved(mem):
    facts = extract_facts(facts_answer(GOOD, FAKE), LETTER, source="m022", who="Сергей Морозов", when=T1)
    assert [f.quote for f in facts] == ["Договор на подписи у юристов"]
    assert mem.add(facts) == 1
    assert [f.what for f in mem.search("договор подписан")] == ["договор у юристов, ждём до 12.12"]


def test_fact_keeps_source_time_author_and_quote(mem):
    mem.add(extract_facts(facts_answer(GOOD), LETTER, source="m022", who="Сергей Морозов", when=T1))
    f = mem.search("Облако Плюс")[0]
    assert (f.topic, f.kind, f.who, f.source, f.when) == ("Облако Плюс", "партнёр", "Сергей Морозов", "m022", T1)
    assert f.quote in LETTER


def test_who_defaults_to_author_and_unknown_kind_is_other(mem):
    raw = dict(GOOD, who="", kind="что-то своё")
    f = extract_facts(facts_answer(raw), LETTER, source="m022", who="Сергей Морозов", when=T1)[0]
    assert f.who == "Сергей Морозов" and f.kind == "прочее"


def test_broken_model_answer_gives_no_facts(mem):
    assert extract_facts(StubLLM(default={"facts": "не список"}), LETTER, source="x", who="", when=T1) == []
    assert extract_facts(StubLLM(default={"facts": [None, 5, {"quote": ""}]}), LETTER, source="x", who="", when=T1) == []


def test_search_ignores_word_forms_case_and_yo(mem):
    mem.add(extract_facts(facts_answer(GOOD), LETTER, source="m022", who="Сергей Морозов", when=T1))
    assert mem.search("облака плюс")
    assert mem.search("ЮРИСТАМ")
    assert not mem.search("ТехноСтрой")
    assert mem.search("") == [] and mem.search("?!") == []


def test_history_of_topic_in_time_order(mem):
    later = dict(GOOD, what="договор: подписание сдвигается на 15.12", quote="подписание сдвигается на 15.12")
    mem.add(extract_facts(facts_answer(later), LETTER2, source="m030", who="Сергей Морозов", when=T2))
    mem.add(extract_facts(facts_answer(GOOD), LETTER, source="m022", who="Сергей Морозов", when=T1))
    h = mem.history("облако плюс")
    assert [f.source for f in h] == ["m022", "m030"]
    assert [f.source for f in mem.search("договор")] == ["m022", "m030"]


def test_same_letter_twice_is_not_duplicated(mem):
    for _ in range(2):
        mem.add(extract_facts(facts_answer(GOOD), LETTER, source="m022", who="Сергей Морозов", when=T1))
    assert len(mem.history("Облако Плюс")) == 1


def test_each_profile_has_own_base(tmp_path):
    ivan = Memory(tmp_path / "ivan_memory.sqlite", FakeClock(T2))
    olga = Memory(tmp_path / "olga_memory.sqlite", FakeClock(T2))
    ivan.add(extract_facts(facts_answer(GOOD), LETTER, source="m022", who="Сергей Морозов", when=T1))
    assert ivan.search("Облако") and not olga.search("Облако")
    ivan.close()
    olga.close()


def test_memory_survives_restart(tmp_path):
    m = Memory(tmp_path / "m.sqlite", FakeClock(T2))
    m.add(extract_facts(facts_answer(GOOD), LETTER, source="m022", who="Сергей Морозов", when=T1))
    m.close()
    m = Memory(tmp_path / "m.sqlite", FakeClock(T2))
    assert m.history("Облако Плюс")[0].when == T1
    m.close()
