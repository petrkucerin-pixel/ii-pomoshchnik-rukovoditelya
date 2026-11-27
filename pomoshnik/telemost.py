"""Ссылка на встречу в Телемосте.

Сейчас — вручную: кабинет открывает Телемост в новой вкладке, руководитель нажимает «Создать видеовстречу»
и вставляет ссылку. Автоматически — через официальный API Телемоста, он доступен только аккаунтам
Яндекс 360 для бизнеса на домене организации. Заказчику достаточно вписать OAuth-токен в `.env`:

    TELEMOST_OAUTH_TOKEN=<токен с правом telemost-api:conferences.create>

Документация: https://yandex.ru/dev/telemost/doc/ru/ (раздел «Доступ к API», «Создание встречи»).
На хакатоне автоматический режим не проверен живьём: у Пита личный аккаунт Яндекса.
"""
from __future__ import annotations

import httpx

API = "https://cloud-api.yandex.net/v1/telemost-api/conferences"
CREATE_URL = "https://telemost.yandex.ru/"        # ручной режим: здесь руководитель создаёт встречу сам


class TelemostError(RuntimeError):
    pass


def create_conference(token: str, *, client: httpx.Client | None = None) -> str:
    """Новая встреча в Телемосте → ссылка для участников (join_url)."""
    c = client or httpx.Client(timeout=20)
    try:
        r = c.post(API, headers={"Authorization": f"OAuth {token}"}, json={"waiting_room_level": "PUBLIC"})
    except httpx.HTTPError as e:
        raise TelemostError(f"Телемост недоступен ({type(e).__name__})") from None
    if r.status_code >= 400:
        raise TelemostError(f"Телемост отказал: {r.status_code}. Проверьте токен и что аккаунт — Яндекс 360 для бизнеса")
    link = (r.json() or {}).get("join_url")
    if not link:
        raise TelemostError("Телемост не вернул ссылку на встречу")
    return link
