"""TASK_16: голос в Telegram. Распознавание и модель — заглушки; проверяем, что код делает с понятой просьбой."""
from datetime import datetime, timedelta

from test_assistant import a, cb, llm_answer  # noqa: F401 — a — фикстура помощника на учебных данных

from pomoshnik.channels.telegram import Update
from pomoshnik.llm import StubLLM


def voice():
    return Update(2, "voice", 555, "", 11, file_id="F1")


def text(t):
    return Update(3, "text", 555, t, 12)


def setup(asst, heard, intent):
    """Руководитель сказал `heard`; модель поняла это как `intent`."""
    asst.pult.download = lambda fid: b"OGG" if fid == "F1" else b""
    asst.transcribe = lambda audio: heard if audio == b"OGG" else ""
    base = asst.llm

    def answer(system, user):
        if "разбери просьбу руководителя" in system.lower():
            return intent
        return base.complete_json(system, user)
    asst.llm = StubLLM(answer)


def sent(asst):
    return [t for _, t, _ in asst.pult.sent]


def test_voice_is_echoed_and_unknown_gets_a_hint(a):
    asst, _, _ = a
    setup(asst, "Эээ, ну это самое", {"intent": "unknown"})
    asst.handle(voice())
    assert "🎙" in sent(asst)[-2] and "Эээ, ну это самое" in sent(asst)[-2]
    assert "Можно сказать" in sent(asst)[-1]


def test_no_transcriber_or_silence(a):
    asst, _, _ = a
    asst.handle(voice())
    assert "Голосовые пока не подключены" in sent(asst)[-1]
    setup(asst, "", {"intent": "unknown"})
    asst.transcribe = lambda audio: "  "
    asst.handle(voice())
    assert "Не расслышал" in sent(asst)[-1]


def test_decision_by_voice_waits_for_one_tap_then_sends_leader_words(a):
    asst, _, mail = a
    asst.process_inbox()
    g = asst.active_groups()[0]
    setup(asst, "Откажи, бюджет только с января", {
        "intent": "decision", "verdict": "no",
        "reply": "Спасибо за предложение. Сейчас не можем: бюджет на это будет только с января."})
    before = len(mail.out)
    asst.handle(voice())
    kind, card, buttons = asst.pult.sent[-1]
    assert "Отказать" in card and g["subject"] in card and "бюджет на это будет только с января" in card
    assert buttons == [f"vok:{g['event_id']}", f"vno:{g['event_id']}"]
    assert len(mail.out) == before                                      # без нажатия ничего не уходит
    asst.handle(cb(f"vok:{g['event_id']}"))
    assert len(mail.out) == before + 1 and "только с января" in mail.out[-1]["body"]
    assert mail.out[-1]["to"] == g["email"]
    assert g["key"] not in {x["key"] for x in asst.groups()}


def test_wrong_recognition_is_cancelled(a):
    asst, _, mail = a
    asst.process_inbox()
    g = asst.active_groups()[0]
    setup(asst, "Согласуй", {"intent": "decision", "verdict": "yes", "reply": ""})
    asst.handle(voice())
    before = len(mail.out)
    asst.handle(cb(f"vno:{g['event_id']}"))
    assert len(mail.out) == before and "не отправляю" in sent(asst)[-1]
    assert g["key"] in {x["key"] for x in asst.groups()}


def test_later_by_voice_is_immediate(a):
    asst, _, mail = a
    asst.process_inbox()
    g = asst.active_groups()[0]
    setup(asst, "Давай потом", {"intent": "decision", "verdict": "later"})
    asst.handle(voice())
    assert g["key"] not in {x["key"] for x in asst.active_groups()}


def test_recall_by_voice(a):
    asst, _, _ = a
    setup(asst, "Что по Облако Плюс?", {"intent": "recall", "query": "Облако Плюс"})
    asst.recall = lambda q: f"ПАМЯТЬ:{q}"
    asst.handle(voice())
    assert sent(asst)[-1] == "ПАМЯТЬ:Облако Плюс"


def test_assign_writes_to_the_person_with_deadline(a):
    asst, clock, mail = a
    setup(asst, "Напомни Ольге подготовить КП для ТехноСтроя к пятнице", {
        "intent": "assign", "person": "Ольга", "what": "подготовить КП для «ТехноСтрой»", "due_text": "к пятнице"})
    asst.handle(voice())
    m = mail.out[-1]
    assert m["to"] == "p02@company.example" and "КП для «ТехноСтрой»" in m["body"] and "11.12" in m["body"]
    assert "Ольге Смирновой" in sent(asst)[-1] or "Ольга Смирнова" in sent(asst)[-1]
    assert any(x["kind"] == "voice_assign" for x in asst.j.actions())


def test_assign_to_unknown_person_says_so(a):
    asst, _, mail = a
    setup(asst, "Напомни Пете про отчёт", {"intent": "assign", "person": "Петя", "what": "отчёт", "due_text": ""})
    before = len(mail.out)
    asst.handle(voice())
    assert len(mail.out) == before and "Не нашёл" in sent(asst)[-1]


def test_meeting_by_voice(a):
    asst, clock, mail = a
    tomorrow = (clock.now() + timedelta(days=1)).date()
    setup(asst, "Назначь планёрку завтра в десять с Ольгой", {
        "intent": "meeting", "title": "Планёрка", "date": tomorrow.isoformat(), "time": "10:00", "minutes": 30,
        "people": ["Ольга"]})
    asst.handle(voice())
    m = next(x for x in asst.meetings if x.title == "Планёрка" and x.start.date() == tomorrow)
    assert m.start.hour == 10 and "Ольга Смирнова" in m.attendees
    assert mail.out[-1]["to"] == "p02@company.example" and "Приглаш" in mail.out[-1]["subject"]
    assert "Планёрка" in sent(asst)[-1] and "10:00" in sent(asst)[-1]


def test_meeting_with_bad_time_is_not_created(a):
    asst, _, _ = a
    n = len(asst.meetings)
    setup(asst, "Назначь встречу", {"intent": "meeting", "title": "Встреча", "date": "вчера", "time": "25:99"})
    asst.handle(voice())
    assert len(asst.meetings) == n and "дату и время" in sent(asst)[-1]


def test_protocol_edit_by_voice(a):
    asst, clock, _ = a
    pid = asst.new_meeting("Сергей, договор с «Облако Плюс» нужно подписать к пятнице.", "Планёрка", clock.now())
    calls = []

    def fake_edit(p, instruction, llm, org):
        calls.append((p, instruction))
        return asst.protocols.protocol(p, org)
    asst.protocols.apply_edit = fake_edit
    setup(asst, "В протоколе срок по договору — понедельник", {
        "intent": "protocol_edit", "instruction": "срок по договору — понедельник"})
    asst.handle(voice())
    assert calls == [(pid, "срок по договору — понедельник")]
    assert asst.pult.sent[-1][2][0] == f"prot:ok:{pid}"                  # снова на утверждение


def test_free_text_goes_through_the_same_router(a):
    asst, _, _ = a
    setup(asst, "", {"intent": "recall", "query": "Ромашка"})
    asst.recall = lambda q: f"ПАМЯТЬ:{q}"
    asst.handle(text("Слушай, а что там у Ромашки"))
    assert sent(asst)[-1] == "ПАМЯТЬ:Ромашка"


def test_recognition_failure_is_reported_not_crashed(a):
    asst, _, _ = a
    setup(asst, "x", {"intent": "unknown"})

    def broken(audio):
        raise RuntimeError("whisper упал")
    asst.transcribe = broken
    asst.handle(voice())
    assert "Не получилось распознать" in sent(asst)[-1]
