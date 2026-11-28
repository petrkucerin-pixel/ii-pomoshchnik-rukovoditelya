"""Общее для тестов: фоновая проверка почты стенда в тестах не запускается — почту тест разбирает сам."""
import pytest


@pytest.fixture(autouse=True)
def _no_mail_loop(monkeypatch):
    monkeypatch.setenv("MAIL_POLL_SECONDS", "0")
