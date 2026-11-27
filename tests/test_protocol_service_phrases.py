"""Служебные фразы аватара — не задачи. Живой прогон 27.11: из «протокол пришлю ему на утверждение»
модель сделала задачу «Утвердить протокол встречи» руководителю."""
from datetime import datetime
from pathlib import Path

from pomoshnik.avatar import opening_text
from pomoshnik.llm import StubLLM
from pomoshnik.org import Org
from pomoshnik.protocol import build_protocol
from yadro.clock import TZ_EKB

ORG = Org.load(Path(__file__).resolve().parent.parent / "data" / "task" / "org_structure.csv")
END = datetime(2026, 12, 11, 14, 20, tzinfo=TZ_EKB)


def test_avatar_service_phrases_do_not_become_tasks():
    transcript = ("00:00:22\nИИ-помощник: " + opening_text("Ивана Петрова") + "\n"
                  "00:00:40\nПетр Кучерин: Анна Волкова отправит отчёт по e-com до пятницы.\n"
                  "00:02:00\nИИ-помощник: Повторю договорённости. Протокол уйдёт на утверждение.")
    llm = StubLLM(default={"tasks": [
        {"owner": "Иван Петров", "what": "Утвердить протокол встречи", "due_text": "",
         "quote": "протокол пришлю ему на утверждение"},
        {"owner": "Руководитель", "what": "Утвердить протокол", "due_text": "", "quote": "Протокол уйдёт на утверждение"},
        {"owner": "Анна Волкова", "what": "Отправить отчёт по e-com", "due_text": "до пятницы",
         "quote": "Анна Волкова отправит отчёт по e-com до пятницы"}]})
    p = build_protocol(llm, transcript, title="Проверка", meeting_end=END, org=ORG)
    assert [t.what for t in p.tasks] == ["Отправить отчёт по e-com"]


def test_dependencies_renumbered_after_service_task_removed():
    llm = StubLLM(default={"tasks": [
        {"owner": "Иван Петров", "what": "Утвердить протокол", "due_text": "", "quote": "протокол пришлю ему на утверждение"},
        {"owner": "Анна Волкова", "what": "Собрать метрики", "due_text": "", "quote": "Анна соберёт метрики"},
        {"owner": "Сергей Морозов", "what": "Сделать витрину", "due_text": "", "quote": "после метрик Сергей сделает витрину",
         "after": [2]}]})
    transcript = ("ИИ-помощник: " + opening_text("Ивана Петрова") + "\nПит: Анна соберёт метрики, "
                  "после метрик Сергей сделает витрину.")
    p = build_protocol(llm, transcript, title="Проверка", meeting_end=END, org=ORG)
    assert [t.what for t in p.tasks] == ["Собрать метрики", "Сделать витрину"] and p.tasks[1].after == [1]
