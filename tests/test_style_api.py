"""TASK_14: «Профиль → Как я пишу» — API. Окружение стенда — как в контракте кабинета."""
from test_cabinet_api import BY_SUBJECT, ROWS, answer, env, letters  # noqa: F401 — env — фикстура

from pomoshnik.llm import StubLLM

SHORT = {"auto_reply": "Спасибо, получил.\n\n{подпись}"}


def style_answer(system, user):
    if "составь тексты" in system.lower():
        return {"letters": SHORT}
    if "научить" in system.lower() or "как писать письма" in system.lower():
        return {"rules": ["Подтверждения — одной строкой: «Спасибо, получил»."], "reply": "Запомнил."}
    return answer(system, user)


def test_style_overview_teach_and_forget(env):
    c, a, _ = env
    a.llm = StubLLM(style_answer)
    d = c.get("/api/style").json()
    assert d["style"] and d["rules"] == [] and {x["key"] for x in d["letters"]} >= {"auto_reply", "invite", "delegate"}
    first = next(x for x in d["letters"] if x["key"] == "auto_reply")
    assert first["source"] == "стандартный" and "{" not in first["example"]          # пример с подставленными данными

    r = c.post("/api/style/teach", json={"message": "Не пиши «подтверждаю», пиши «спасибо, получил»."}).json()
    assert r["reply"] == "Запомнил." and len(r["rules"]) == 1
    d = c.get("/api/style").json()
    ar = next(x for x in d["letters"] if x["key"] == "auto_reply")
    assert ar["source"] == "по вашим правилам" and ar["example"].startswith("Спасибо, получил.")
    assert any(x["kind"] == "style_rule_added" and x["actor"] == "human" for x in a.j.actions())

    assert c.delete(f"/api/style/rules/{d['rules'][0]['id']}").status_code == 200
    assert c.get("/api/style").json()["rules"] == []
    assert c.delete("/api/style/rules/999999").status_code == 404
    assert c.post("/api/style/teach", json={"message": "  "}).status_code == 422


def test_next_auto_reply_uses_learned_style(env):
    c, a, _ = env
    a.llm = StubLLM(style_answer)
    c.post("/api/style/teach", json={"message": "короче"})
    sent = [x["payload"] for x in a.j.actions() if x["kind"] == "mail_sent" and x["payload"].get("template") == "auto_reply"]
    before = len(sent)
    # новое письмо той же категории, что уже получало автоответ
    old = next(x for x in a.j.actions() if x["kind"] == "mail_sent" and x["payload"].get("template") == "auto_reply")
    ev = a.j.event(old["event_id"])["payload"]
    a.mail = type("M", (), {"fetch_new": lambda self: [type("E", (), dict(
        id="new-style", from_name=ev["from_name"], from_email=ev["from"], subject=ev["subject"] + " (2)",
        body=ev["body"], received=None))()]})()
    BY_SUBJECT[ev["subject"] + " (2)"] = BY_SUBJECT[ev["subject"]]
    a.process_inbox()
    sent = [x["payload"] for x in a.j.actions() if x["kind"] == "mail_sent" and x["payload"].get("template") == "auto_reply"]
    assert len(sent) == before + 1 and sent[-1]["body"].startswith("Спасибо, получил.")
