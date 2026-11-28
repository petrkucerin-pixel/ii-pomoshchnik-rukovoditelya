"""Контракт API кабинета (TASK_07). Ответы сверяются с примерами tests/fixtures/cabinet/, по которым верстает Codex."""
import csv
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import pomoshnik.server as srv
from pomoshnik.llm import StubLLM
from pomoshnik.profile import load_profile
from pomoshnik.triage import CLASSIFY_SYSTEM, decide

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures" / "cabinet"
ROWS = list(csv.DictReader(open(ROOT / "data" / "task" / "inbox.csv", encoding="utf-8"))) + \
    list(csv.DictReader(open(ROOT / "data" / "demo" / "spam.csv", encoding="utf-8")))
BY_SUBJECT = {r["subject"]: r["category"] for r in ROWS}
PROTO = {"topics": [], "decisions": [],
         "tasks": [{"owner": "Сергей Морозов", "what": "Подписать договор", "due_text": "к пятнице",
                    "quote": "договор с «Облако Плюс» нужно подписать к пятнице"}],
         "open_questions": ["Бюджет не решён"]}
TRANSCRIPT = "Сергей, договор с «Облако Плюс» нужно подписать к пятнице."


def answer(system, user):
    if "секретарь" in system:
        return PROTO
    if "память помощника" in system:
        if "ждём до 12.12" in user:
            return {"facts": [{"topic": "Облако Плюс", "kind": "партнёр", "what": "ждём до 12.12",
                               "quote": "Договор на подписи у юристов"}]}
        return {"facts": []}
    if "шаблон" in system.lower():
        return {"reply": "Сделал короче.", "subject": "Fwd: {тема}", "body": "{имя}, возьмите в работу. {zzz}"}
    subj = user.split("Тема: ", 1)[1].split("\n", 1)[0]
    return {"category": BY_SUBJECT.get(subj, ""), "summary": subj, "confidence": 0.9}


@pytest.fixture
def env(tmp_path, monkeypatch):
    prof = tmp_path / "profiles"
    shutil.copytree(ROOT / "profiles", prof)
    monkeypatch.setattr(srv, "RUNTIME", tmp_path / "rt")
    monkeypatch.setattr(srv, "PROFILES", prof)
    monkeypatch.setattr(srv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("DEMO_DATE", "2026-12-11")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("MAIL_PROVIDER", raising=False)
    with TestClient(srv.app) as c:
        a = srv.stand.assistant
        a.llm = srv.stand.llm = StubLLM(answer)
        a.process_inbox()
        yield c, a, prof
    srv.stand.close()


def fixture(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def same_shape(real, example, path="$"):
    """Все поля примера есть в ответе; для списков — у первого элемента."""
    if isinstance(example, dict):
        assert isinstance(real, dict), path
        for k, v in example.items():
            assert k in real, f"{path}.{k}"
            if k in ("metrics", "cards", "rules"):      # подписи и категории зависят от данных
                continue
            if real[k] is not None and v is not None:
                same_shape(real[k], v, f"{path}.{k}")
    elif isinstance(example, list) and example and real:
        same_shape(real[0], example[0], f"{path}[0]")


@pytest.mark.parametrize("name,url", [
    ("today", "/api/today"), ("mail", "/api/mail"), ("templates", "/api/templates"),
    ("meetings", "/api/meetings?start=2026-12-07&days=7"), ("meeting", "/api/meetings/ev001@company.example"),
    ("tasks", "/api/tasks"), ("memory", "/api/memory"), ("profile", "/api/profile"), ("metrics", "/api/metrics"),
    ("journal", "/api/journal?limit=5"),
])
def test_get_answers_have_fixture_shape(env, name, url):
    c, _, _ = env
    r = c.get(url)
    assert r.status_code == 200, r.text
    same_shape(r.json(), fixture(name))


def test_protocols_edits_and_discuss_have_fixture_shape(env):
    c, _, _ = env
    pid = c.post("/api/meetings/transcript", json={"title": "Планёрка отдела", "transcript": TRANSCRIPT,
                                                    "minutes_ago": 5}).json()["protocol_id"]
    same_shape(c.get("/api/protocols").json(), fixture("protocols"))
    same_shape(c.post(f"/api/protocols/{pid}/edit", json={"instruction": "срок — пятница"}).json(),
               fixture("protocols")["protocols"][0])
    same_shape(c.post("/api/templates/delegate/discuss",
                      json={"message": "короче", "subject": "Fwd: {тема}", "body": "{имя}, возьмите"}).json(),
               fixture("template_discuss"))
    c.put("/api/templates/delegate", json={"subject": "Fwd: {тема}", "body": "{имя}, прошу взять в работу."})
    same_shape(c.get("/api/edits").json(), fixture("edits"))


# ---------------- переписка ----------------
def letters(c, **kw):
    return [x for x in c.get("/api/mail").json()["letters"] if all(x.get(k) == v for k, v in kw.items())]


def test_mail_groups(env):
    c, _, _ = env
    m = {x["id"]: x for x in letters(c, direction="in")}
    assert (m["m001"]["group"], m["m001"]["party"]) == ("counterparty", "Ромашка-Ритейл")
    assert (m["m022"]["group"], m["m022"]["party"]) == ("team", "Сергей Морозов")
    viktor = next(x for x in m.values() if x["from_email"] == "p09@company.example")
    assert viktor["group"] == "internal" and viktor["party_note"] == "Коммерческий директор"
    assert {x["group"] for x in m.values() if x["category"] == "рассылка"} == {"newsletter"}
    assert len(m) == 84 and {x["category"] for x in m.values() if x["group"] == "spam"} == {"спам"}
    keys = [g["key"] for g in c.get("/api/mail").json()["groups"]]
    assert keys == ["counterparty", "team", "internal", "newsletter", "spam"]


def test_outgoing_letters_are_grouped_by_recipient(env):
    c, _, _ = env
    out = letters(c, direction="out")
    fwd = [x for x in out if x["subject"].startswith("Fwd:")]
    assert fwd and all(x["group"] == "team" for x in fwd)
    assert any(x["category"] == "вопрос_ревизора" for x in out)
    assert all(x["to_email"] for x in out)


# ---------------- решения ----------------
def test_decision_from_cabinet_sends_letter_and_survives_restart(env):
    c, a, _ = env
    dec = c.get("/api/today").json()["decisions"]
    asked = sum(1 for x in a.j.actions() if x["kind"] == "mail_спросить_руководителя")
    assert sum(d["count"] for d in dec) == asked > len(dec) > 0          # повторы склеены в один вопрос
    assert all("prepared" in d for d in dec)
    first = dec[0]
    assert c.post(f"/api/decisions/{first['event_id']}",
                  json={"verdict": "yes", "text": "Подтверждаю, работаем."}).json() == {"ok": True}
    assert first["event_id"] not in [d["event_id"] for d in c.get("/api/today").json()["decisions"]]
    sent = letters(c, direction="out", category="решение")
    assert sent and sent[-1]["subject"] == f"Re: {first['subject']}"
    srv.stand.restart_assistant()
    after = srv.stand.assistant.groups()
    assert first["event_id"] not in [g["event_id"] for g in after] and len(after) == len(dec) - 1
    assert c.post("/api/decisions/999999", json={"verdict": "yes"}).status_code == 404
    assert c.post(f"/api/decisions/{dec[1]['event_id']}", json={"verdict": "maybe"}).status_code == 422


# ---------------- шаблоны ----------------
def test_templates_list(env):
    c, _, _ = env
    keys = {t["key"] for t in c.get("/api/templates").json()["templates"]}
    assert keys == {"delegate", "auto_reply", "review_question", "protocol_mail", "decision_yes", "decision_no", "invite", "assign",
                    "task_go"}


def test_template_edit_changes_next_letter_and_is_logged(env):
    c, a, prof = env
    r = c.put("/api/templates/delegate", json={"subject": "Поручение: {тема}", "body": "{имя}, прошу взять в работу."})
    assert r.status_code == 200 and r.json()["body"] == "{имя}, прошу взять в работу."
    assert (prof / "ivan_petrov" / "templates.json").exists()
    row = next(r for r in ROWS if r["category"] == "продажи")
    a.mail = type("M", (), {"fetch_new": lambda self: [type("E", (), dict(
        id="new1", from_name=row["from_name"], from_email=row["from_email"], subject=row["subject"] + " (2)",
        body=row["body"], received=None))()]})()
    BY_SUBJECT[row["subject"] + " (2)"] = "продажи"
    a.process_inbox()
    out = letters(c, direction="out")[-1]
    assert out["subject"].startswith("Поручение: ") and "прошу взять в работу" in out["body"]
    assert any(x["kind"] == "template_edit" for x in a.j.actions())


def test_template_unknown_variable_is_422(env):
    c, _, _ = env
    r = c.put("/api/templates/delegate", json={"subject": "x", "body": "{zzz}"})
    assert r.status_code == 422 and "zzz" in r.json()["detail"]
    assert c.put("/api/templates/nope", json={"subject": "x", "body": "y"}).status_code == 404


def test_template_discuss_does_not_save_and_flags_unknown(env):
    c, _, _ = env
    before = c.get("/api/templates").json()
    r = c.post("/api/templates/delegate/discuss", json={"message": "короче", "subject": "Fwd: {тема}", "body": "x"}).json()
    assert r["reply"] == "Сделал короче." and "{zzz}" in r["body"] and r["problems"]
    assert c.get("/api/templates").json() == before


# ---------------- спам ----------------
def test_spam_category_is_known_and_always_ignored(tmp_path):
    assert "спам" in CLASSIFY_SYSTEM
    d = tmp_path / "p"
    d.mkdir()
    shutil.copy(ROOT / "data" / "task" / "leader_profile.md", d / "profile.md")
    p = load_profile(d, org_path=ROOT / "data" / "task" / "org_structure.csv")
    from pomoshnik.profile import Rule
    p.rules["спам"] = Rule("делегировать", "p02")
    assert decide(p, "спам", "x@spam.example", confidence=0.99).action == "игнорировать"


# ---------------- совещания ----------------
def test_meetings_week_and_card(env):
    c, _, _ = env
    w = c.get("/api/meetings", params={"start": "2026-12-07", "days": 7}).json()
    starts = [m["start"] for m in w["meetings"]]
    assert starts == sorted(starts) and all("2026-12-07" <= s[:10] <= "2026-12-13" for s in starts) and len(starts) >= 20
    card = c.get("/api/meetings/ev001@company.example").json()
    names = {x["name"] for x in card["meeting"]["attendees"]}
    assert card["tasks"] and all(t["owner"] in names for t in card["tasks"])
    assert card["avatar_url"].startswith("/avatar?title=")
    assert c.get("/api/meetings/nope").status_code == 404


def test_transcript_protocol_linked_to_meeting(env):
    c, _, _ = env
    m = c.get("/api/meetings/ev001@company.example").json()["meeting"]
    pid = c.post("/api/meetings/transcript", json={"title": m["title"], "transcript": TRANSCRIPT,
                                                    "minutes_ago": 0, "meeting_uid": m["uid"]}).json()["protocol_id"]
    p = next(x for x in c.get("/api/protocols").json()["protocols"] if x["id"] == pid)
    assert p["status"] == "черновик" and p["tasks"][0]["owner"] == "Сергей Морозов"
    card = c.get(f"/api/meetings/{m['uid']}").json()
    assert card["meeting"]["protocol_id"] == pid and card["protocol"]["id"] == pid
    assert c.post(f"/api/protocols/{pid}/approve").json() == {"ok": True}
    assert c.post(f"/api/protocols/{pid}/approve").status_code == 409


def test_memory_and_profile(env):
    c, _, _ = env
    mem = c.get("/api/memory", params={"q": "облака"}).json()
    assert mem["topics"][0]["topic"] == "Облако Плюс" and mem["topics"][0]["history"][0]["sources"]
    assert c.get("/api/memory", params={"q": "Марс"}).json()["topics"] == []
    p = c.get("/api/profile").json()
    assert p["name"] == "Иван Петров" and "деньги" in p["confirm"]
    assert len(c.get("/api/journal", params={"limit": 3}).json()["actions"]) == 3


def test_metrics_count_incoming_letters_only(env):
    c, a, _ = env
    m = c.get("/api/metrics").json()
    assert any(x["kind"] == "mail_sent" for x in a.j.actions())
    assert m["letters"] == len(ROWS) == 84


# ---------------- новая встреча кликом по слоту (TASK_10) ----------------
NEW = {"title": "Разбор КП «ТехноСтрой»", "start": "2026-12-11T17:00", "minutes": 45, "category": "клиент",
       "attendees": ["Ольга Смирнова", "Мария Лебедева"], "emails": ["pit@example.org"],
       "link": "https://telemost.yandex.ru/j/123", "avatar": True}


def test_create_meeting_shows_in_calendar_and_sends_invites(env):
    c, a, _ = env
    r = c.post("/api/meetings", json=NEW)
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["title"] == NEW["title"] and m["start"].startswith("2026-12-11T17:00") and m["end"].startswith("2026-12-11T17:45")
    week = c.get("/api/meetings", params={"start": "2026-12-07", "days": 7}).json()["meetings"]
    assert m["uid"] in [x["uid"] for x in week]
    card = c.get(f"/api/meetings/{m['uid']}").json()["meeting"]
    assert card["link"] == NEW["link"] and card["avatar"] is True
    assert {x["name"] for x in card["attendees"]} == {"Ольга Смирнова", "Мария Лебедева", "pit@example.org"}
    inv = [x for x in letters(c, direction="out") if x["category"] == "приглашение"]
    assert {x["to_email"] for x in inv} == {"p02@company.example", "p06@company.example", "pit@example.org"}
    assert all(NEW["link"] in x["body"] and "11.12 17:00" in x["body"] for x in inv)
    srv.stand.restart_assistant()                                         # после перезапуска стенда встреча на месте
    assert m["uid"] in [x.uid for x in srv.stand.assistant.meetings]


@pytest.mark.parametrize("bad", [{"title": " "}, {"start": "завтра"}, {"minutes": 2}, {"link": "ftp://x"},
                                 {"attendees": ["Марсианин"]}, {"emails": ["не-адрес"]}])
def test_create_meeting_validation(env, bad):
    c, _, _ = env
    r = c.post("/api/meetings", json=NEW | bad)
    assert r.status_code == 422


def test_avatar_flag_brings_robot_whatever_category(env):
    c, a, _ = env
    m = c.post("/api/meetings", json=NEW | {"category": "клиент"}).json()
    ap = srv.stand.autopilot
    assert m["uid"] in [x.uid for x in ap._meetings(a)]


def test_people_for_meeting_form(env):
    c, _, _ = env
    people = c.get("/api/people").json()["people"]
    assert {"name": "Ольга Смирнова", "position": "Директор по продажам SMB", "email": "p02@company.example"} in people
    assert all(p["name"] != "Иван Петров" for p in people)                 # руководитель приглашает, а не приглашается


# ---------------- кто ведёт встречу: я или мой ИИ-аватар ----------------
def test_lead_choice_overrides_category_and_drives_autopilot(env):
    c, a, _ = env
    uid = "ev001@company.example"                                   # операционная — по умолчанию ведёт аватар
    assert c.get(f"/api/meetings/{uid}").json()["meeting"]["avatar"] is True
    assert c.put(f"/api/meetings/{uid}/lead", json={"avatar": False}).json() == {"ok": True}
    assert c.get(f"/api/meetings/{uid}").json()["meeting"]["avatar"] is False
    assert uid not in [m.uid for m in srv.stand.autopilot._meetings_all(a)]
    client_meeting = next(m for m in a.meetings if m.category == "клиент")
    c.put(f"/api/meetings/{client_meeting.uid}/lead", json={"avatar": True})
    assert client_meeting.uid in [m.uid for m in srv.stand.autopilot._meetings_all(a)]
    srv.stand.restart_assistant()                                    # выбор хранится в журнале
    assert c.get(f"/api/meetings/{uid}").json()["meeting"]["avatar"] is False
    assert c.put("/api/meetings/nope/lead", json={"avatar": True}).status_code == 404


def test_link_saved_by_put_shows_in_card(env):
    c, _, _ = env
    uid = "ev001@company.example"
    c.put(f"/api/meetings/{uid}/link", json={"link": "https://telemost.yandex.ru/j/9"})
    m = c.get(f"/api/meetings/{uid}").json()["meeting"]
    assert m["link"] == "https://telemost.yandex.ru/j/9" and "avatar_at" in m


# ---------------- риски: подробности по кнопке ----------------
def test_risk_has_details_source_letters_and_what_assistant_did(env):
    c, a, _ = env
    d = c.get("/api/today").json()
    assert d["risks"] and len(d["risk_items"]) == len(d["risks"])
    r = d["risk_items"][0]
    assert r["text"] == d["risks"][0] and r["day"]
    src = r["sources"][0]
    assert src["from"] and src["subject"] and src["body"] and src["at"]          # отчёт, из которого риск
    assert "%" in src["body"] or any(ch.isdigit() for ch in src["body"])
    assert all(x["to_name"] and x["question"] for x in r["actions"])           # кому помощник уже написал


def test_risk_shows_only_related_actions_and_what_leader_should_do(env):
    c, _, _ = env
    r = c.get("/api/today").json()["risk_items"][0]
    author = r["sources"][0]["from"]
    assert r["actions"] and all(x["to_name"] == author for x in r["actions"])      # чужие отчёты не примешаны
    nxt = r["next"]
    assert "не хватает 6 %" in nxt["text"] and "ничего не нужно" in nxt["text"]
    assert nxt["meeting"] and nxt["meeting"]["title"] in nxt["then"]


# ---------------- задача: страница и что дальше ----------------
def test_task_page_has_context_contradiction_and_next_step(env):
    c, _, _ = env
    r = c.get("/api/tasks/t001")
    assert r.status_code == 200
    t = r.json()
    assert t["task"]["task"] == "Подготовить КП для «ТехноСтрой»" and t["task"]["owner"] == "Ольга Смирнова"
    assert any("ТехноСтрой" in x["from"] + x["subject"] for x in t["letters"])     # письма клиента по теме
    assert "не наступил" in t["task"]["why"]                                        # «просрочена», а срок 24.12
    assert t["next"]["text"] and set(t["next"]["actions"]) == {"remind", "move", "done"}
    assert c.get("/api/tasks/nope").status_code == 404


def test_task_actions_remind_move_done(env):
    c, a, _ = env
    before = len(letters(c, direction="out"))
    assert c.post("/api/tasks/t001/action", json={"action": "remind"}).status_code == 200
    out = letters(c, direction="out")
    assert len(out) == before + 1 and out[-1]["to_email"] == "p02@company.example"
    assert "Подготовить КП для «ТехноСтрой»" in out[-1]["subject"]
    assert c.get("/api/tasks/t001").json()["history"][-1]["action"] == "remind"
    c.post("/api/tasks/t001/action", json={"action": "move", "due": "2026-12-18"})
    t = next(x for x in c.get("/api/tasks").json()["tasks"] if x["id"] == "t001")
    assert t["due"] == "2026-12-18" and t["status"] == "перенесена" and not t["overdue"]
    c.post("/api/tasks/t001/action", json={"action": "done"})
    t = next(x for x in c.get("/api/tasks").json()["tasks"] if x["id"] == "t001")
    assert t["status"] == "выполнена" and not t["overdue"]
    srv.stand.restart_assistant()
    assert next(x for x in c.get("/api/tasks").json()["tasks"] if x["id"] == "t001")["status"] == "выполнена"
    assert c.post("/api/tasks/t001/action", json={"action": "fly"}).status_code == 422
    assert c.post("/api/tasks/t001/action", json={"action": "move"}).status_code == 422      # без нового срока


def test_every_overdue_task_says_why(env):
    c, _, _ = env
    d = c.get("/api/today").json()
    assert d["overdue"] and all(t["why"] for t in d["overdue"])
    by_id = {t["id"]: t for t in c.get("/api/tasks").json()["tasks"]}
    assert "ещё не наступил" in by_id["t001"]["why"]                              # статус из данных, срок 24.12
    assert "прошёл" in by_id["t005"]["why"]                                       # срок 01.12, сегодня 11.12
    assert all(not t["why"] for t in by_id.values() if not t["overdue"])


# ---------------- просроченные задачи: помощник выясняет сам ----------------
def test_assistant_asks_owners_of_overdue_tasks_itself_once(env):
    from pomoshnik import cabinet
    c, a, _ = env
    before = len(letters(c, direction="out"))
    n = cabinet.chase_overdue(a)
    out = letters(c, direction="out")[before:]
    overdue = [t for t in c.get("/api/tasks").json()["tasks"] if t["overdue"]]
    assert n == len(out) == len({t["id"] for t in overdue if t["owner"] != "Иван Петров"}) > 0
    kp = next(x for x in out if "Подготовить КП" in x["subject"])
    assert kp["to_email"] == "p02@company.example" and "24.12" in kp["body"] and "просрочена" in kp["body"]
    late = next(x for x in out if "Подписать договор" in x["subject"])
    assert "прошёл" in late["body"] and "новый срок" in late["body"]
    assert cabinet.chase_overdue(a) == 0                                        # второй раз не пишет
    p = c.get("/api/tasks/t001").json()
    assert "выясняет" in p["task"]["why"] and "ждём ответ" in p["next"]["text"]


def test_owner_answer_shows_on_task_page(env):
    from pomoshnik import cabinet
    c, a, _ = env
    cabinet.chase_overdue(a)
    a.clock.advance(hours=1) if hasattr(a.clock, "advance") else None
    eid, _ = a.j.record_event("mail", "reply-t001", {"from": "p02@company.example", "from_name": "Ольга Смирнова",
                                                       "subject": "Re: Задача «Подготовить КП для «ТехноСтрой»»",
                                                       "body": "Срок не сдвигался, статус ошибочный — КП отправлю 20.12.",
                                                       "received": "2099-01-01T10:00:00+05:00"})
    a.j.log_action("mail_в_сводку", event_id=eid, payload={"category": "статус"})
    p = c.get("/api/tasks/t001").json()
    assert p["answer"] and "статус ошибочный" in p["answer"]["body"]
    assert "Ответ пришёл" in p["next"]["text"]


# ---------------- сквозная цепочка: спросил → ответ → память → следующая встреча ----------------
class ReplyBox:
    def __init__(self, letters):
        self.letters = letters

    def fetch_new(self):
        out, self.letters = self.letters, []
        return out

    def send(self, *a, **k):
        return "<x>"


def test_owner_reply_goes_to_memory_not_to_leader_and_reaches_next_meeting(env):
    from types import SimpleNamespace
    from datetime import date
    from pomoshnik import cabinet
    c, a, _ = env
    cabinet.chase_overdue(a)
    asked = next(x for x in letters(c, direction="out") if "Подготовить КП" in x["subject"])
    queue = len(a.decisions)
    reply = SimpleNamespace(id="reply-kp", from_name="Ольга Смирнова", from_email="p02@company.example",
                            to="p01@company.example", subject="Re: " + asked["subject"],
                            body="Статус в списке ошибочный, срок не сдвигался. КП для «ТехноСтрой» отправлю клиенту 20.12.",
                            received=a.clock.now())
    a.mail = ReplyBox([reply])           # модель-заглушка не узнаёт тему — код должен узнать ответ сам
    a.process_inbox()
    assert len(a.decisions) == queue                                         # к руководителю не попал
    route = [x for x in a.j.actions() if x["kind"].startswith("mail_") and x["kind"] != "mail_sent"][-1]
    assert route["kind"] == "mail_в_сводку" and "ответ" in route["payload"]["reason"]
    mem = c.get("/api/memory?q=ТехноСтрой").json()
    assert any("20.12" in h["what"] or "20.12" in h["quote"] for t in mem["topics"] for h in t["history"])
    page = c.get("/api/tasks/t001").json()
    assert page["answer"] and "перенёс" in page["next"]["text"]                # назвала 20.12 — срок перенесён сам
    day = date.fromisoformat(c.get("/api/today").json()["day"])
    meet = next(m for m in a.meetings if m.start.date() > day and "Ольга Смирнова" in m.attendees)
    card = c.get(f"/api/meetings/{meet.uid}").json()
    assert any("Подготовить КП" in q and "20.12" in q for q in card["history_questions"])   # аватар спросит по ответу
    late = next(m for m in a.meetings if m.start.date() > day and "Алексей Новиков" in m.attendees)
    hq = c.get(f"/api/meetings/{late.uid}").json()["history_questions"]
    assert any("Обновить прогноз" in q and "ответа нет" in q for q in hq)                  # не ответил — спросит


# ---------------- исполнитель назвал дату: перенос, если не расходится с планами ----------------
def _reply_to_ask(c, a, task_word, owner, email, body):
    from types import SimpleNamespace
    from pomoshnik import cabinet
    cabinet.chase_overdue(a)
    asked = next(x for x in letters(c, direction="out") if x["subject"].startswith("Задача «") and task_word in x["subject"])
    a.mail = ReplyBox([SimpleNamespace(id=f"reply-{task_word}", from_name=owner, from_email=email,
                                       to="p01@company.example", subject="Re: " + asked["subject"], body=body,
                                       received=a.clock.now())])
    a.process_inbox()


def test_named_date_without_conflict_moves_deadline_itself(env):
    c, a, _ = env
    _reply_to_ask(c, a, "Подготовить КП", "Ольга Смирнова", "p02@company.example",
                  "Статус ошибочный. КП для «ТехноСтрой» отправлю клиенту 20.12.")
    t = next(x for x in c.get("/api/tasks").json()["tasks"] if x["id"] == "t001")
    assert t["due"] == "2026-12-20" and t["status"] == "перенесена" and not t["overdue"]
    p = c.get("/api/tasks/t001").json()
    assert "перенёс" in p["next"]["text"] and p["history"][-1]["action"] == "move"


def test_named_date_conflicting_with_plans_goes_to_leader(env):
    c, a, _ = env
    _reply_to_ask(c, a, "Подписать договор", "Сергей Морозов", "p05@company.example",
                  "Юристы ещё смотрят, подпишем 22.12.")
    t = next(x for x in c.get("/api/tasks").json()["tasks"] if x["id"] == "t005")
    assert t["due"] == "2026-12-01"                                             # срок не тронут
    p = c.get("/api/tasks/t005").json()
    nxt = p["next"]["text"]                      # ближайшая встреча с партнёром: 11.12 (если ещё впереди) или 18.12
    assert p["proposal"]["due"] == "2026-12-22" and "Облако Плюс" in nxt and ("11.12" in nxt or "18.12" in nxt)


def test_reply_without_date_changes_nothing(env):
    c, a, _ = env
    _reply_to_ask(c, a, "Подготовить КП", "Ольга Смирнова", "p02@company.example", "Работаю, всё в порядке.")
    t = next(x for x in c.get("/api/tasks").json()["tasks"] if x["id"] == "t001")
    assert t["due"] == "2026-12-24" and c.get("/api/tasks/t001").json()["proposal"] is None


# ---------------- переписка: сразу видно, где нужна реакция руководителя ----------------
def test_every_incoming_letter_has_clear_status(env):
    c, a, _ = env
    d = c.get("/api/mail").json()
    inc = [x for x in d["letters"] if x["direction"] == "in"]
    assert all(x["status"]["kind"] in ("you", "done", "wait", "info") and x["status"]["label"] for x in inc)
    you = [x for x in inc if x["status"]["kind"] == "you"]
    assert len(you) == len(a.decisions) and d["counts"]["you"] == len(a.groups())      # вопросы, а не копии
    assert not any("_" in x["status"]["label"] for x in inc)                            # без служебных слов
    by = {x["id"]: x["status"] for x in inc}
    assert by["m002"]["kind"] == "done" and by["m002"]["label"].startswith("Передал")   # КП → Ольге
    assert any(s["kind"] == "info" for s in by.values())                                # рассылки, спам
    first = c.get("/api/today").json()["decisions"][0]
    c.post(f"/api/decisions/{first['event_id']}", json={"verdict": "no"})
    st = {x["id"]: x["status"] for x in c.get("/api/mail").json()["letters"] if x["direction"] == "in"}
    decided = [s for s in st.values() if s["label"].startswith("Вы отказали")]
    assert decided and all(s["kind"] == "done" for s in decided)


def test_sent_letters_say_who_initiated_and_link_to_incoming(env):
    c, _, _ = env
    first = c.get("/api/today").json()["decisions"][0]
    c.post(f"/api/decisions/{first['event_id']}", json={"verdict": "yes"})
    out = [x for x in c.get("/api/mail").json()["letters"] if x["direction"] == "out"]
    assert all(x["status"]["kind"] in ("assistant", "you") and x["status"]["label"] for x in out)
    fwd = next(x for x in out if x["subject"].startswith("Fwd:"))
    assert fwd["status"]["kind"] == "assistant" and fwd["reply_to"]                 # к какому входящему
    mine = [x for x in out if x["status"]["label"] == "По вашему решению"]
    assert mine and mine[-1]["reply_to"]
