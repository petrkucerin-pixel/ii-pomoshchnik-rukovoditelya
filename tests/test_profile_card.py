"""Профиль: карточка руководителя с правкой, «Штат сотрудников», «Мои аватары» (Пит 28.11)."""
import pytest

from test_cabinet_api import env  # noqa: F401 — env — фикстура стенда на учебных данных

import pomoshnik.server as srv

DAVID = "dff45d08-bc9d-4527-bb54-1781aef8553b"
MINE = "52dfeb28-be9d-4bea-be17-b4c6309808b4"


class FakeLA:
    def __init__(self, own=None):
        self.own = own if own is not None else [
            {"id": MINE, "name": "petrkucerin (Friendly)", "type": "IMAGE", "status": "ACTIVE", "preview_url": "https://x/p.png"}]

    def avatars(self):
        return self.own

    def avatar(self, aid):
        if aid == DAVID:
            return {"id": DAVID, "name": "David (Friendly)", "type": "VIDEO", "status": "ACTIVE", "preview_url": "https://x/d.png"}
        hit = next((a for a in self.own if a["id"] == aid), None)
        if not hit:
            raise RuntimeError("нет такого")
        return hit


# ---------------- карточка ----------------
def test_card_defaults_and_edit(env):
    c, a, _ = env
    d = c.get("/api/profile/card").json()
    assert d["name"] == "Иван Петров" and d["title"] and d["about"] and d["email"].endswith("@company.example")
    assert d["phone"] == "" and d["goals"]
    assert "SMB" in d["about"] and "Облако Плюс" in d["about"] and d["title"] not in d["about"]   # регистр и без повтора
    r = c.put("/api/profile/card", json={**{k: d[k] for k in ("name", "title", "about", "email", "phone", "address")},
                                         "phone": "+7 (912) 000-00-00", "address": "Екатеринбург, ул. Малышева, 1",
                                         "about": "Руковожу продажами SMB и digital."})
    assert r.status_code == 200 and r.json()["phone"] == "+7 (912) 000-00-00"
    again = c.get("/api/profile/card").json()
    assert again["address"].startswith("Екатеринбург") and again["about"] == "Руковожу продажами SMB и digital."
    assert any(x["kind"] == "profile_card" and x["actor"] == "human" for x in a.j.actions())


def test_card_validation_and_name_is_used_by_assistant(env):
    c, a, _ = env
    base = c.get("/api/profile/card").json()
    keep = {k: base[k] for k in ("name", "title", "about", "email", "phone", "address")}
    assert c.put("/api/profile/card", json={**keep, "email": "не почта"}).status_code == 422
    assert c.put("/api/profile/card", json={**keep, "phone": "звоните вечером"}).status_code == 422
    assert c.put("/api/profile/card", json={**keep, "name": " "}).status_code == 422
    assert c.put("/api/profile/card", json={**keep, "name": "Иван Сергеевич Петров"}).status_code == 200
    assert a.p.name == "Иван Сергеевич Петров"                        # подпись и аватар берут новое имя


# ---------------- штат ----------------
def test_staff_groups_with_work_numbers(env):
    c, a, _ = env
    d = c.get("/api/profile/staff").json()
    titles = [g["title"] for g in d["groups"]]
    assert titles == ["Руководство", "Моя команда", "Коллеги"]
    team = d["groups"][1]["people"]
    assert len(team) == 7 and d["total"] == 11 and len(d["groups"][2]["people"]) == 3
    olga = next(p for p in team if p["name"] == "Ольга Смирнова")
    assert olga["position"] == "Директор по продажам SMB" and olga["email"] == "p02@company.example"
    assert olga["tasks_open"] >= 1 and olga["letters"] >= 1
    assert d["groups"][0]["people"][0]["name"] == "Виктор Орлов"


# ---------------- аватары ----------------
def test_avatars_list_choose_and_robot_uses_choice(env, monkeypatch):
    c, a, _ = env
    monkeypatch.setattr(srv, "_avatar_client", lambda: FakeLA())
    monkeypatch.setenv("LIVEAVATAR_AVATAR_ID", DAVID)
    d = c.get("/api/profile/avatars").json()
    ids = [x["id"] for x in d["avatars"]]
    assert ids == [MINE, DAVID]                                         # сначала ваши, потом готовый
    assert next(x for x in d["avatars"] if x["id"] == DAVID)["current"] is True
    assert next(x for x in d["avatars"] if x["id"] == MINE)["source"] == "Обучен на вас"
    assert c.put("/api/profile/avatar", json={"avatar_id": MINE}).status_code == 200
    assert srv.stand.robot.avatar_id == MINE and srv.stand.autopilot.avatar_id == MINE
    assert srv.current_avatar_id() == MINE
    assert next(x for x in c.get("/api/profile/avatars").json()["avatars"] if x["id"] == MINE)["current"] is True
    assert any(x["kind"] == "avatar_choice" for x in a.j.actions())


def test_new_trained_avatar_appears_and_not_ready_cannot_be_chosen(env, monkeypatch):
    c, a, _ = env
    new = {"id": "new-1", "name": "Второй", "type": "IMAGE", "status": "DEPLOYING", "preview_url": ""}
    monkeypatch.setattr(srv, "_avatar_client", lambda: FakeLA(own=[new]))
    d = c.get("/api/profile/avatars").json()
    row = next(x for x in d["avatars"] if x["id"] == "new-1")
    assert row["ready"] is False and "обучается" in row["status_text"]
    assert c.put("/api/profile/avatar", json={"avatar_id": "new-1"}).status_code == 409
    assert c.put("/api/profile/avatar", json={"avatar_id": "чужой"}).status_code == 404
