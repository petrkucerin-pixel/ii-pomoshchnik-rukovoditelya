"""TASK_14: помощник сам пишет письма в стиле руководителя и учится на правках. Факты подставляет код."""
from datetime import datetime

import pytest

from pathlib import Path

from pomoshnik.llm import StubLLM
from pomoshnik.org import Org
from pomoshnik.templates import Templates
from pomoshnik.writer import StyleBook, audience_of
from yadro.clock import TZ_EKB, FakeClock
from yadro.journal import Journal

STYLE = "Коротко, по делу, на «вы», без восклицаний."
SHORT = {"auto_reply": "Спасибо, получил.\n\n{подпись}",
         "invite": "Коллеги, встреча «{встреча}» {когда}, {длительность} мин. Ссылка: {ссылка}\n\n{подпись}",
         "task_go": "{имя}, {после} готово — начинайте «{задача}», срок {срок}.\n\n{подпись}"}


def gen(letters, rules=("Пиши коротко, без «подтверждаю».",), reply="Запомнил."):
    """Модель: на просьбу научить — правила, на составление писем — тексты."""
    def answer(system, user):
        if "составь тексты" in system.lower():
            return {"letters": letters}
        if "руководитель поправил" in system.lower():
            return {"rule": "В подтверждениях не пиши «подтверждаю», пиши «спасибо, получил»."}
        return {"rules": list(rules), "reply": reply}
    return StubLLM(answer)


@pytest.fixture
def env(tmp_path):
    j = Journal(tmp_path / "j.sqlite", FakeClock(datetime(2026, 12, 11, 10, tzinfo=TZ_EKB)))
    return j, Templates(tmp_path)


def book(env, llm):
    j, t = env
    return StyleBook(llm, j, t, style=STYLE)


def test_without_rules_letters_are_the_standard_ones(env):
    b = book(env, StubLLM())
    subject, body = b.render("auto_reply", тема="Отчёт", подпись="— ИИ-помощник Ивана Петрова")
    assert subject == "Re: Отчёт" and body.startswith("Принято, подтверждаю.")
    assert b.rules() == []


def test_teach_makes_a_rule_and_the_next_letter_follows_it(env):
    llm = gen(SHORT)
    b = book(env, llm)
    r = b.teach("Не пиши «подтверждаю», это сухо. Пиши короче.")
    assert r["reply"] == "Запомнил." and [x["text"] for x in b.rules()] == ["Пиши коротко, без «подтверждаю»."]
    assert b.rules()[0]["source"] == "вы сказали"
    subject, body = b.render("auto_reply", тема="Отчёт", подпись="— ИИ-помощник Ивана Петрова")
    assert subject == "Re: Отчёт" and body == "Спасибо, получил.\n\n— ИИ-помощник Ивана Петрова"
    gen_prompt = next(u for s, u in llm.calls if "составь тексты" in s.lower())
    assert STYLE in gen_prompt and "без «подтверждаю»" in gen_prompt     # стиль и правила — в задании модели


def test_facts_come_from_code_exactly(env):
    b = book(env, gen(SHORT))
    b.teach("короче")
    _, body = b.render("invite", встреча="Планёрка", когда="12.12 10:00", длительность=30,
                       ссылка="https://telemost.yandex.ru/j/555", подпись="— ИИ-помощник")
    assert "https://telemost.yandex.ru/j/555" in body and "12.12 10:00" in body and "30 мин" in body


@pytest.mark.parametrize("bad,why", [
    ("Встреча «{встреча}» завтра в 10:00. {ссылка} {когда}\n\n{подпись}", "цифр"),       # своя цифра
    ("Встреча «{встреча}» {когда}.\n\n{подпись}", "ссылка"),                           # нет обязательной ссылки
    ("Встреча «{встреча}» {когда} {ссылка} {бюджет}\n\n{подпись}", "бюджет"),            # выдуманная переменная
])
def test_bad_generated_letter_keeps_previous_text_and_says_why(env, bad, why):
    b = book(env, gen({"invite": bad, "auto_reply": SHORT["auto_reply"]}))
    b.teach("короче")
    _, body = b.render("invite", встреча="Планёрка", когда="12.12 10:00", длительность=30, ссылка="L", подпись="П")
    assert body.startswith("Здравствуйте!\n\nПриглашаю")                # прежний текст
    assert b.render("auto_reply", тема="x", подпись="П")[1].startswith("Спасибо, получил.")   # остальные — новые
    inv = next(x for k in b.overview()["kinds"] for x in k["letters"] if x["key"] == "invite")
    assert inv["source"] == "стандартный" and why in inv["problem"]


def test_learns_from_leader_edit_once(env):
    j, _ = env
    llm = gen(SHORT)
    b = book(env, llm)
    j.log_action("decision_draft_edit", actor="human", path="decision",
                 payload={"key": "q1", "draft": "Подтверждаю получение.", "text": "Спасибо, получил."})
    assert b.learn_pending() == 1
    rule = b.rules()[0]
    assert "спасибо, получил" in rule["text"] and rule["source"] == "ваша правка"
    assert rule["example"] == {"draft": "Подтверждаю получение.", "text": "Спасибо, получил."}
    n = len(llm.calls)
    assert b.learn_pending() == 0 and len(llm.calls) == n                 # уже выучено — модель не зовём
    assert rule["kind"] == "decision"                                      # правка в карточке — про ответы по решению
    assert b.render("auto_reply", тема="x", подпись="П")[1].startswith("Принято, подтверждаю.")   # автоответ не тронут


def test_edit_about_facts_only_is_not_a_rule(env):
    j, _ = env
    b = book(env, StubLLM(lambda s, u: {"rule": None}))
    j.log_action("decision_draft_edit", actor="human", path="decision",
                 payload={"key": "q1", "draft": "Срок — пятница.", "text": "Срок — понедельник."})
    assert b.learn_pending() == 1 and b.rules() == []


def test_forget_rule_returns_standard_letters(env):
    b = book(env, gen(SHORT))
    b.teach("короче")
    b.forget(b.rules()[0]["id"])
    assert b.rules() == []
    assert b.render("auto_reply", тема="x", подпись="П")[1].startswith("Принято, подтверждаю.")


def test_everything_survives_restart(env):
    j, t = env
    book(env, gen(SHORT)).teach("короче")
    again = StyleBook(StubLLM(), j, t, style=STYLE)                       # модель после перезапуска не нужна
    assert len(again.rules()) == 1 and again.render("auto_reply", тема="x", подпись="П")[1].startswith("Спасибо")
    assert "без «подтверждаю»" in again.rules_text("auto_reply")


def test_model_failure_keeps_letters_working(env):
    def boom(system, user):
        if "составь тексты" in system.lower():
            raise RuntimeError("модель недоступна")
        return {"rules": ["Короче."], "reply": "Запомнил."}
    b = book(env, StubLLM(boom))
    b.teach("короче")
    assert len(b.rules()) == 1
    assert b.render("auto_reply", тема="x", подпись="П")[1].startswith("Принято, подтверждаю.")


# ---------------- виды писем и адресаты (решение Пита 27.11: «на все письма одного шаблона быть не может») ----------------
ORG = Org.load(Path(__file__).resolve().parent.parent / "data" / "task" / "org_structure.csv")


def test_audience_by_org_structure():
    assert audience_of(ORG, "p09@company.example") == "руководство"          # руководитель руководителя
    assert audience_of(ORG, "p02@company.example") == "команда"              # подчинённый
    assert audience_of(ORG, "p10@company.example") == "команда"              # коллега
    assert audience_of(ORG, "buyer@romashka.example") == "внешний"


def test_rule_for_one_kind_does_not_touch_other_letters(env):
    llm = gen(SHORT)
    b = book(env, llm)
    b.teach("короче", kind="auto_reply")
    assert b.render("auto_reply", тема="x", подпись="П")[1].startswith("Спасибо, получил.")
    _, invite = b.render("invite", встреча="Планёрка", когда="12.12", длительность=30, ссылка="L", подпись="П")
    assert invite.startswith("Здравствуйте!\n\nПриглашаю")                       # приглашение не тронуто
    asked = next(u for s, u in llm.calls if "составь тексты" in s.lower())
    assert "Ключ: auto_reply" in asked and "Ключ: invite" not in asked           # модель переписывает только автоответ


def test_rule_for_one_audience_only(env):
    b = book(env, gen({"auto_reply": "Спасибо, получил.\n\n{подпись}",
                       "auto_reply@руководство": "Виктор Андреевич, спасибо, получил.\n\n{подпись}"}))
    b.teach("руководству — по имени-отчеству", kind="auto_reply", audience="руководство")
    assert b.render("auto_reply", "руководство", подпись="П")[1].startswith("Виктор Андреевич")
    assert b.render("auto_reply", "внешний", подпись="П")[1].startswith("Принято, подтверждаю.")   # клиентам — как было
    assert b.render("auto_reply", "команда", подпись="П")[1].startswith("Принято, подтверждаю.")


def test_rules_text_for_decision_drafts(env):
    j, _ = env
    b = book(env, gen(SHORT))
    b.teach("на «вы»", kind="*")
    b.teach("руководству без «ты»", kind="decision", audience="руководство")
    b.teach("команде коротко", kind="decision", audience="команда")
    b.teach("автоответ одной строкой", kind="auto_reply")
    boss = b.rules_text("decision", "руководство")
    assert len(boss.splitlines()) == 2 and len(b.rules_text("decision", "команда").splitlines()) == 2
    assert len(b.rules_text("decision").splitlines()) == 1                       # без адресата — только общие


def test_learned_rule_belongs_to_decision_kind_and_recipient(env, tmp_path):
    j, t = env
    b = StyleBook(gen(SHORT), j, t, style=STYLE, org=ORG)
    eid, _ = j.record_event("mail", "m-boss", {"from": "p09@company.example", "from_name": "Виктор Орлов",
                                               "subject": "Бюджет", "body": "..."})
    j.log_action("decision_draft_edit", actor="human", path="decision", event_id=eid,
                 payload={"key": "q1", "draft": "Подтверждаю.", "text": "Виктор Андреевич, спасибо, получил."})
    b.learn_pending()
    r = b.rules()[0]
    assert (r["kind"], r["audience"], r["source"]) == ("decision", "руководство", "ваша правка")
