"""Ссылка Телемоста: сейчас — вручную через кнопку «Открыть Телемост»; с токеном Яндекс 360 для бизнеса — автоматически."""
import json

import httpx
import pytest

from pomoshnik.telemost import CREATE_URL, TelemostError, create_conference


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_create_conference_returns_join_url_and_sends_oauth():
    seen = {}

    def h(req):
        seen["auth"], seen["body"], seen["url"] = req.headers.get("Authorization"), json.loads(req.content), str(req.url)
        return httpx.Response(201, json={"id": "c1", "join_url": "https://telemost.yandex.ru/j/555"})

    assert create_conference("TOKEN-1", client=client(h)) == "https://telemost.yandex.ru/j/555"
    assert seen["auth"] == "OAuth TOKEN-1" and seen["url"].endswith("/v1/telemost-api/conferences")
    assert seen["body"]["waiting_room_level"] == "PUBLIC"


def test_errors_do_not_leak_token():
    def h(req):
        return httpx.Response(403, json={"description": "Forbidden: TOKEN-1 is not a 360 business account"})

    with pytest.raises(TelemostError) as e:
        create_conference("TOKEN-1", client=client(h))
    assert "TOKEN-1" not in str(e.value) and "403" in str(e.value)


def test_manual_mode_without_token(monkeypatch):
    import pomoshnik.server as srv
    from fastapi.testclient import TestClient
    monkeypatch.setattr(srv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("LLM_API_KEY", "x")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEMOST_OAUTH_TOKEN", "")
    with TestClient(srv.app) as c:
        assert c.get("/api/telemost").json() == {"auto": False, "create_url": CREATE_URL}
        r = c.post("/api/telemost/conference")
        assert r.status_code == 501 and "TELEMOST_OAUTH_TOKEN" in r.json()["detail"]
        monkeypatch.setenv("TELEMOST_OAUTH_TOKEN", "T")
        monkeypatch.setattr(srv, "create_conference", lambda token: "https://telemost.yandex.ru/j/777")
        assert c.get("/api/telemost").json()["auto"] is True
        assert c.post("/api/telemost/conference").json() == {"link": "https://telemost.yandex.ru/j/777"}
    srv.stand.close()


def test_env_example_lists_telemost_token_without_values():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    assert "TELEMOST_OAUTH_TOKEN=" in text
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            assert value.strip() in ("", "anthropic", "deepseek", "yandex", "ivan_petrov", "операционная",
                                     "Ивана Петрова", "https://api.deepseek.com", "deepseek-flash", "1"), key
