"""Очередь «ждут только вас»: повторы склеены, передача ждёт кадрового решения, карточка с готовым решением (TASK_12)."""
import csv
import shutil
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from pomoshnik.assistant import Assistant
from pomoshnik.channels.telegram import Update
from pomoshnik.decisions import PREP_SYSTEM, group, names
from pomoshnik.eval_triage import evaluate
from pomoshnik.llm import StubLLM
from pomoshnik.metrics import compute_metrics
from pomoshnik.profile import load_profile
from pomoshnik.protocol import ProtocolStore
from yadro.clock import TZ_EKB, FakeClock
from yadro.journal import Journal

ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "data" / "task"
ROWS = list(csv.DictReader(open(TASK / "inbox.csv", encoding="utf-8")))
BY_SUBJECT = {r["subject"]: r["category"] for r in ROWS}
START = datetime(2026, 12, 7, 8, 30, tzinfo=TZ_EKB)
OFFER = "Кандидат на позицию аккаунт-менеджера"
ACCESS = "Доступ к CRM для нового сотрудника"
THESES = "Подготовить тезисы к совету директоров"


class FakePult:
    def __init__(self):
        self.sent = []

    def send_text(self, text):
        self.sent.append(("text", text, None))
        return len(self.sent)

    def send_buttons(self, text, buttons):
        self.sent.append(("buttons", text, [b.data for row in buttons for b in row]))
        return len(self.sent)

    def answer_callback(self, cid, text=""):
        pass


class FakeMail:
    def __init__(self, rows):
        self.rows, self.out = rows, []

    def fetch_new(self):
        rows, self.rows = self.rows, []
        return [SimpleNamespace(id=r["id"], from_name=r["from_name"], from_email=r["from_email"], subject=r["subject"],
                                body=r["body"], received=datetime.fromisoformat(r["received"]).replace(tzinfo=TZ_EKB))
                for r in rows]

    def send(self, to, subject, body, in_reply_to=None):
        self.out.append({"to": to, "subject": subject, "body": body, "in_reply_to": in_reply_to})
        return f"<{len(self.out)}@test>"


def source_id(user, kind):
    line = next((x for x in user.splitlines() if f"[{kind}]" in x), "")
    return line.split(" ", 1)[0]


def prep(user):
    """Модель ссылается на задачу из выданных источников, на должность и на несуществующий источник."""
    return {"draft": "Добрый день! Тезисы пришлю к 15.12.", "advice": "согласовать", "why": "задача уже в работе",
            "facts": [{"source": source_id(user, "задача") or "S1", "note": "задача в работе"},
                      {"source": source_id(user, "оргструктура") or "S1", "note": "кто пишет"},
                      {"source": "S99", "note": "выдумка"}]}


def answer(system, user):
    if system == PREP_SYSTEM:
        return prep(user)
    if "память помощника" in system:
        return {"facts": []}
    subj = user.split("Тема: ", 1)[1].split("\n", 1)[0]
    return {"category": BY_SUBJECT.get(subj, ""), "summary": subj, "confidence": 0.9}


def make(tmp_path, profile_src: Path, rows=ROWS, journal=None, llm=None):
    d = tmp_path / f"prof_{profile_src.parent.name}_{profile_src.name}"
    if not d.exists():
        d.mkdir()
        shutil.copy(profile_src, d / "profile.md")
    clock = FakeClock(START)
    j = journal or Journal(tmp_path / "j.sqlite", clock)
    ps = ProtocolStore(tmp_path / "p.sqlite", clock)
    mail = FakeMail(list(rows))
    a = Assistant(load_profile(d, org_path=TASK / "org_structure.csv"), llm or StubLLM(answer), FakePult(), mail, j, ps,
                  calendar_path=TASK / "calendar.ics", tasks_path=TASK / "tasks.csv")
    return a, mail


IVAN = ROOT / "profiles" / "ivan_petrov" / "profile.md"
CUSTOMER = TASK / "leader_profile.md"


@pytest.fixture
def a(tmp_path):
    asst, mail = make(tmp_path, IVAN)
    asst.process_inbox()
    yield asst, mail, tmp_path
    asst.j.close()


def fwd_to_assistant(m):
    return m["to"] == "p08@company.example" and m["subject"].startswith("Fwd:")


def grp(asst, subject):
    return next(g for g in asst.groups() if g["subject"] == subject)


def again(asst, tmp_path):
    """Тот же журнал, новый помощник — как после перезапуска стенда."""
    b, _ = make(tmp_path, IVAN, rows=[], journal=asst.j, llm=asst.llm)
    b.send_mail = asst.send_mail
    return b


# ---------------- склейка повторов ----------------
def test_repeats_are_one_question_with_count(a):
    asst, _, _ = a
    gs = asst.groups()
    assert sum(g["count"] for g in gs) == len(asst.decisions)
    assert len(gs) == len({(d["email"], d["subject"]) for d in asst.decisions}) <= 9
    assert grp(asst, OFFER)["count"] == sum(1 for r in ROWS if r["subject"] == OFFER) > 1


def test_one_decision_closes_all_copies_with_one_reply(a):
    asst, mail, tmp_path = a
    g = grp(asst, OFFER)
    before = len(mail.out)
    assert asst.decide_letter(g["event_ids"][-1], "no", notify=False)
    replies = [m for m in mail.out[before:] if m["subject"] == f"Re: {OFFER}"]
    assert len(replies) == 1
    assert not set(g["event_ids"]) & {d["event_id"] for d in asst.decisions}
    assert not set(g["event_ids"]) & {d["event_id"] for d in again(asst, tmp_path).decisions}


def test_later_moves_whole_question_to_the_end(a):
    asst, _, _ = a
    first = asst.groups()[0]
    asst.decide_letter(first["event_id"], "later", notify=False)
    assert asst.groups()[-1]["key"] == first["key"] and asst.groups()[-1]["count"] == first["count"]


def test_group_helper_keeps_first_seen_order_and_latest_letter():
    ds = [{"event_id": 1, "email": "A@x", "subject": "Тема", "at": "1", "id": "m1"},
          {"event_id": 2, "email": "b@x", "subject": "Другое", "at": "2", "id": "m2"},
          {"event_id": 3, "email": "a@x", "subject": "Re: тема", "at": "3", "id": "m3"}]
    gs = group(ds)
    assert [g["event_ids"] for g in gs] == [[1, 3], [2]]
    assert gs[0]["id"] == "m3" and gs[0]["at"] == "3"


# ---------------- доступы ждут кадрового решения ----------------
def test_names_match_across_cases():
    assert names("финалист — Сергей К., опыт 5 лет") & names("доступ для Сергея К.")
    assert not names("Иван, финалист", exclude=("Иван Петров",))


def test_access_waits_for_offer_then_goes_to_assistant(a):
    asst, mail, _ = a
    assert ACCESS not in {d["subject"] for d in asst.decisions}
    assert not [m for m in mail.out if fwd_to_assistant(m)]
    asst.decide_letter(grp(asst, OFFER)["event_id"], "yes", notify=False)
    fwd = [m for m in mail.out if fwd_to_assistant(m)]
    assert len(fwd) == 1 and ACCESS in fwd[0]["subject"]
    assert any("Сергей" in x or ACCESS in x for x in asst.digest)


def test_access_dropped_when_offer_refused(a):
    asst, mail, _ = a
    asst.decide_letter(grp(asst, OFFER)["event_id"], "no", notify=False)
    assert not [m for m in mail.out if fwd_to_assistant(m)]
    rel = [x for x in asst.j.actions() if x["kind"] == "delegation_released"]
    assert rel and all(x["payload"]["verdict"] == "no" for x in rel)


def test_waiting_access_survives_restart(a):
    asst, mail, tmp_path = a
    b = again(asst, tmp_path)
    b.decide_letter(grp(b, OFFER)["event_id"], "yes", notify=False)
    assert len([m for m in mail.out if fwd_to_assistant(m)]) == 1


def test_access_without_open_hr_question_goes_at_once(tmp_path):
    rows = [r for r in ROWS if r["subject"] == ACCESS][:1]
    asst, mail = make(tmp_path, IVAN, rows=rows)
    asst.process_inbox()
    assert [m["to"] for m in mail.out if m["subject"].startswith("Fwd:")] == ["p08@company.example"]


def test_ivan_profile_rules_match_reference(tmp_path):
    asst, _ = make(tmp_path, IVAN, rows=[])
    assert asst.p.rules["доступы"].action == "делегировать" and asst.p.rules["доступы"].delegate_to == "p08"
    res = evaluate(asst.p, ROWS)
    assert res["errors"] == [] and res["guarded_leaks"] == []


# ---------------- пересмотр очереди по новым правилам ----------------
def test_recheck_moves_old_queue_by_new_rules_without_double_count(tmp_path):
    old, _ = make(tmp_path, CUSTOMER)
    old.process_inbox()
    assert ACCESS in {d["subject"] for d in old.decisions}
    before = compute_metrics(old.j)
    new, mail = make(tmp_path, IVAN, rows=[], journal=old.j)
    assert new.recheck_queue() == 3
    assert ACCESS not in {d["subject"] for d in new.decisions}
    after = compute_metrics(new.j)
    assert after["letters"] == before["letters"] == 80
    assert after["letters_without_leader"] == before["letters_without_leader"] + 3
    assert new.recheck_queue() == 0                                       # повтор ничего не меняет
    new.decide_letter(grp(new, OFFER)["event_id"], "yes", notify=False)    # доступ ждал оффер и после пересмотра
    assert len([m for m in mail.out if fwd_to_assistant(m)]) == 1


# ---------------- карточка с готовым решением ----------------
def test_prepare_card_checks_sources(a):
    asst, _, _ = a
    assert asst.prepare_decisions() == len(asst.groups())
    p = grp(asst, THESES)["prepared"]
    assert p["draft"].startswith("Добрый день")
    assert [f["kind"] for f in p["facts"]] == ["задача", "оргструктура"]    # факт без источника выброшен
    task = p["facts"][0]
    assert task["text"] == task["source_text"] and task["text"].startswith("Тезисы к совету директоров")  # дословно
    assert task["note"] == "задача в работе"
    assert p["advice"] == "согласовать"
    prompt = next(u for s, u in asst.llm.calls if s == PREP_SYSTEM and THESES in u)
    assert "Коммерческий директор" in prompt.split("\n", 1)[0]             # кто пишет — видно модели
    assert asst.prepare_decisions() == 0                                   # готовое не пересчитывается


def test_external_sender_is_marked_for_model(a):
    asst, _, _ = a
    asst.prepare_decisions()
    prompt = next(u for s, u in asst.llm.calls if s == PREP_SYSTEM and "Ромашка" in u.split("\n", 1)[0])
    assert "внешний отправитель" in prompt.split("\n", 1)[0]


@pytest.mark.parametrize("facts", [
    [{"source": "S42", "note": "нет такого"}],                             # источника нет
    "only_org_and_goals",                                                  # должность и цель — не основание
])
def test_advice_needs_support_and_numbers_are_checked(tmp_path, facts):
    rows = [r for r in ROWS if r["subject"] == THESES][:1]

    def model(s, u):
        if s != PREP_SYSTEM:
            return answer(s, u)
        fs = facts if isinstance(facts, list) else \
            [{"source": source_id(u, "оргструктура")}, {"source": source_id(u, "цель квартала")}]
        return {"draft": "Пришлю 7 слайдов к 15.12.", "advice": "отказать", "why": "так", "facts": fs}

    asst, _ = make(tmp_path, IVAN, rows=rows, llm=StubLLM(model))
    asst.process_inbox()
    asst.prepare_decisions()
    p = asst.groups()[0]["prepared"]
    assert p["advice"] == "нет совета" and p["why"] == ""
    assert all(f["kind"] in ("оргструктура", "цель квартала") for f in p["facts"])
    assert p["unverified"] == ["7"]                                        # 15.12 есть в письме, 7 — нет


def test_yes_sends_prepared_draft_and_edit_is_logged(a):
    asst, mail, tmp_path = a
    asst.prepare_decisions()
    g = grp(asst, THESES)
    asst.decide_letter(g["event_id"], "yes", notify=False)
    assert mail.out[-1]["subject"] == f"Re: {THESES}" and "Тезисы пришлю к 15.12." in mail.out[-1]["body"]
    g2 = grp(asst, OFFER)
    asst.decide_letter(g2["event_id"], "yes", notify=False, text="Согласую оффер, выходим 15 января.")
    sent = [m for m in mail.out if m["subject"] == f"Re: {OFFER}"]
    assert "выходим 15 января" in sent[-1]["body"]
    assert any(x["kind"] == "decision_draft_edit" for x in asst.j.actions())
    assert compute_metrics(asst.j)["leader_edits"] == 1
    assert again(asst, tmp_path).groups()[0]["prepared"]                    # подготовка переживает перезапуск


def test_telegram_card_shows_count_advice_and_draft(a):
    asst, _, _ = a
    asst.prepare_decisions()
    while asst.groups()[0]["subject"] != THESES:
        asst.decide_letter(asst.groups()[0]["event_id"], "later", notify=False)
    asst.handle(Update(1, "callback", 555, "dec:next", 10, "cb"))
    kind, text, buttons = asst.pult.sent[-1]
    g = asst.groups()[0]
    assert f"пишут {g['count']}-й раз" in text and "Совет: согласовать" in text and "Тезисы пришлю" in text
    assert buttons[0] == f"dec:yes:{g['event_id']}"


# ---------------- факты из памяти: без самого письма и без повторов ----------------
def test_memory_facts_skip_the_letter_itself_and_repeats(tmp_path):
    from pomoshnik.decisions import PREP_VERSION, sources
    from pomoshnik.memory import Fact, Memory
    clock = FakeClock(START)
    mem = Memory(tmp_path / "m.sqlite", clock)
    at = START
    copy = "Подтвердите, пожалуйста, что запуск будет до 25 декабря"
    mem.add([Fact("Ромашка-Ритейл", "клиент", "просит подтвердить запуск", "Клиент", at, src, copy)
             for src in ("m001", "m023", "m068")])                                   # сам вопрос, три копии
    other = "Интеграция Ромашки идёт по плану, тестовый стенд готов"
    mem.add([Fact("Ромашка-Ритейл", "сделка", "интеграция по плану", "Ольга", at, "протокол 3", other),
             Fact("Ромашка-Ритейл", "сделка", "интеграция по плану", "Ольга", at, "m050", other)])   # повтор цитаты
    letter = "Сроки запуска интеграции\nКлиент «Ромашка-Ритейл»: " + copy
    srcs = [s for s in sources(letter, memory=mem, exclude=("m001", "m023", "m068")) if s["kind"] == "память"]
    assert len(srcs) == 1 and other in srcs[0]["text"]
    mem.close()
    assert PREP_VERSION >= 2


def test_old_prepared_cards_are_redone(a):
    asst, _, tmp_path = a
    asst.prepare_decisions()
    key = asst.groups()[0]["key"]
    asst.j.log_action("decision_prepared", payload={**asst.prepared[key], "v": 1})   # подготовка старой версии
    b = again(asst, tmp_path)
    assert b.groups()[0]["prepared"]["v"] >= 2


# ---------------- два черновика: на «да» и на «нет» ----------------
def two_drafts(user):
    return {"draft_yes": "Подтверждаю запуск до 25 декабря.", "draft_no": "Подтвердить 25 декабря не можем, вернёмся с датой.",
            "advice": "нет совета", "why": "", "facts": [{"source": source_id(user, "оргструктура") or "S1"}]}


def test_each_button_sends_its_own_draft(tmp_path):
    rows = [r for r in ROWS if r["subject"] == "Сроки запуска интеграции"]
    llm = StubLLM(lambda s, u: two_drafts(u) if s == PREP_SYSTEM else answer(s, u))
    asst, mail = make(tmp_path, IVAN, rows=rows, llm=llm)
    asst.process_inbox()
    asst.prepare_decisions()
    p = asst.groups()[0]["prepared"]
    assert p["draft_yes"].startswith("Подтверждаю") and p["draft_no"].startswith("Подтвердить 25 декабря не можем")
    asst.decide_letter(asst.groups()[0]["event_id"], "no", notify=False)
    assert "не можем" in mail.out[-1]["body"] and "Подтверждаю" not in mail.out[-1]["body"]
    assert not any(x["kind"] == "decision_draft_edit" for x in asst.j.actions())


def test_no_with_own_text_is_an_edit_and_old_single_draft_still_works(a):
    asst, mail, _ = a
    asst.prepare_decisions()                                           # заглушка отдаёт старый «draft» — это ответ на «да»
    g = grp(asst, THESES)
    assert g["prepared"]["draft_yes"] == g["prepared"]["draft"] and g["prepared"]["draft_no"] == ""
    asst.decide_letter(g["event_id"], "no", notify=False, text="Не успею к 15.12, давайте к 18.12.")
    assert "к 18.12" in mail.out[-1]["body"]
    assert any(x["kind"] == "decision_draft_edit" for x in asst.j.actions())


def test_telegram_shows_both_drafts(tmp_path):
    rows = [r for r in ROWS if r["subject"] == "Сроки запуска интеграции"][:1]
    llm = StubLLM(lambda s, u: two_drafts(u) if s == PREP_SYSTEM else answer(s, u))
    asst, _ = make(tmp_path, IVAN, rows=rows, llm=llm)
    asst.process_inbox()
    asst.prepare_decisions()
    asst.handle(Update(1, "callback", 555, "dec:next", 10, "cb"))
    text = asst.pult.sent[-1][1]
    assert "Если согласуете" in text and "Если откажете" in text and "не можем" in text
