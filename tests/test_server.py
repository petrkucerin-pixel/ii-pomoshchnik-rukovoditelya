import pytest
from fastapi.testclient import TestClient

import pomoshnik.server as srv


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "RUNTIME", tmp_path)
    monkeypatch.setattr(srv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with TestClient(srv.app) as c:
        yield c
    srv.stand.journal.close()
    srv.stand.protocols.close()
    srv.stand.memory.close()


def test_health_without_bot_token(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] and r.json()["bot"] == "нет токена"


def test_index_and_metrics(client, tmp_path):
    srv.stand.journal.log_action("mail_авто", path="now", payload={"minutes_saved": 3})
    srv.stand.journal.log_action("mail_спросить_руководителя", path="decision", payload={"minutes_saved": 2})
    m = client.get("/api/metrics").json()
    assert m["letters"] == 2 and m["share_without_leader"] == 0.5 and m["hours_saved"] == pytest.approx(5 / 60, abs=0.01)
    page = client.get("/").text
    assert "ИИ-помощник руководителя" in page and "Иван Петров" in page and "без руководителя" in page
    assert (tmp_path / "ivan_petrov.sqlite").exists()


class FakeReal:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def fetch_new(self):
        if self.fail:
            raise RuntimeError("Не удалось прочитать почту Яндекса по IMAP (OSError)")
        return ["настоящее письмо"]

    def send(self, to, subject, body, in_reply_to=None):
        self.sent.append(to)
        return "<real@yandex>"


def test_combined_mailbox_routes_real_and_fictional(tmp_path):
    from pomoshnik.channels.mail import CsvMailbox, OutlookStub
    stub = OutlookStub(CsvMailbox(srv.TASK / "inbox.csv"), tmp_path / "sent.jsonl")
    real = FakeReal()
    box = srv.CombinedMailbox(stub, real)
    assert len(box.fetch_new()) == 81
    box.send("p02@company.example", "Fwd", "x")
    box.send("pit@yandex.ru", "Протокол", "x")
    assert real.sent == ["pit@yandex.ru"] and len(stub.sent()) == 1


def test_combined_mailbox_survives_yandex_failure(tmp_path):
    from pomoshnik.channels.mail import CsvMailbox, OutlookStub
    box = srv.CombinedMailbox(OutlookStub(CsvMailbox(srv.TASK / "inbox.csv"), tmp_path / "s.jsonl"), FakeReal(fail=True))
    assert len(box.fetch_new()) == 80 and "IMAP" in box.last_error


def test_meeting_before_bot_bound(client):
    r = client.post("/meeting", data={"title": "x", "transcript": "y"})
    assert r.status_code == 409


def _seed_memory():
    from datetime import datetime
    from pomoshnik.memory import Fact
    from yadro.clock import TZ_EKB
    srv.stand.memory.add([Fact("Облако Плюс", "партнёр", "договор у юристов, ждём до 12.12", "Сергей Морозов",
                               datetime(2026, 12, 8, 14, 10, tzinfo=TZ_EKB), "m022", "Договор на подписи у юристов")])
    srv.stand.journal.log_action("report_review", path="now", summary="Облако Плюс: статус не меняется",
                                 payload={"kind": "нет_движения", "topic": "Облако Плюс", "day": "2026-12-11",
                                          "to": "p05@company.example", "to_name": "Сергей Морозов",
                                          "question": "Сергей, что мешает подписать договор?"})


def test_index_shows_memory_and_avatar_questions(client, monkeypatch):
    monkeypatch.setenv("DEMO_DATE", "2026-12-11")
    _seed_memory()
    page = client.get("/").text
    assert "Память помощника" in page and "Облако Плюс" in page and "ждём до 12.12" in page
    assert "Что аватар спросит на встрече" in page and "что мешает подписать договор" in page


def test_index_memory_search(client):
    _seed_memory()
    page = client.get("/", params={"q": "облака"}).text
    assert "Договор на подписи у юристов" in page and "m022" in page
    assert "ничего нет" in client.get("/", params={"q": "Марс"}).text


def test_index_escapes_memory(client):
    from datetime import datetime
    from pomoshnik.memory import Fact
    from yadro.clock import TZ_EKB
    srv.stand.memory.add([Fact("<script>x</script>", "сделка", "<b>что</b>", "кто",
                               datetime(2026, 12, 8, tzinfo=TZ_EKB), "m1", "цитата из письма")])
    page = client.get("/").text
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page
