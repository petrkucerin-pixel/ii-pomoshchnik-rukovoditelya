import pytest
from fastapi.testclient import TestClient

import pomoshnik.server as srv


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "RUNTIME", tmp_path)
    monkeypatch.setattr(srv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with TestClient(srv.app) as c:
        yield c
    srv.stand.journal.close()
    srv.stand.protocols.close()


def test_health_without_bot_token(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] and r.json()["bot"] == "нет токена"


def test_index_and_metrics(client, tmp_path):
    srv.stand.journal.log_action("mail_авто", path="now", payload={"minutes_saved": 3})
    srv.stand.journal.log_action("mail_спросить_руководителя", path="decision", payload={"minutes_saved": 2})
    m = client.get("/api/metrics").json()
    assert m["letters"] == 2 and m["share_without_leader"] == 0.5 and m["hours_saved"] == pytest.approx(5 / 60, abs=0.01)
    page = client.get("/").text
    assert "ИИ-помощник руководителя" in page and "Иван Петров" in page and "без руководителя" in page
    assert (tmp_path / "ivan_petrov.sqlite").exists()


def test_meeting_before_bot_bound(client):
    r = client.post("/meeting", data={"title": "x", "transcript": "y"})
    assert r.status_code == 409
