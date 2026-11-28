"""Журнал действий: запись раскрывается — подробности (Пит 28.11: «хочется нажать и почитать подробнее»)."""
from test_cabinet_api import env  # noqa: F401 — env — фикстура стенда на учебных данных


def flat(d):
    return [x for day in d["days"] for x in day["items"]]


def detail(c, item):
    r = c.get(f"/api/profile/journal/{item['id']}")
    assert r.status_code == 200
    return r.json()


def test_every_row_opens(env):
    c, a, _ = env
    a.decide_letter(a.active_groups()[0]["event_id"], "no", text="Сейчас не можем, вернёмся в январе.", notify=False)
    items = flat(c.get("/api/profile/journal").json())
    for x in items[:40]:
        d = detail(c, x)
        assert d["title"] and (d["fields"] or d["blocks"] or d["note"])


def test_decision_shows_letter_and_what_was_sent(env):
    c, a, _ = env
    g = a.active_groups()[0]
    a.decide_letter(g["event_id"], "no", text="Сейчас не можем, вернёмся в январе.", notify=False)
    row = next(x for x in flat(c.get("/api/profile/journal").json()) if x["text"].startswith("Вы отказали"))
    d = detail(c, row)
    blocks = {b["label"]: b["text"] for b in d["blocks"]}
    assert g["subject"] in d["title"] or any(g["subject"] in v for v in blocks.values())
    assert any("вернёмся в январе" in v for v in blocks.values())            # что ушло отправителю
    assert any(f["label"] == "От" for f in d["fields"])                       # само письмо


def test_delegation_shows_whom_and_why(env):
    c, a, _ = env
    row = next(x for x in flat(c.get("/api/profile/journal").json()) if x["text"].startswith("Передал ответственному"))
    d = detail(c, row)
    labels = {f["label"]: f["value"] for f in d["fields"]}
    assert labels.get("Кому") and labels.get("Почему") and labels.get("От")


def test_telegram_text_is_kept_and_shown(env):
    c, a, _ = env
    import pomoshnik.server as srv
    srv.stand.pult.target = type("T", (), {"send_text": lambda self, t: 1, "send_buttons": lambda self, t, b: 2})()
    a.morning_plan(a.today())
    row = next(x for x in flat(c.get("/api/profile/journal", params={"group": "Telegram"}).json())
               if x["text"].startswith("Прислал утренний план"))
    d = detail(c, row)
    assert any("Доброе утро" in b["text"] and "<b>" not in b["text"] for b in d["blocks"])
    assert not any(x["text"].startswith("tg") for x in flat(c.get("/api/profile/journal").json()))   # сами отправки — не строки


def test_unknown_id_is_404(env):
    c, _, _ = env
    assert c.get("/api/profile/journal/999999").status_code == 404



def test_task_row_explains_itself_without_technical_fields(env):
    """Пит 28.11: «Не понял. Что тут?» — было «Task id t007 / Action ask»."""
    c, a, _ = env
    from pomoshnik import cabinet
    cabinet.chase_overdue(a)
    row = next(x for x in flat(c.get("/api/profile/journal", params={"group": "Протоколы и задачи"}).json())
               if x["text"].startswith("Спросил исполнителя ("))
    d = detail(c, row)
    labels = {f["label"]: f["value"] for f in d["fields"]}
    assert "просрочена" in labels["Что сделано"] and labels["Исполнитель"] and labels["Срок"]
    assert not any(f["label"].lower() in ("task id", "action") for f in d["fields"])
    assert any(b["label"].startswith("Письмо исполнителю") for b in d["blocks"])
    assert d["links"][0]["href"].startswith("#meetings/t/")



def test_meeting_link_only_for_meetings_in_calendar(env):
    c, a, _ = env
    a.j.log_action("robot_started", path="now", summary="Проверка: робот заходит в Телемост",
                   payload={"uid": "e2e@test", "title": "Проверка", "link": "https://telemost.yandex.ru/j/1"})
    uid = a.meetings[0].uid
    a.j.log_action("robot_started", path="now", summary=f"{a.meetings[0].title}: робот заходит в Телемост",
                   payload={"uid": uid, "title": a.meetings[0].title, "link": "https://telemost.yandex.ru/j/2"})
    rows = [x for x in flat(c.get("/api/profile/journal").json()) if x["text"].startswith("Аватар зашёл")]
    links = {detail(c, x)["title"]: detail(c, x)["links"] for x in rows}
    assert links["Проверка"] == [] and links[a.meetings[0].title][0]["href"] == "#meetings/m/" + uid



def test_mail_draft_is_mail_not_telegram(env):
    """Пит 28.11: «В ТГ не вижу ничего такого?» — черновик ответа в почте попал в раздел «Telegram»."""
    c, a, _ = env
    a.j.log_action("reply_draft", path="now", summary="черновик ответа: Скидка 12%",
                   payload={"letter_id": "m079", "text": "Ольга, согласую 12%."})
    a.j.log_action("something_new", path="now", summary="новое действие", payload={})
    items = flat(c.get("/api/profile/journal").json())
    draft = next(x for x in items if "черновик ответа" in x["text"])
    assert draft["group"] == "Почта и решения" and "ждёт вас в почте" in draft["text"]
    assert next(x for x in items if x["text"] == "новое действие")["group"] == "Прочее"
    d = detail(c, draft)
    assert any("Ольга, согласую 12%" in b["text"] for b in d["blocks"]) and "В Telegram его не было" in d["note"]
    assert not flat(c.get("/api/profile/journal", params={"group": "Telegram"}).json()) or all(
        x["text"].startswith(("Прислал", "Telegram")) for x in flat(c.get("/api/profile/journal", params={"group": "Telegram"}).json()))


def test_spam_is_not_remembered():
    from pomoshnik.assistant import MEMORY_SKIP
    assert "спам" in MEMORY_SKIP
