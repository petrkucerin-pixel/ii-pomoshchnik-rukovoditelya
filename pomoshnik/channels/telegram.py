"""Синхронный транспорт Telegram для одного чата руководителя."""

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from html.parser import HTMLParser
from threading import Lock
from typing import Any

import httpx


_TEXT_LIMIT = 4096
_telegram_request = ContextVar("telegram_request", default=False)


def _outside_telegram_request(record: logging.LogRecord) -> bool:
    return not _telegram_request.get()


# HTTPX пишет полный URL на INFO, транспорт и кодек HTTP/2 — детали на DEBUG.
# Фильтр действует только в контексте текущего запроса бота.
for _logger in ("httpx", "httpcore.http11", "httpcore.http2", "hpack.hpack", "hpack.table"):
    logging.getLogger(_logger).addFilter(_outside_telegram_request)


@dataclass(frozen=True)
class Button:
    label: str
    data: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.data.encode("utf-8")) <= 64:
            raise ValueError("Данные кнопки должны занимать от 1 до 64 байт UTF-8")


@dataclass(frozen=True)
class Update:
    update_id: int
    kind: str
    chat_id: int | None
    text: str
    message_id: int | None
    callback_id: str | None = None
    file_id: str | None = None


class TelegramError(Exception):
    def __init__(self, description: str, error_code: int | None) -> None:
        super().__init__(description)
        self.description = description
        self.error_code = error_code


class TelegramRateLimit(TelegramError):
    def __init__(self, retry_after: int, description: str = "Too Many Requests") -> None:
        super().__init__(description, 429)
        self.retry_after = retry_after


class _HtmlChunks(HTMLParser):
    """Лимит Telegram считается после разбора HTML. Восстанавливаем теги на границе."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.chunks: list[str] = []
        self.parts: list[str] = []
        self.open_tags: list[tuple[str, str]] = []
        self.size = 0

    def _next_chunk(self) -> None:
        self.parts.extend(f"</{name}>" for name, _ in reversed(self.open_tags))
        self.chunks.append("".join(self.parts))
        self.parts = [opening for _, opening in self.open_tags]
        self.size = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if self.size == _TEXT_LIMIT:
            self._next_chunk()
        opening = self.get_starttag_text()
        self.open_tags.append((tag, opening))
        self.parts.append(opening)

    def handle_endtag(self, tag: str) -> None:
        if self.open_tags and self.open_tags[-1][0] == tag:
            self.open_tags.pop()
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        start = 0
        while start < len(data):
            if self.size == _TEXT_LIMIT:
                self._next_chunk()
            count = min(_TEXT_LIMIT - self.size, len(data) - start)
            self.parts.append(data[start:start + count])
            self.size += count
            start += count

    def handle_entityref(self, name: str) -> None:
        if self.size == _TEXT_LIMIT:
            self._next_chunk()
        self.parts.append(f"&{name};")
        self.size += 1

    def handle_charref(self, name: str) -> None:
        self.handle_entityref(f"#{name}")

    @classmethod
    def split(cls, text: str) -> list[str]:
        if len(text) <= _TEXT_LIMIT:
            return [text]
        parser = cls()
        parser.feed(text)
        parser.close()
        if parser.size or not parser.chunks:
            parser.chunks.append("".join(parser.parts))
        return parser.chunks


def _keyboard(buttons: list[list[Button]]) -> dict:
    return {"inline_keyboard": [
        [{"text": button.label, "callback_data": button.data} for button in row]
        for row in buttons
    ]}


class TelegramPult:
    def __init__(
        self, token: str, chat_id: int | str, *, client: httpx.Client | None = None,
        api_base: str = "https://api.telegram.org",
    ) -> None:
        if not token:
            raise ValueError("Нужен токен Telegram-бота")
        self._token = token
        self._chat_id = chat_id
        self._base = api_base.rstrip("/")
        self._client = client if client is not None else httpx.Client(timeout=35)
        self._owns_client = client is None
        self._offset = 0
        self._poll_lock = Lock()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _call(self, method: str, body: dict, *, timeout: httpx.Timeout | None = None) -> Any:
        url = f"{self._base}/bot{self._token}/{method}"
        context = _telegram_request.set(True)
        try:
            response = self._client.post(
                url, json=body, follow_redirects=False, timeout=timeout or self._client.timeout
            )
        except (httpx.HTTPError, httpx.InvalidURL, OSError) as exc:
            raise TelegramError(f"Не удалось выполнить {method} ({type(exc).__name__})", None) from None
        finally:
            _telegram_request.reset(context)

        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            payload = {}
        if response.is_success and payload.get("ok") is True and "result" in payload:
            return payload["result"]

        code = payload.get("error_code", response.status_code)
        description = str(payload.get("description", f"Некорректный ответ Telegram на {method}"))
        description = description.replace(self._token, "[скрыто]")
        if code == 429 or response.status_code == 429:
            retry_after = payload.get("parameters", {}).get("retry_after", 0)
            raise TelegramRateLimit(retry_after, description)
        raise TelegramError(description, code)

    def _send(self, text: str, buttons: list[list[Button]] | None = None) -> int:
        body: dict = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}
        if buttons is not None:
            body["reply_markup"] = _keyboard(buttons)
        return self._call("sendMessage", body)["message_id"]

    def send_text(self, text: str) -> int:
        for part in _HtmlChunks.split(text):
            message_id = self._send(part)
        return message_id

    def send_buttons(self, text: str, buttons: list[list[Button]]) -> int:
        return self._send(text, buttons)

    def edit_text(self, message_id: int, text: str, buttons: list[list[Button]] | None = None) -> None:
        body: dict = {
            "chat_id": self._chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
        }
        if buttons is not None:
            body["reply_markup"] = _keyboard(buttons)
        self._call("editMessageText", body)

    def download(self, file_id: str, *, limit: int = 20_000_000) -> bytes:
        """Файл от руководителя (голосовое) по file_id. Ошибки — без токена в тексте."""
        info = self._call("getFile", {"file_id": file_id})
        path, size = info.get("file_path"), int(info.get("file_size") or 0)
        if not path or size > limit:
            raise TelegramError("файл недоступен или слишком большой", None)
        try:
            r = self._client.get(f"{self._base}/file/bot{self._token}/{path}", follow_redirects=False, timeout=60)
        except (httpx.HTTPError, OSError) as exc:
            raise TelegramError(f"Не удалось скачать файл ({type(exc).__name__})", None) from None
        if not r.is_success:
            raise TelegramError(f"Не удалось скачать файл ({r.status_code})", r.status_code)
        return r.content

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def _update(self, raw: dict) -> Update:
        uid = raw["update_id"]
        callback = raw.get("callback_query")
        message = (callback if callback is not None else raw).get("message", {})
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        mid = message.get("message_id")
        other = Update(uid, "other", chat_id, "", mid)
        if chat_id is None:
            return other
        target = str(self._chat_id)
        if target != str(chat_id):
            username = chat.get("username")
            if not username or target.casefold() != f"@{username}".casefold():
                return other
        if callback is not None:
            if "data" in callback:
                return Update(uid, "callback", chat_id, callback["data"], mid, callback["id"])
        elif "text" in message:
            return Update(uid, "text", chat_id, message["text"], mid)
        elif "voice" in message:
            return Update(uid, "voice", chat_id, "", mid, file_id=message["voice"]["file_id"])
        return other

    def poll(self, timeout: int = 25) -> list[Update]:
        request_timeout = httpx.Timeout(self._client.timeout)
        if request_timeout.read is not None:
            request_timeout.read = max(request_timeout.read, timeout + 10)
        with self._poll_lock:
            batch = self._call(
                "getUpdates",
                {"offset": self._offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
                timeout=request_timeout,
            )
            updates = {}
            for raw in batch:
                uid = raw["update_id"]
                if uid >= self._offset and uid not in updates:
                    updates[uid] = self._update(raw)
            if updates:
                self._offset = max(updates) + 1
            return list(updates.values())
