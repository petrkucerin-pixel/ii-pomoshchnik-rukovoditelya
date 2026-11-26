"""Контракт Telegram-пульта. Пишет Claude, исполнитель (Codex) этот файл не меняет."""
import json

import httpx
import pytest

from pomoshnik.channels.telegram import Button, TelegramError, TelegramPult, TelegramRateLimit

TOKEN = "123:SECRET_TOKEN_VALUE"
CHAT = 555


class FakeTG:
    def __init__(self):
        self.calls = []
        self.updates = []
        self.next_id = 100
        self.fail = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}") if request.content else dict(request.url.params)
        self.calls.append((method, body))
        if self.fail:
            return httpx.Response(200 if self.fail[0] != 429 else 429, json={
                "ok": False, "error_code": self.fail[0], "description": self.fail[1],
                **({"parameters": {"retry_after": 7}} if self.fail[0] == 429 else {})})
        if method in ("sendMessage", "editMessageText"):
            self.next_id += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})
        if method == "answerCallbackQuery":
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "getUpdates":
            off = int(body.get("offset", 0))
            res = [u for u in self.updates if u["update_id"] >= off]
            return httpx.Response(200, json={"ok": True, "result": res})
        return httpx.Response(404, json={"ok": False, "error_code": 404, "description": "Not Found"})


@pytest.fixture
def tg():
    fake = FakeTG()
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return fake, TelegramPult(TOKEN, CHAT, client=client)


def last(fake, method):
    return [b for m, b in fake.calls if m == method][-1]


def test_send_text(tg):
    fake, pult = tg
    mid = pult.send_text("План дня")
    body = last(fake, "sendMessage")
    assert mid == 101
    assert str(body["chat_id"]) == str(CHAT)
    assert body["text"] == "План дня"
    assert body.get("parse_mode") == "HTML"


def test_send_buttons_inline_keyboard(tg):
    fake, pult = tg
    pult.send_buttons("Утвердить протокол?", [[Button("Утвердить", "p1:ok"), Button("Править", "p1:edit")]])
    kb = last(fake, "sendMessage")["reply_markup"]
    if isinstance(kb, str):
        kb = json.loads(kb)
    row = kb["inline_keyboard"][0]
    assert [b["text"] for b in row] == ["Утвердить", "Править"]
    assert [b["callback_data"] for b in row] == ["p1:ok", "p1:edit"]


def test_button_data_limit_64_bytes():
    Button("ok", "я" * 32)  # 64 байта — можно
    with pytest.raises(ValueError):
        Button("too long", "я" * 33)


def test_long_text_split(tg):
    fake, pult = tg
    pult.send_text("а" * 9000)
    sent = [b for m, b in fake.calls if m == "sendMessage"]
    assert len(sent) == 3
    assert all(len(b["text"]) <= 4096 for b in sent)
    assert "".join(b["text"] for b in sent) == "а" * 9000


def test_edit_and_answer_callback(tg):
    fake, pult = tg
    pult.edit_text(42, "Утверждено", buttons=None)
    assert last(fake, "editMessageText")["message_id"] == 42
    pult.answer_callback("cb-1", "Готово")
    assert last(fake, "answerCallbackQuery")["callback_query_id"] == "cb-1"


def test_poll_text_callback_and_offset(tg):
    fake, pult = tg
    fake.updates = [
        {"update_id": 10, "message": {"message_id": 1, "chat": {"id": CHAT}, "text": "план"}},
        {"update_id": 11, "callback_query": {"id": "cb7", "data": "p1:ok",
                                             "message": {"message_id": 2, "chat": {"id": CHAT}}}},
        {"update_id": 12, "message": {"message_id": 3, "chat": {"id": CHAT}, "voice": {"file_id": "F1"}}},
        {"update_id": 13, "message": {"message_id": 4, "chat": {"id": 999}, "text": "чужой"}},
    ]
    ups = pult.poll(timeout=0)
    assert [u.kind for u in ups] == ["text", "callback", "voice", "other"]
    assert ups[0].text == "план" and ups[0].chat_id == CHAT
    assert ups[1].text == "p1:ok" and ups[1].callback_id == "cb7" and ups[1].message_id == 2
    assert ups[2].file_id == "F1"
    assert pult.poll(timeout=0) == []
    assert int(last(fake, "getUpdates")["offset"]) == 14


def test_error_and_rate_limit_hide_token(tg):
    fake, pult = tg
    fake.fail = (400, "Bad Request: chat not found")
    with pytest.raises(TelegramError) as e:
        pult.send_text("x")
    assert "SECRET_TOKEN_VALUE" not in str(e.value)
    fake.fail = (429, "Too Many Requests: retry after 7")
    with pytest.raises(TelegramRateLimit) as e2:
        pult.send_text("x")
    assert e2.value.retry_after == 7
    assert isinstance(e2.value, TelegramError)
