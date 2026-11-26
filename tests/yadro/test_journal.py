from datetime import date

import pytest

from yadro.journal import Journal


def test_new_event_and_duplicate(journal):
    eid, new = journal.record_event("mail", "<msg-1@x>", {"subject": "Статус акции"})
    assert new
    eid2, new2 = journal.record_event("mail", "<msg-1@x>", {"subject": "Статус акции"})
    assert eid2 == eid and not new2


def test_same_external_id_other_channel_is_new(journal):
    a, _ = journal.record_event("mail", "42", {})
    b, new = journal.record_event("telegram", "42", {})
    assert new and a != b


def test_event_payload_roundtrip_cyrillic(journal):
    eid, _ = journal.record_event("telegram", "7", {"text": "Анна, лендинг к пятнице", "n": 3})
    ev = journal.event(eid)
    assert ev["payload"] == {"text": "Анна, лендинг к пятнице", "n": 3}
    assert ev["channel"] == "telegram"
    assert ev["received_at"] == journal.clock.now()


def test_actions_are_stamped_and_filtered(journal, clock):
    eid, _ = journal.record_event("mail", "1", {})
    journal.log_action("draft", event_id=eid, summary="черновик ответа")
    clock.advance(minutes=2)
    journal.log_action("cancel", event_id=eid, actor="human", path="undo")
    journal.log_action("digest", actor="system")
    acts = journal.actions(event_id=eid)
    assert [a["kind"] for a in acts] == ["draft", "cancel"]
    assert acts[1]["actor"] == "human"
    assert (acts[1]["at"] - acts[0]["at"]).seconds == 120
    assert len(journal.actions()) == 3


def test_unknown_actor_rejected(journal):
    with pytest.raises(ValueError):
        journal.log_action("x", actor="robot")


def test_llm_cost_total_and_since(journal, clock):
    journal.log_llm_call("deepseek", "deepseek-flash", 1000, 200, 0.0012, 850)
    start = clock.now()
    clock.advance(hours=1)
    journal.log_llm_call("deepseek", "deepseek-v4-pro", 3000, 500, 0.0100, 2100)
    assert journal.cost() == pytest.approx(0.0112)
    assert journal.cost(since=clock.now()) == pytest.approx(0.0100)
    assert journal.cost(since=start) == pytest.approx(0.0112)


def test_llm_call_rejects_negative_tokens(journal):
    with pytest.raises(ValueError):
        journal.log_llm_call("deepseek", "m", -1, 0, 0.0, 0)


def test_day_summary(journal, clock):
    e1, _ = journal.record_event("mail", "a", {})
    e2, _ = journal.record_event("telegram", "b", {})
    journal.log_action("send", event_id=e1, path="now")
    journal.log_action("send", event_id=e2, path="now")
    journal.log_action("schedule", event_id=e2, path="undo")
    journal.log_action("decide", event_id=e2, actor="human", path="decision")
    journal.log_llm_call("deepseek", "deepseek-flash", 100, 10, 0.001, 100)
    clock.advance(days=1)
    journal.record_event("mail", "c", {})
    s = journal.day_summary(date(2026, 11, 24))
    assert s["events"] == 2
    assert s["by_path"] == {"now": 2, "undo": 1, "decision": 1}
    assert s["human_actions"] == 1
    assert s["cost_usd"] == pytest.approx(0.001)
    assert journal.day_summary(date(2026, 11, 25))["events"] == 1


def test_persists_after_reopen(tmp_path, clock):
    p = tmp_path / "j.sqlite"
    j = Journal(p, clock)
    j.record_event("mail", "x", {"a": 1})
    j.close()
    j2 = Journal(p, clock)
    _, new = j2.record_event("mail", "x", {"a": 1})
    assert not new
    j2.close()
