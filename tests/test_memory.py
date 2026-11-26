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


# ---------------- вся история для аватара ----------------
def _fact(topic, what, quote, when, source, who="Сергей Морозов"):
    from pomoshnik.memory import Fact
    return Fact(topic=topic, kind="партнёр", what=what, who=who, when=when, source=source, quote=quote)


def test_brief_has_every_topic_newest_first_and_collapses_repeats(mem):
    from datetime import timedelta
    from pomoshnik.memory import brief
    for i in range(7):
        mem.add([_fact("Облако Плюс", "договор у юристов, ждём до 12.12", "Договор на подписи у юристов",
                       T1 + timedelta(hours=i * 10), f"m{i}")])
    mem.add([_fact("ТехноСтрой", "КП до конца месяца", "Ольга подготовит КП", T1 - timedelta(days=3), "протокол 1", "Ольга")])
    text = brief(mem)
    assert text.index("ТехноСтрой") > text.index("Облако Плюс")            # свежая тема выше
    assert text.count("Договор на подписи у юристов") == 1 and "7 раз без изменений" in text
    assert "протокол 1" in text and "Ольга" in text and "05.12" in text


def test_brief_cuts_old_topics_and_says_so(mem):
    from datetime import timedelta
    from pomoshnik.memory import brief
    for i in range(40):
        mem.add([_fact(f"Сделка {i}", f"статус {i}", f"статус сделки номер {i}", T1 + timedelta(hours=i), f"m{i}")])
    text = brief(mem, max_chars=1500)
    assert len(text) <= 1500 and "Сделка 39" in text and "Сделка 0 " not in text and "ещё" in text


def test_brief_empty_memory(mem):
    from pomoshnik.memory import brief
    assert brief(mem) == ""


def test_repeat_with_other_punctuation_is_still_one_line(mem):
    from datetime import timedelta
    from pomoshnik.memory import brief
    mem.add([_fact("Облако Плюс", "ждём до 12.12", "Договор на подписи у юристов, ждём до 12.12.", T1, "m1")])
    mem.add([_fact("Облако Плюс", "ждём до 12.12", "Договор на подписи у юристов, ждём до 12.12", T1 + timedelta(hours=5), "m2")])
    assert "2 раза без изменений" in brief(mem)


def test_topic_quotes_removed_whole_not_half():
    raw = dict(GOOD, topic="Маржа по «Ромашка-Ритейл»", quote="Договор на подписи у юристов")
    f = extract_facts(facts_answer(raw), LETTER, source="m022", who="Сергей Морозов", when=T1)[0]
    assert f.topic == "Маржа по Ромашка-Ритейл"
