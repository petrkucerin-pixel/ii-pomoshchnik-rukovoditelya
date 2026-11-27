"""Контракт экрана кабинета (TASK_08_GPT). Менять может только Claude."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import pomoshnik.server as srv

STATIC = Path(srv.__file__).resolve().parent / "static" / "cabinet"
SECTIONS = {"today": "Сегодня", "mail": "Переписка", "meetings": "Совещания", "memory": "Память", "profile": "Профиль"}
ENDPOINTS = ["/api/today", "/api/decisions/", "/api/mail", "/api/style", "/api/style/teach", "/api/style/rules/", "/api/meetings",
             "/api/meetings/transcript", "/api/protocols", "/approve", "/reject", "/edit", "/api/tasks", "/api/memory",
             "/api/edits", "/api/profile", "/api/metrics", "/api/journal", "/lead"]


def read(name):
    return (STATIC / name).read_text(encoding="utf-8")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "RUNTIME", tmp_path)
    monkeypatch.setattr(srv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with TestClient(srv.app) as c:
        yield c
    srv.stand.close()


def test_root_serves_cabinet_with_five_sections(client):
    page = client.get("/").text
    assert 'name="viewport"' in page and "cabinet.js" in page and "cabinet.css" in page
    for key, title in SECTIONS.items():
        assert f'data-section="{key}"' in page, key
        assert title in page, title


def test_static_files_served(client):
    for name in ("cabinet.js", "cabinet.css"):
        assert client.get(f"/static/cabinet/{name}").status_code == 200


def test_uses_blue_glass_theme_before_own_css():
    page = read("index.html")
    assert "/static/theme/theme.css" in page and page.index("theme.css") < page.index("cabinet.css")
    for cls in ("t-nav", "t-hero", "t-card"):
        assert cls in page + read("cabinet.js"), cls
    assert "hero.svg" in page + read("cabinet.js")


def test_theme_is_complete(client):
    for path in ("theme.css", "preview.html", "img/hero.svg", "img/logo.svg", "fonts/montserrat-cyrillic-600.woff2"):
        assert client.get(f"/static/theme/{path}").status_code == 200, path


def test_js_uses_every_endpoint():
    js = read("cabinet.js")
    missing = [e for e in ENDPOINTS if e not in js]
    assert not missing, missing


def test_mail_groups_and_style_labels():
    text = read("index.html") + read("cabinet.js")
    # Пит 27.11: почта как в Outlook — папки «Входящие / Отправленные / Рассылки / Нежелательная почта»
    for word in ("Входящие", "Отправленные", "Рассылки", "Нежелательная почта",
                 "Как я пишу", "Научить помощника", "Запомнить", "Забыть"):
        assert word in text, word
    assert '"Шаблоны"]' not in text          # TASK_14: вкладки «Шаблоны» нет — руководитель шаблоны не составляет


def test_calendar_week_and_day_views():
    text = read("index.html") + read("cabinet.js")
    assert "Неделя" in text and "День" in text and "Сегодня" in text


def test_no_external_resources():
    for name in ("index.html", "cabinet.js", "cabinet.css"):
        assert not re.search(r"https?://", read(name)), name


def test_api_text_never_goes_through_html_parsing():
    js = read("cabinet.js")
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert bad not in js, bad


def test_phone_layout_rules():
    css = read("cabinet.css")
    assert re.search(r"@media[^{]*max-width", css)
    assert re.search(r"min-height:\s*44px", css)


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_js_syntax():
    r = subprocess.run(["node", "--check", str(STATIC / "cabinet.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_meetings_calendar_alone_meeting_page_and_new_meeting_by_slot():
    js = read("cabinet.js")
    assert "meetings/m/" in js and "#meetings/protocols" in js and "#meetings/tasks" in js   # встреча — своя страница
    assert "Новая встреча" in js and "/api/people" in js and 'addEventListener("click"' in js   # клик по слоту
    assert "/api/telemost" in js and "Создать комнату в Телемосте" in js and "/api/telemost/conference" in js  # комната Телемоста
    assert "Веду сам" in js and "Ведёт мой ИИ-аватар" in js and "Подключиться к встрече" in js            # кто ведёт встречу


def test_live_protocol_during_meeting_and_robot_per_meeting():
    js = read("cabinet.js")
    assert "протокол по ходу" in js and "/api/robot?" in js and "/api/robot/stop?" in js
