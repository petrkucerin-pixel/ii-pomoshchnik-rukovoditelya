"""TASK_17: свежая память. Проверка Пита 27.11: решения руководителя не попадали в память, одна тема жила
в нескольких, факты со встреч датировались другим календарём, чем письма."""
from datetime import datetime, timedelta

from test_assistant import am, a  # noqa: F401 — фикстуры помощника (am — с памятью)

from pomoshnik.memory import Fact, Memory, topics
from yadro.clock import TZ_EKB, FakeClock


def fact(topic, what, when, source="m1", who="Мария Лебедева", quote=None, kind="задача"):
    return Fact(topic=topic, kind=kind, what=what, who=who, when=when, source=source, quote=quote or what)


# ---------------- решения руководителя → память ----------------
def test_leader_decision_closes_the_topic_in_memory(am):
    asst, clock, _ = am
    asst.process_inbox()
    g = next(x for x in asst.active_groups() if "Отпуск" in x["subject"])
    letter_ids = [asst.j.event(e)["external_id"] for e in g["event_ids"]]
    asst.memory.add([fact("Отпуск Ирины Т.", "Согласовать отпуск 20–30 декабря", clock.now(), source=letter_ids[0])])
    asst.decide_letter(g["event_id"], "no", text="Мария, отпуск в эти даты согласовать не можем — конец года.")
    assert asst.sync_memory() == 1
    hist = asst.memory.history("Отпуск Ирины Т.")
    last = hist[-1]
    assert last.kind == "решение" and "отказ" in last.what and last.who == asst.p.name
    assert "конец года" in last.quote
    assert asst.sync_memory() == 0 and len(asst.memory.history("Отпуск Ирины Т.")) == len(hist)   # один раз
    reply = asst.recall("отпуск Ирины")
    assert reply.rstrip().splitlines()[-2].startswith("•") and "отказ" in reply.splitlines()[-2]


def test_decision_without_known_topic_uses_letter_subject_and_later_is_not_a_decision(am):
    asst, clock, _ = am
    asst.process_inbox()
    gs = asst.active_groups()
    asst.decide_letter(gs[0]["event_id"], "later")
    assert asst.sync_memory() == 0
    asst.decide_letter(gs[1]["event_id"], "yes")
    assert asst.sync_memory() == 1
    assert any(f.kind == "решение" and "согласовано" in f.what and gs[1]["subject"] in f.what
               for f in asst.memory.all())


def test_recall_brings_decisions_made_a_moment_ago(am):
    asst, clock, _ = am
    asst.process_inbox()
    g = asst.active_groups()[0]
    asst.decide_letter(g["event_id"], "yes")
    assert "согласовано" in asst.recall(g["subject"])                  # «что по …» сразу после решения


# ---------------- одна тема — одно место ----------------
def test_topics_with_the_same_core_are_one(tmp_path):
    clock = FakeClock(datetime(2026, 12, 11, 10, tzinfo=TZ_EKB))
    mem = Memory(tmp_path / "m.sqlite", clock)
    t = clock.now()
    mem.add([fact("Облако Плюс", "Ждём до 12.12", t, "m1", "Сергей Морозов"),
             fact("Договор с Облако Плюс", "Срок в задаче 01.12, в отчёте 12.12", t + timedelta(hours=1), "p3", "ИИ"),
             fact("Ромашка-Ритейл", "Просит скидку 12%", t, "m2", "Ольга Смирнова"),
             fact("Маржа по контракту Ромашка-Ритейл", "Маржа 18%", t, "m3", "Ольга Смирнова"),
             fact("План", "что-то общее", t, "m4"),
             fact("План отпусков", "график к 20.12", t, "m5")])
    groups = {frozenset(f.topic for f in g) for g in topics(mem)}
    assert frozenset({"Облако Плюс", "Договор с Облако Плюс"}) in groups
    assert frozenset({"Ромашка-Ритейл", "Маржа по контракту Ромашка-Ритейл"}) in groups
    assert frozenset({"План"}) in groups and frozenset({"План отпусков"}) in groups      # общее слово не склеивает
    related = mem.related("Облако Плюс")
    assert {f.what for f in related} == {"Ждём до 12.12", "Срок в задаче 01.12, в отчёте 12.12"}


def test_recall_answers_by_the_whole_topic(am):
    asst, clock, _ = am
    t = clock.now()
    asst.memory.add([fact("Облако Плюс", "Ждём до 12.12", t, "m1", "Сергей Морозов"),
                     fact("Договор с Облако Плюс", "Срок в задаче 01.12, в отчёте 12.12", t, "p3", "Иван Петров")])
    reply = asst.recall("Облако Плюс")
    assert "Ждём до 12.12" in reply and "01.12, в отчёте 12.12" in reply


# ---------------- одни часы: учебный день помощника ----------------
def test_now_is_on_the_work_day(a, monkeypatch):
    asst, clock, _ = a
    monkeypatch.setenv("DEMO_DATE", "2026-12-11")
    n = asst.now()
    assert n.date().isoformat() == "2026-12-11" and (n.hour, n.minute) == (clock.now().hour, clock.now().minute)
    monkeypatch.delenv("DEMO_DATE")
    assert asst.now() == clock.now()


# ---------------- сводка за сегодня ----------------
def test_digest_since_keeps_only_today(a):
    from pomoshnik.assistant import digest_from_journal
    asst, clock, _ = a
    asst.process_inbox()
    all_lines = digest_from_journal(asst.j, asst.p.org)
    clock.advance(days=1)
    start = clock.now().replace(hour=0, minute=0)
    assert all_lines and digest_from_journal(asst.j, asst.p.org, since=start) == []


def test_decision_is_written_once_per_glued_topic(am):
    asst, clock, _ = am
    asst.process_inbox()
    g = next(x for x in asst.active_groups() if "Отпуск" in x["subject"])
    src = asst.j.event(g["event_ids"][0])["external_id"]
    asst.memory.add([fact("Отпуск Ирины Т.", "Согласовать отпуск", clock.now(), source=src),
                     fact("Отпуск Ирины Т. 20–30 декабря", "Замена найдена", clock.now(), source=src, quote="замену нашли")])
    asst.decide_letter(g["event_id"], "no")
    asst.sync_memory()
    decided = [f for f in asst.memory.all() if f.kind == "решение"]
    assert len(decided) == 1 and decided[0].what.startswith("отказ — ")          # без «Иван Петров: Иван Петров»
