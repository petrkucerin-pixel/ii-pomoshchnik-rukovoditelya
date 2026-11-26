"""Сменный слой модели. Любой OpenAI-совместимый адрес: DeepSeek, GigaChat-шлюз, YandexGPT, локальная модель.

Смена поставщика — правка .env: LLM_BASE_URL, LLM_MODEL, LLM_API_KEY (или DEEPSEEK_API_KEY).
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol

# $ за 1 млн токенов, пиковые цены DeepSeek (api-docs.deepseek.com, 26.11.2026). Берём пик, чтобы не занизить.
PRICES = {
    "deepseek-flash": (0.30, 1.20),
    "deepseek-v4-pro": (1.32, 3.96),
}


@dataclass
class Usage:
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_ms: int


class LLM(Protocol):
    def complete_json(self, system: str, user: str, *, model: str | None = None) -> dict: ...


def cost_usd(model: str, tin: int, tout: int) -> float:
    pin, pout = PRICES.get(model, (0.0, 0.0))
    return (tin * pin + tout * pout) / 1_000_000


def parse_json(text: str) -> dict:
    """JSON из ответа модели: допускаем обёртку ```json ... ``` и текст вокруг."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        a, b = text.find("{"), text.rfind("}")
        if a != -1 and b > a:
            text = text[a:b + 1]
    return json.loads(text)


class OpenAICompatible:
    def __init__(self, base_url: str, api_key: str, model: str, *, on_usage: Callable[[Usage], None] | None = None,
                 client=None, timeout: float = 60):
        self.model = model
        self.on_usage = on_usage
        if client is None:
            from openai import OpenAI
            client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=1)
        self.client = client

    def complete_json(self, system: str, user: str, *, model: str | None = None) -> dict:
        model = model or self.model
        t0 = time.monotonic()
        r = self.client.chat.completions.create(
            model=model, temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        ms = int((time.monotonic() - t0) * 1000)
        u = r.usage
        tin, tout = (u.prompt_tokens, u.completion_tokens) if u else (0, 0)
        if self.on_usage:
            self.on_usage(Usage(model, tin, tout, cost_usd(model, tin, tout), ms))
        return parse_json(r.choices[0].message.content or "{}")


class StubLLM:
    """Заглушка для тестов: ответ по первому совпавшему ключу в тексте запроса или функцией."""

    def __init__(self, answers: dict[str, dict] | Callable[[str, str], dict] | None = None, default: dict | None = None):
        self.answers = answers or {}
        self.default = default or {}
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system: str, user: str, *, model: str | None = None) -> dict:
        self.calls.append((system, user))
        if callable(self.answers):
            return self.answers(system, user)
        for key, ans in self.answers.items():
            if key in user:
                return ans
        return self.default


def from_env(on_usage: Callable[[Usage], None] | None = None) -> OpenAICompatible:
    base = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
    key = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "deepseek-flash")
    if not key:
        raise RuntimeError("ключ модели не задан: LLM_API_KEY или DEEPSEEK_API_KEY в .env")
    return OpenAICompatible(base, key, model, on_usage=on_usage)


def load_dotenv(path: str = ".env") -> None:
    """Минимальное чтение .env без сторонних библиотек. Уже заданные переменные не перетирает."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
