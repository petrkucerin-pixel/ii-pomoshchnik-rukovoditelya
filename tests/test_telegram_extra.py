import json
import logging
import traceback

import httpx
import pytest

from pomoshnik.channels.telegram import Button, TelegramError, TelegramPult, Update


TOKEN = "42:unit-test"
API = "https://telegram.invalid/proxy"


def test_html_split_keeps_nested_tags_and_entities():
    texts = []

    def send(request):
        assert str(request.url) == f"{API}/bot{TOKEN}/sendMessage"
        body = json.loads(request.content)
        assert body["parse_mode"] == "HTML"
        texts.append(body["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(texts)}})

    opening = '<b><a href="https://example.org">'
    closing = "</a></b>"
    prefix = "я" * 4095
    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        pult = TelegramPult(TOKEN, 42, client=client, api_base=API + "/")
        assert pult.send_text(opening + prefix + "&amp;&#128077;" + closing) == 2
    assert texts == [opening + prefix + "&amp;" + closing, opening + "&#128077;" + closing]


def test_edit_keyboard_keeps_rows_and_can_clear_buttons():
    keyboards = []

    def edit(request):
        keyboards.append(json.loads(request.content)["reply_markup"]["inline_keyboard"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 8}})

    with httpx.Client(transport=httpx.MockTransport(edit)) as client:
        pult = TelegramPult(TOKEN, 42, client=client)
        pult.edit_text(8, "Решение", [[Button("Да", "ok")], [Button("Правка", "edit")]])
        pult.edit_text(8, "Готово", [])
    assert keyboards == [
        [[{"text": "Да", "callback_data": "ok"}], [{"text": "Правка", "callback_data": "edit"}]],
        [],
    ]


def test_poll_ignores_replays_and_preserves_offset_on_failure():
    offsets = []
    ours = {"chat": {"id": 42}, "message_id": 8, "text": "План"}
    foreign = {"chat": {"id": 91}, "message_id": 9}
    batch = [
        {"update_id": 12, "callback_query": {"message": foreign, "id": "cb", "data": "ok"}},
        {"update_id": 10, "message": ours},
        {"update_id": 10, "message": ours},
        {"update_id": 11, "callback_query": {"inline_message_id": "inline", "id": "cb2", "data": "ok"}},
    ]

    def poll(request):
        body = json.loads(request.content)
        assert body["timeout"] == 45
        assert request.extensions["timeout"]["read"] > body["timeout"]
        offsets.append(body["offset"])
        if len(offsets) == 2:
            raise httpx.ReadTimeout("обрыв связи", request=request)
        return httpx.Response(200, json={"ok": True, "result": batch})

    with httpx.Client(transport=httpx.MockTransport(poll)) as client:
        pult = TelegramPult(TOKEN, "42", client=client)
        assert pult.poll(45) == [
            Update(12, "other", 91, "", 9),
            Update(10, "text", 42, "План", 8),
            Update(11, "other", None, "", None),
        ]
        with pytest.raises(TelegramError):
            pult.poll(45)
        assert pult.poll(45) == []
    assert offsets == [0, 13, 13]


@pytest.mark.parametrize("failure", ["api", "transport", "non_json"])
def test_token_absent_from_logs_and_formatted_errors(caplog, failure):
    def respond(request):
        if request.url.path == "/health":
            return httpx.Response(200)
        if failure == "transport":
            raise httpx.ConnectError(str(request.url), request=request)
        if failure == "non_json":
            return httpx.Response(502, text=str(request.url))
        return httpx.Response(200, json={
            "ok": False, "error_code": 401, "description": f"Unauthorized: {request.url}",
        })

    caplog.set_level(logging.DEBUG)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        pult = TelegramPult(TOKEN, 42, client=client)
        with pytest.raises(TelegramError) as error:
            pult.send_text("План")
        pult.close()
        client.get("https://example.org/health")

    if failure == "api":
        assert error.value.error_code == 401
        assert "Unauthorized" in error.value.description
    assert TOKEN not in "".join(traceback.format_exception(error.value))
    assert TOKEN not in caplog.text
    assert "https://example.org/health" in caplog.text
