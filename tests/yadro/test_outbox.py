import pytest

from yadro.outbox import Outbox


@pytest.fixture
def box(journal):
    return Outbox(journal, undo_minutes=5)


def ids(msgs):
    return [m.id for m in msgs]


def test_now_path_is_due_immediately(box):
    m = box.put("telegram", "team", "Анна, лендинг к пятнице", "now")
    assert m.status == "scheduled"
    assert ids(box.due()) == [m.id]


def test_undo_path_waits_for_window(box, clock):
    m = box.put("mail", "partner@x.ru", "Новые условия", "undo")
    assert box.due() == []
    clock.advance(minutes=4, seconds=59)
    assert box.due() == []
    clock.advance(seconds=1)
    assert ids(box.due()) == [m.id]


def test_custom_undo_minutes(journal, clock):
    box = Outbox(journal, undo_minutes=1)
    m = box.put("mail", "p@x.ru", "текст", "undo")
    clock.advance(minutes=1)
    assert ids(box.due()) == [m.id]


def test_cancel_inside_window(box, clock):
    m = box.put("mail", "p@x.ru", "текст", "undo")
    clock.advance(minutes=2)
    assert box.cancel(m.id)
    assert box.get(m.id).status == "cancelled"
    clock.advance(minutes=10)
    assert box.due() == []


def test_cancel_after_window_fails(box, clock):
    m = box.put("mail", "p@x.ru", "текст", "undo")
    clock.advance(minutes=6)
    assert not box.cancel(m.id)
    assert box.get(m.id).status == "scheduled"


def test_cancel_after_sent_fails(box):
    m = box.put("telegram", "team", "текст", "now")
    box.mark_sent(m.id, "tg-100")
    assert not box.cancel(m.id)


def test_edit_and_send_now(box):
    m = box.put("mail", "p@x.ru", "черновик", "undo")
    assert box.edit(m.id, "исправленный текст", send_now=True)
    got = box.get(m.id)
    assert got.body == "исправленный текст"
    assert ids(box.due()) == [m.id]


def test_edit_keeps_window(box, clock):
    m = box.put("mail", "p@x.ru", "черновик", "undo")
    box.edit(m.id, "правка")
    assert box.due() == []
    clock.advance(minutes=5)
    assert box.get(m.id).body == "правка"
    assert ids(box.due()) == [m.id]


def test_edit_after_sent_fails(box):
    m = box.put("telegram", "team", "текст", "now")
    box.mark_sent(m.id, "1")
    assert not box.edit(m.id, "поздно")


def test_decision_waits_for_choice(box):
    m = box.put("mail", "client@x.ru", "", "decision", options=["Да, скидка 10 %", "Нет, по прайсу"])
    assert m.status == "awaiting_decision"
    assert box.due() == []
    assert box.decide(m.id, choice=1)
    got = box.get(m.id)
    assert got.body == "Нет, по прайсу"
    assert ids(box.due()) == [m.id]


def test_decision_with_own_text(box):
    m = box.put("mail", "client@x.ru", "", "decision", options=["А", "Б"])
    assert box.decide(m.id, body="Свой ответ руководителя")
    assert box.get(m.id).body == "Свой ответ руководителя"


def test_decision_reject(box):
    m = box.put("mail", "client@x.ru", "", "decision", options=["А"])
    assert box.decide(m.id, reject=True)
    assert box.get(m.id).status == "rejected"
    assert box.due() == []


def test_decision_bad_choice(box):
    m = box.put("mail", "client@x.ru", "", "decision", options=["А"])
    with pytest.raises(ValueError):
        box.decide(m.id, choice=5)


def test_decide_only_for_decision_messages(box):
    m = box.put("telegram", "team", "текст", "now")
    assert not box.decide(m.id, choice=0)


def test_decision_needs_options_or_body(box):
    with pytest.raises(ValueError):
        box.put("mail", "c@x.ru", "", "decision")


def test_unknown_path_rejected(box):
    with pytest.raises(ValueError):
        box.put("mail", "c@x.ru", "текст", "later")


def test_mark_sent_is_idempotent(box):
    m = box.put("telegram", "team", "текст", "now")
    assert box.mark_sent(m.id, "tg-1")
    assert not box.mark_sent(m.id, "tg-2")
    got = box.get(m.id)
    assert got.status == "sent" and got.external_id == "tg-1"
    assert box.due() == []


def test_due_is_ordered_by_send_time(box, clock):
    a = box.put("mail", "p@x.ru", "a", "undo")
    clock.advance(minutes=1)
    b = box.put("telegram", "team", "b", "now")
    clock.advance(minutes=10)
    # b ушёл бы в 10:01, a — в 10:05 после окна отмены
    assert ids(box.due()) == [b.id, a.id]


def test_actions_go_to_journal_with_actor(box, journal):
    eid, _ = journal.record_event("mail", "m1", {})
    m = box.put("mail", "p@x.ru", "текст", "undo", event_id=eid)
    box.cancel(m.id)
    acts = journal.actions(event_id=eid)
    assert [(a["kind"], a["actor"], a["path"]) for a in acts] == [
        ("outbox_put", "assistant", "undo"),
        ("outbox_cancel", "human", "undo"),
    ]


def test_sent_logged_by_system(box, journal):
    m = box.put("telegram", "team", "текст", "now")
    box.mark_sent(m.id, "x")
    assert journal.actions()[-1]["kind"] == "outbox_sent"
    assert journal.actions()[-1]["actor"] == "system"


def test_pending_decisions_list(box):
    box.put("mail", "a@x.ru", "", "decision", options=["1"])
    box.put("telegram", "team", "x", "now")
    assert len(box.awaiting_decision()) == 1
