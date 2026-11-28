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
