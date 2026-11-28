"""Профиль → «Правила» (Пит 28.11: «Все должно быть логично и оформлено. Начнём с правил. Это большая кнопка,
проваливаемся и читаем внутри»). Правила читаются по-человечески: что решаете только вы, что помощник делает сам,
кому передаёт, сколько писем прошло по правилу."""
from test_cabinet_api import env  # noqa: F401 — env — фикстура стенда на учебных данных


def test_rules_page_reads_like_a_policy(env):
    c, a, _ = env
    d = c.get("/api/profile/rules").json()

    only = {x["key"]: x for x in d["only_me"]}
    assert set(only) == {"деньги", "кадры", "обязательства_клиентам", "руководство"}
    assert only["деньги"]["title"] == "Деньги и бюджеты" and "скидки" in only["деньги"]["examples"]
    assert only["руководство"]["title"].startswith("Письма руководству")
    assert sum(x["letters"] for x in d["only_me"]) > 0                     # сколько писем пришло вам на решение

    groups = {g["action"]: g for g in d["groups"]}
    assert list(groups)[:2] == ["делегировать", "авто"]                    # сначала что помощник делает, потом — что пропускает
    sales = next(r for r in groups["делегировать"]["rules"] if r["category"] == "продажи")
    assert sales["to"] == {"name": "Ольга Смирнова", "position": "Директор по продажам SMB"}   # человек, а не «p02»
    assert sales["profile_line"].startswith("передача запроса") and sales["letters"] >= 1
    assert groups["игнорировать"]["title"] and all(r["title"][0].isupper() for g in d["groups"] for r in g["rules"])

    s = d["summary"]
    assert s["only_me"] == 4 and s["auto"] == sum(len(g["rules"]) for g in d["groups"])
    assert s["letters_without_you"] + s["letters_to_you"] == s["letters"] > 0
    assert d["fallback"]["letters"] >= 0 and "спрашиваю вас" in d["fallback"]["text"]


# ---------------- Профиль → «Метрики» (Пит 28.11: «плашка, провалимся — круговые диаграммы, меняются по мере новых цифр») ----------------
def test_metrics_have_parts_for_donuts_and_grow_with_new_mail(env):
    c, a, _ = env
    m = c.get("/api/metrics").json()
    parts = m["letters_by_action"]
    assert [p["key"] for p in parts] == ["делегировать", "спросить_руководителя", "авто", "в_сводку", "игнорировать",
                                         "уточнить"]
    assert sum(p["value"] for p in parts) == m["letters"]
    assert next(p for p in parts if p["key"] == "спросить_руководителя")["value"] == m["letters"] - m["letters_without_leader"]
    hours = m["hours_by_activity"]
    assert abs(sum(p["value"] for p in hours) - m["hours_saved"]) < 0.05 and hours[0]["label"] == "Почта"
    assert m["hours_goal"] == 8

    from types import SimpleNamespace
    from test_cabinet_api import BY_SUBJECT, ROWS
    row = next(r for r in ROWS if r["category"] == "рассылка")
    a.mail = type("M", (), {"fetch_new": lambda self: [SimpleNamespace(
        id="new-m", from_name=row["from_name"], from_email=row["from_email"], subject=row["subject"] + " (3)",
        body=row["body"], received=a.clock.now())]})()
    BY_SUBJECT[row["subject"] + " (3)"] = "рассылка"
    a.process_inbox()
    again = c.get("/api/metrics").json()
    assert again["letters"] == m["letters"] + 1                          # цифры живые: новое письмо — новая доля
    assert next(p for p in again["letters_by_action"] if p["key"] == "игнорировать")["value"] == \
        next(p for p in parts if p["key"] == "игнорировать")["value"] + 1



def test_read_marks_do_not_change_routing_numbers(env):
    """Почта ставит «прочитано» (mail_read) — это не решение по письму: доли и правила не меняются."""
    c, a, _ = env
    before = c.get("/api/metrics").json()
    rules_before = c.get("/api/profile/rules").json()["summary"]
    eid = next(x["event_id"] for x in a.j.actions() if x["kind"] == "mail_спросить_руководителя")
    a.j.log_action("mail_read", actor="human", event_id=eid, payload={})
    a.j.log_action("mail_уточнить", event_id=None, summary="Непонятное письмо — уточнить", payload={"category": "клиенты"})
    after = c.get("/api/metrics").json()
    assert after["letters_without_leader"] == before["letters_without_leader"] + 1          # только «уточнил»
    assert after["letters"] == before["letters"] + 1
    assert c.get("/api/profile/rules").json()["summary"]["letters_to_you"] == rules_before["letters_to_you"]
    texts = [x["text"] for day in c.get("/api/profile/journal").json()["days"] for x in day["items"]]
    assert any(t.startswith("Уточнил у отправителя") for t in texts) and not any("mail_read" in t for t in texts)



def test_tile_numbers_match_inside_and_metrics(env):
    """Пит 28.11: «Только вы, Сам. Если уж заявляем, то внутри должно соответствовать и не наводить на доп вопросы»."""
    c, a, _ = env
    a.recheck_queue()                                   # повторный разбор не должен удваивать письма
    s = c.get("/api/profile/rules").json()
    m = c.get("/api/metrics").json()
    assert s["summary"]["letters"] == m["letters"]                                   # одна цифра на обеих плитках
    assert s["summary"]["letters_without_you"] == m["letters_without_leader"]
    assert s["summary"]["groups_letters"] == s["summary"]["letters_without_you"]     # сумма по правилам = «Без вас»
    assert sum(x["letters"] for x in s["only_me"]) + s["fallback"]["letters"] == s["summary"]["letters_to_you"]
    assert s["summary"]["auto"] == sum(len(g["rules"]) for g in s["groups"])
