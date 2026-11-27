import csv
import shutil
from pathlib import Path

import pytest

from pomoshnik.eval_triage import evaluate
from pomoshnik.llm import StubLLM, cost_usd, parse_json
from pomoshnik.profile import load_profile
from pomoshnik.triage import decide, triage

ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "data" / "task"
ORG = TASK / "org_structure.csv"
ROWS = list(csv.DictReader(open(TASK / "inbox.csv", encoding="utf-8")))


@pytest.fixture
def ivan(tmp_path):
    d = tmp_path / "ivan_petrov"
    d.mkdir()
    shutil.copy(TASK / "leader_profile.md", d / "profile.md")
    return load_profile(d, org_path=ORG)


def second_profile(tmp_path, edit):
    d = tmp_path / "vtoroy"
    d.mkdir()
    (d / "profile.md").write_text(edit((TASK / "leader_profile.md").read_text(encoding="utf-8")), encoding="utf-8")
    return load_profile(d, org_path=ORG)


# --- профиль ---------------------------------------------------------------

def test_profile_reads_identity_rhythm_and_confirm_list(ivan):
    assert ivan.name == "Иван Петров"
    assert ivan.plan_time == "08:30"
    assert ivan.confirm == {"деньги", "кадры", "обязательства_клиентам", "руководство"}
    assert len(ivan.goals) == 3


def test_profile_rules_from_text(ivan):
    assert ivan.rules["календарь"].action == "авто"
    assert ivan.rules["рассылка"].action == "игнорировать"
    assert ivan.rules["продажи"].action == "делегировать" and ivan.rules["продажи"].delegate_to == "p02"
    assert ivan.rules["клиенты"].delegate_to == "p06"
    assert ivan.rules["отчёт"].action == "в_сводку" and ivan.rules["статус"].action == "в_сводку"
    assert "приглашение" not in ivan.rules          # «неприоритетные» приглашения — решает руководитель
    assert (ivan.dir / "rules.json").exists()


# --- проверочные случаи TASK_04 --------------------------------------------

def test_case1_rules_accuracy_and_no_guarded_leaks(ivan):
    res = evaluate(ivan, ROWS)
    assert res["accuracy"] >= 0.95
    assert res["guarded_leaks"] == []


def test_case2_second_profile_changes_decisions_without_code(tmp_path):
    p2 = second_profile(tmp_path, lambda t: t.replace(
        "- отчёты и статусы — не отвечать, а включить в утреннюю или недельную сводку.\n", "")
        .replace("Иван Петров", "Анна Сидорова"))
    assert p2.name == "Анна Сидорова"
    assert decide(p2, "отчёт", "p07@company.example").action == "спросить_руководителя"
    assert decide(p2, "календарь", "p04@company.example").action == "авто"


def test_case2_profiles_keep_separate_rules_files(tmp_path, ivan):
    p2 = second_profile(tmp_path, lambda t: t)
    assert p2.dir != ivan.dir and (p2.dir / "rules.json").exists()


def test_case3_unknown_category_goes_to_leader(ivan):
    d = decide(ivan, "доступы", "p10@company.example")
    assert d.action == "спросить_руководителя" and "не задано" in d.reason


def test_case4_boss_letter_always_to_leader(ivan):
    d = decide(ivan, "статус", "p09@company.example")
    assert d.action == "спросить_руководителя" and "руководителя руководителя" in d.reason


@pytest.mark.parametrize("category", ["деньги", "рассылка", "продажи"])
def test_case5_low_confidence_to_leader(ivan, category):
    llm = StubLLM(default={"category": category, "summary": "?", "confidence": 0.3})
    d = triage(ivan, llm, subject="?", body="?", from_name="X", from_email="p07@company.example")
    assert d.action == "спросить_руководителя" and "уверенность" in d.reason


def test_case5_low_confidence_summary_rule_still_goes_to_summary(ivan):
    """«В сводку» ничего не отправляет наружу, а руководитель письмо всё равно увидит — сомнение не повод его звать."""
    llm = StubLLM(default={"category": "отчёт", "summary": "дашборд", "confidence": 0.55})
    d = triage(ivan, llm, subject="Дашборд", body="?", from_name="Алексей", from_email="p07@company.example")
    assert d.action == "в_сводку" and "0.55" in d.reason


def test_case6_unknown_category_from_model_rejected(ivan):
    llm = StubLLM(default={"category": "срочное_всё", "summary": "?", "confidence": 0.99})
    d = triage(ivan, llm, subject="x", body="y", from_name="X", from_email="p07@company.example")
    assert d.action == "спросить_руководителя" and d.category == ""


def test_case6_llm_rules_cannot_override_confirm_or_invent_people(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    shutil.copy(TASK / "leader_profile.md", d / "profile.md")
    llm = StubLLM(default={"rules": {
        "деньги": {"action": "авто"},                                    # запрет профиля важнее
        "доступы": {"action": "делегировать", "delegate_to": "p08"},      # правдоподобно — принимаем
        "приглашение": {"action": "делегировать", "delegate_to": "p99"},  # нет такого человека
        "погода": {"action": "игнорировать"},                             # нет такой категории
    }})
    p = load_profile(d, org_path=ORG, llm=llm)
    assert decide(p, "деньги", "p10@company.example").action == "спросить_руководителя"
    assert p.rules["доступы"].delegate_to == "p08"
    assert "приглашение" not in p.rules and "погода" not in p.rules


def test_delegation_target_and_external_sender(ivan):
    d = decide(ivan, "продажи", "client1@romashka.example")
    assert d.action == "делегировать" and d.delegate_to == "p02" and d.sender_role == "внешний"


# --- слой модели -------------------------------------------------------------

def test_parse_json_with_fences_and_text():
    assert parse_json('Ответ:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('вот {"b": [1, 2]} конец') == {"b": [1, 2]}


def test_cost_known_and_unknown_model():
    assert cost_usd("deepseek-flash", 1_000_000, 1_000_000) == pytest.approx(1.5)
    assert cost_usd("local-model", 1000, 1000) == 0.0
