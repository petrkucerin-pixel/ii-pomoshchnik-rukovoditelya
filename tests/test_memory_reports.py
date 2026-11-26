import csv
from datetime import date, datetime
from pathlib import Path

from pomoshnik.org import Org
from pomoshnik.plan import load_tasks
from pomoshnik.reports import Report, plan_percent, review
from yadro.clock import TZ_EKB

TASK = Path(__file__).resolve().parent.parent / "data" / "task"
ORG = Org.load(TASK / "org_structure.csv")
TASKS = load_tasks(TASK / "tasks.csv")
GOALS = ["Выполнить план продаж SMB."]
TODAY = date(2026, 12, 11)


def reports(rows=None):
    rows = rows or [r for r in csv.DictReader(open(TASK / "inbox.csv", encoding="utf-8"))
                    if r["category"] in ("отчёт", "статус")]
    return [Report(r["id"], r["from_name"], r["from_email"],
                   datetime.fromisoformat(r["received"]).replace(tzinfo=TZ_EKB), r["subject"], r["body"]) for r in rows]


def kinds(fs, kind):
    return [f for f in fs if f.kind == kind]


def test_plan_percent():
    assert plan_percent("Прогноз 94% от плана, риск по двум сделкам.") == 94
    assert plan_percent("Обновил дашборд за неделю, отклонение от плана −6%.") == 94
    assert plan_percent("Всё хорошо") is None


def test_case1_one_question_per_stale_status_not_seven():
    fs = review(reports(), TASKS, ORG, GOALS, TODAY)
    stale = kinds(fs, "нет_движения")
    oblako = [f for f in stale if "Облако Плюс" in f.topic]
    assert len(oblako) == 1 and oblako[0].to_email == "p05@company.example"
    assert len(oblako[0].sources) == 7
    assert "не меняется" in oblako[0].question


def test_case2_task_deadline_mismatch():
    fs = kinds(review(reports(), TASKS, ORG, GOALS, TODAY), "срок_задачи")
    f = next(f for f in fs if "Облако Плюс" in f.topic)
    assert "01.12" in f.question and "12.12" in f.question and "уже прошёл" in f.question
    assert f.to_email == "p05@company.example"


def test_case3_numbers_agree():
    fs = review(reports(), TASKS, ORG, GOALS, TODAY)
    assert kinds(fs, "сверено") and not kinds(fs, "цифры")


def test_case3_numbers_disagree():
    rows = [r for r in csv.DictReader(open(TASK / "inbox.csv", encoding="utf-8")) if r["category"] in ("отчёт", "статус")]
    for r in rows:
        r["body"] = r["body"].replace("−6%", "−9%")
    fs = kinds(review(reports(rows), TASKS, ORG, GOALS, TODAY), "цифры")
    assert {f.to_email for f in fs} == {"p02@company.example", "p07@company.example"}


def test_case4_vague_risk_asks_author():
    fs = kinds(review(reports(), TASKS, ORG, GOALS, TODAY), "нет_деталей")
    assert len(fs) == 1 and fs[0].to_email == "p02@company.example" and "Какие сделки" in fs[0].question


def test_case5_missing_report_reminder():
    fs = kinds(review(reports(), TASKS, ORG, GOALS, TODAY), "нет_отчёта")
    anna = [f for f in fs if f.to_name == "Анна Волкова"]
    assert anna and anna[0].to_email == "p04@company.example"


def test_case6_goal_risk_goes_to_leader_only_once():
    fs = kinds(review(reports(), TASKS, ORG, GOALS, TODAY), "риск_цели")
    assert len(fs) == 1 and "94" in fs[0].leader and fs[0].to_email is None


def test_no_goal_no_leader_line():
    assert not kinds(review(reports(), TASKS, ORG, ["Запустить партнёрский канал"], TODAY), "риск_цели")


def test_questions_go_to_people_not_leader():
    fs = review(reports(), TASKS, ORG, GOALS, TODAY)
    assert all(f.to_email != "p01@company.example" for f in fs)
    assert sum(1 for f in fs if f.leader) == 1
