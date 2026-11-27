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


def test_prompt_knows_it_cannot_see_who_speaks():
    p = build_context_prompt(leader="Ивана Петрова", title="Планёрка", questions=[], open_tasks=[], style="",
                             participants=["Ольга Смирнова", "Сергей Морозов"])
    assert "не видишь, кто говорит" in p and "назовёт себя" in p
    assert "Ольга Смирнова, Сергей Морозов" in p
    assert "назови" in opening_text("Ивана Петрова").lower() or "представ" in opening_text("Ивана Петрова").lower()


def test_transcript_fragments_are_merged_into_utterances():
    t = transcript_to_text([
        {"role": "avatar", "transcript": "Начнём?", "relative_timestamp": 7},
        {"role": "user", "transcript": "Да,", "relative_timestamp": 11},
        {"role": "user", "transcript": "договорённости и", "relative_timestamp": 15},
        {"role": "user", "transcript": "плюс по облачному", "relative_timestamp": 19},
        {"role": "avatar", "transcript": "Первый вопрос.", "relative_timestamp": 27},
        {"role": "user", "transcript": "Нет.", "relative_timestamp": 60},
    ], merge=True)
    assert t.splitlines() == ["00:00:07", "ИИ-помощник: Начнём?", "00:00:11",
                              "Участник: Да, договорённости и плюс по облачному", "00:00:27", "ИИ-помощник: Первый вопрос.",
                              "00:01:00", "Участник: Нет."]


def test_single_participant_means_no_who_answered_questions():
    one = build_context_prompt(leader="Ивана Петрова", title="Планёрка", questions=[], open_tasks=[], style="",
                               participants=["Пит (это Иван Петров)"])
    assert "один человек" in one and "не переспрашивай" in one
    # живая проверка 27.11: на старте была одна Ольга, потом вошёл Алексей — аватар благодарил «Ольгу» за его слова
    assert "присоединиться" in one and "незнакомое имя" in one
    many = build_context_prompt(leader="Ивана Петрова", title="Планёрка", questions=[], open_tasks=[], style="",
                                participants=["Пит", "Оля С."])
    assert "один человек" not in many and "не больше одного раза подряд" in many


def test_avatar_moderates_gives_floor_by_name_and_asks_to_speak_in_turn():
    """Пит 27.11: говорят сразу 3–4 человека — аватар просит говорить по очереди и сам назначает, кто первый."""
    p = build_context_prompt(leader="Ивана Петрова", title="Планёрка", questions=["Ольга Смирнова — прогноз декабря"],
                             open_tasks=[], style="", participants=["Ольга Смирнова", "Алексей Новиков"])
    assert "вам слово" in p and "по очереди" in p and "первым спрашивай" in p.lower()
    assert "по очереди" in opening_text("Ивана Петрова")
