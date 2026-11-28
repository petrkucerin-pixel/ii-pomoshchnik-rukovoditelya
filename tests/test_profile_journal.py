"""Профиль → «Журнал действий» (Пит 28.11). Журнал читается фразами «кто что сделал», по дням, без повторов
и служебных слов; фильтры «Помощник / Вы» и по разделам."""
from test_cabinet_api import env  # noqa: F401 — env — фикстура стенда на учебных данных


def flat(d):
    return [x for day in d["days"] for x in day["items"]]


def test_journal_reads_like_sentences_by_day(env):
    c, a, _ = env
    g = a.active_groups()[0]
    a.decide_letter(g["event_id"], "yes", notify=False)
    d = c.get("/api/profile/journal").json()
    items = flat(d)
    assert d["days"] and all(day["label"] for day in d["days"])
    texts = [x["text"] for x in items]
    assert any(t.startswith("Вы согласовали «") for t in texts)          # а не «yes: …»
    assert not any(t.startswith(("yes:", "no:", "later:")) or "no_session" in t for t in texts)
    decided = [x for x in items if x["text"].startswith("Вы согласовали «" + g["subject"])]
    assert len(decided) == 1                                             # копии одного письма — одна строка
    assert any(x["who"] == "Помощник" and x["text"].startswith("Передал ") for x in items)
    assert {x["group"] for x in items} <= set(d["groups"])
    assert d["counts"]["Вы"] >= 1 and d["counts"]["Помощник"] > d["counts"]["Вы"]
    mine = flat(c.get("/api/profile/journal", params={"who": "human"}).json())
    assert d["counts"]["Вы"] == len(mine)                               # счётчик = строки, а не копии писем


def test_journal_filters(env):
    c, a, _ = env
    a.decide_letter(a.active_groups()[0]["event_id"], "no", notify=False)
    mine = flat(c.get("/api/profile/journal", params={"who": "human"}).json())
    assert mine and all(x["who"] == "Вы" for x in mine)
    mail = flat(c.get("/api/profile/journal", params={"group": "Почта и решения"}).json())
    assert mail and all(x["group"] == "Почта и решения" for x in mail)
    assert c.get("/api/profile/journal", params={"group": "нет такого"}).status_code == 422
