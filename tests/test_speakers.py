"""TASK_12: кто говорит — по флагу vad из Телемоста; связки «подпись в Телемосте → человек» запоминаются."""
from pathlib import Path

import pytest

from pomoshnik.avatar import transcript_to_text
from pomoshnik.org import Org
from pomoshnik.robot.speakers import (aliases_from_journal, intervals, label_entries, learn_aliases,
                                      parse_signal, resolve_name)
from yadro.clock import FakeClock, TZ_EKB
from yadro.journal import Journal
from datetime import datetime

ORG = Org.load(Path(__file__).resolve().parent.parent / "data" / "task" / "org_structure.csv")
T0 = 1_000_000.0          # мс: начало сессии аватара


def test_parse_signal_descriptions_and_vad():
    state = {"people": {}, "vad": {}}
    ev = parse_signal('{"uid":"1","upsertDescription":{"description":[{"id":"p-olga","meta":{"name":"Оля С."}},'
                      '{"id":"p-bot","meta":{"name":"ИИ-помощник Ивана Петрова"}}]}}', state, 5.0)
    assert state["people"] == {"p-olga": "Оля С.", "p-bot": "ИИ-помощник Ивана Петрова"} and ev == []
    ev = parse_signal('{"slotsConfig":{"slots":[{"selfView":{},"vad":false},'
                      '{"participantVideoByMid":{"participantId":"p-olga","mid":"video_AA"},"vad":true}]}}', state, 6.0)
    assert ev == [(6.0, "p-olga", True)]
    assert parse_signal('{"slotsConfig":{"slots":[{"participantVideoByMid":{"participantId":"p-olga"},"vad":true}]}}',
                        state, 7.0) == []                     # не изменилось — события нет
    assert parse_signal("не json", state, 8.0) == [] and parse_signal('{"ack":{}}', state, 8.0) == []


def test_intervals_close_open_speech_at_end():
    ev = [(100.0, "a", True), (102.0, "a", False), (103.0, "b", True)]
    assert intervals(ev, end=110.0) == {"a": [(100.0, 102.0)], "b": [(103.0, 110.0)]}


def test_label_entries_by_overlap_and_skip_robot():
    entries = [{"role": "avatar", "transcript": "Ольга, что по прогнозу?", "relative_timestamp": 2},
               {"role": "user", "transcript": "Это Пит. Всё по плану.", "relative_timestamp": 6},
               {"role": "user", "transcript": "А у меня вопрос", "relative_timestamp": 15},
               {"role": "user", "transcript": "непонятно кто", "relative_timestamp": 40}]
    ev = [(T0 + 5500, "p-pit", True), (T0 + 9000, "p-pit", False),
          (T0 + 14800, "p-olga", True), (T0 + 18000, "p-olga", False),
          (T0 + 1000, "p-bot", True), (T0 + 4000, "p-bot", False)]
    people = {"p-pit": "Пит", "p-olga": "Оля С.", "p-bot": "ИИ-помощник Ивана Петрова"}
    out = label_entries(entries, ev, T0, people, robot_name="ИИ-помощник Ивана Петрова",
                        aliases={"Оля С.": "Ольга Смирнова"})
    assert [e.get("speaker") for e in out] == [None, "Пит", "Ольга Смирнова", None]
    text = transcript_to_text(out, merge=True)
    assert "Пит: Это Пит. Всё по плану." in text and "Ольга Смирнова: А у меня вопрос" in text and "Участник: непонятно кто" in text



def test_one_person_in_meeting_owns_every_line_even_without_name_in_signals():
    """Живая проверка 27.11: имя Пита пришло только со страницы, в сигналах Телемоста id без имени → «Участник»."""
    entries = [{"role": "avatar", "transcript": "Начнём?", "relative_timestamp": 2},
               {"role": "user", "transcript": "Петр Кучерин говорит.", "relative_timestamp": 6},
               {"role": "user", "transcript": "Решений нет.", "relative_timestamp": 40}]
    ev = [(T0 + 5500, "p-x", True), (T0 + 9000, "p-x", False)]
    out = label_entries(entries, ev, T0, {}, robot_name="ИИ-помощник Ивана Петрова", solo="Петр Кучерин")
    assert [e.get("speaker") for e in out] == [None, "Петр Кучерин", "Петр Кучерин"]
    two = label_entries(entries, ev, T0, {}, robot_name="ИИ-помощник Ивана Петрова")
    assert [e.get("speaker") for e in two] == [None, "Участник", None]

def test_resolve_and_learn_aliases(tmp_path):
    assert resolve_name("Ольга Смирнова", ORG) == "Ольга Смирнова"
    assert resolve_name("Пит", ORG) is None
    j = Journal(tmp_path / "j.sqlite", FakeClock(datetime(2026, 11, 27, 9, 0, tzinfo=TZ_EKB)))
    labeled = [{"role": "user", "speaker_raw": "Пит", "transcript": "Это Иван Петров, всё по плану."},
               {"role": "user", "speaker_raw": "Оля С.", "transcript": "Меня зовут Ольга."},
               {"role": "user", "speaker_raw": "Гость", "transcript": "Я Марсианин Марсов."}]
    learned = learn_aliases(j, labeled, ORG)
    assert learned == {"Пит": "Иван Петров", "Оля С.": "Ольга Смирнова"}
    assert aliases_from_journal(j) == learned
    assert learn_aliases(j, labeled, ORG) == {}                    # уже знаем — повторно не пишем
    j.close()
