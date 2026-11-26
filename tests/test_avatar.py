import json

import httpx
import pytest

from pomoshnik.avatar import (LiveAvatar, LiveAvatarError, Session, build_context_prompt, latest_finished,
                              opening_text, transcript_to_text)

KEY = "LA_SECRET_KEY"


def fake(handler):
    return LiveAvatar(KEY, client=httpx.Client(base_url="https://api.liveavatar.com",
                                               transport=httpx.MockTransport(handler)))


def test_context_prompt_has_identity_rules_and_questions():
    p = build_context_prompt(leader="Ивана Петрова", title="Планёрка", questions=["Сергей, договор — что готово?"],
                             open_tasks=["КП для «ТехноСтрой» — Ольга, 24.12"], style="коротко")
    assert "ИИ-помощник" in p and "Сергей, договор" in p and "деньги" in p and "ТехноСтрой" in p
    assert "ИИ-помощник Ивана Петрова" in opening_text("Ивана Петрова")


def test_transcript_ordered_and_labelled():
    t = transcript_to_text([
        {"role": "user", "transcript": "Договор подпишем к пятнице", "relative_timestamp": 65},
        {"role": "avatar", "transcript": "Здравствуйте!", "relative_timestamp": 2},
        {"role": "user", "transcript": "  ", "relative_timestamp": 70},
    ])
    assert t.splitlines() == ["00:00:02", "ИИ-помощник: Здравствуйте!", "00:01:05", "Участник: Договор подпишем к пятнице"]


def test_api_key_header_and_context_creation():
    seen = {}

    def h(req):
        seen["key"] = req.headers.get("X-API-KEY")
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"code": 1000, "data": {"id": "ctx-1"}})

    assert fake(h).create_context("Планёрка" * 20, "промпт", "привет") == "ctx-1"
    assert seen["key"] == KEY and len(seen["body"]["name"]) <= 64


def test_transcript_paging():
    pages = {None: {"transcript_data": [{"role": "user", "transcript": "a", "relative_timestamp": 1}], "next_timestamp": 5},
             "5": {"transcript_data": [{"role": "avatar", "transcript": "b", "relative_timestamp": 6}], "next_timestamp": None}}

    def h(req):
        return httpx.Response(200, json={"code": 1000, "data": pages[req.url.params.get("start_timestamp")]})

    assert [e["transcript"] for e in fake(h).transcript("s1")] == ["a", "b"]


def test_errors_do_not_leak_key():
    def h(req):
        return httpx.Response(401, json={"code": 4010, "message": "Invalid credentials"})

    with pytest.raises(LiveAvatarError) as e:
        fake(h).credits()
    assert KEY not in str(e.value)

    def boom(req):
        raise httpx.ConnectError("no route", request=req)

    with pytest.raises(LiveAvatarError):
        fake(boom).credits()


def test_latest_finished_session():
    s = [Session("a", "", "2026-11-27T10:00:00", 60, "EMBED"), Session("b", "", None, 0, "EMBED"),
         Session("c", "", "2026-11-27T11:00:00", 90, "EMBED")]
    assert latest_finished(s).id == "c"
    assert latest_finished([Session("x", "", None, 0, "EMBED")]) is None


def test_context_prompt_carries_history_and_history_questions():
    p = build_context_prompt(leader="Ивана Петрова", title="Планёрка", questions=[], open_tasks=[], style="",
                             history="Облако Плюс\n- 08.12–11.12 Сергей Морозов — ждём до 12.12 (m022)",
                             history_questions=["Сергею Морозову: статус по «Облако Плюс» не меняется с 08.12"])
    assert "ждём до 12.12 (m022)" in p and "не меняется с 08.12" in p
    assert "не придумывай" in p.lower() and "истори" in p.lower()


def test_context_prompt_without_history_says_so():
    p = build_context_prompt(leader="Ивана Петрова", title="Планёрка", questions=[], open_tasks=[], style="")
    assert "истории пока нет" in p.lower()
