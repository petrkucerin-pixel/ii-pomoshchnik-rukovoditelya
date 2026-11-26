"""Контракт почтовых модулей. Пишет Claude, исполнитель (Codex) этот файл не меняет."""
import email.message
import imaplib
import json
from email.header import Header
from email.utils import formataddr, make_msgid
from pathlib import Path

import pytest

from pomoshnik.channels.mail import CsvMailbox, Email, OutlookStub, YandexMailbox, parse_message

INBOX_CSV = Path(__file__).resolve().parent.parent / "data" / "task" / "inbox.csv"
PASSWORD = "SECRETPASSWORD16"


def raw_letter(subject="Перенос встречи", body="Коллеги, переносим на среду.", charset="utf-8",
               html=False, msgid="<a1@x>", sender=("Ольга Смирнова", "olga@company.example")):
    m = email.message.EmailMessage()
    m["Subject"] = Header(subject, charset).encode()
    m["From"] = formataddr((str(Header(sender[0], charset).encode()), sender[1]))
    m["To"] = "lead@company.example"
    m["Date"] = "Mon, 07 Dec 2026 09:15:00 +0500"
    m["Message-ID"] = msgid
    if html:
        m.set_content("<p>" + body + "</p>", subtype="html", charset=charset)
    else:
        m.set_content(body, charset=charset)
    return m.as_bytes()


# --- parse_message -------------------------------------------------------------

def test_parse_utf8():
    e = parse_message(raw_letter())
    assert isinstance(e, Email)
    assert e.subject == "Перенос встречи"
    assert e.from_name == "Ольга Смирнова" and e.from_email == "olga@company.example"
    assert "переносим на среду" in e.body
    assert e.id == "<a1@x>"
    assert e.received.utcoffset().total_seconds() == 5 * 3600


@pytest.mark.parametrize("cs", ["koi8-r", "windows-1251"])
def test_parse_legacy_charsets(cs):
    e = parse_message(raw_letter(charset=cs))
    assert e.subject == "Перенос встречи"
    assert "переносим" in e.body


def test_parse_html_only_strips_tags():
    e = parse_message(raw_letter(html=True, body="Жалоба <b>клиента</b>"))
    assert "<" not in e.body and "Жалоба" in e.body and "клиента" in e.body


# --- CSV и заглушка Outlook ----------------------------------------------------

def test_csv_mailbox_reads_all_once():
    box = CsvMailbox(INBOX_CSV)
    first = box.fetch_new()
    assert len(first) == 80
    assert first[0].id == "m001"
    assert first[0].received.tzinfo is not None
    assert box.fetch_new() == []
    with pytest.raises(NotImplementedError):
        box.send("a@b", "s", "b")


def test_outlook_stub_records_sent(tmp_path):
    out = tmp_path / "outlook_sent.jsonl"
    stub = OutlookStub(CsvMailbox(INBOX_CSV), out)
    assert len(stub.fetch_new()) == 80
    mid = stub.send("olga@company.example", "Протокол встречи", "Текст протокола", in_reply_to="m002")
    assert mid
    rec = stub.sent()
    assert len(rec) == 1 and rec[0]["to"] == "olga@company.example" and rec[0]["subject"] == "Протокол встречи"
    assert rec[0]["in_reply_to"] == "m002"
    assert json.loads(out.read_text(encoding="utf-8").splitlines()[0])["body"] == "Текст протокола"


# --- Яндекс через подделки IMAP/SMTP -------------------------------------------

class FakeIMAP:
    def __init__(self, store, fail_once=None):
        self.store, self.fail_once, self.logged = store, fail_once, None

    def login(self, user, pwd):
        if self.fail_once:
            exc, self.fail_once = self.fail_once, None
            raise exc
        self.logged = (user, pwd)
        return "OK", [b"Logged in"]

    def select(self, box, readonly=False):
        assert readonly
        return "OK", [str(len(self.store)).encode()]

    def uid(self, cmd, *args):
        if cmd.lower() == "search":
            start = int(args[-1].split(":")[0])
            found = [u for u in self.store if u >= start] or [max(self.store)]
            return "OK", [" ".join(map(str, found)).encode()]
        if cmd.lower() == "fetch":
            u = int(args[0])
            return "OK", [(f"{u} (UID {u} RFC822 {{1}}".encode(), self.store[u]), b")"]
        raise AssertionError(cmd)

    def logout(self):
        return "BYE", []


class FakeSMTP:
    sent = []

    def login(self, u, p):
        return 235, b"ok"

    def send_message(self, msg, *a, **k):
        FakeSMTP.sent.append(msg)
        return {}

    def quit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_box(tmp_path, store, fail_once=None):
    holder = {}

    def imap_factory():
        holder["imap"] = FakeIMAP(store, fail_once)
        return holder["imap"]

    return YandexMailbox("pit@yandex.ru", PASSWORD, imap_factory=imap_factory,
                         smtp_factory=lambda: FakeSMTP(), state_path=tmp_path / "state.json"), holder


def test_yandex_fetch_only_new_and_remembers_uid(tmp_path):
    store = {5: raw_letter(msgid="<m5@x>"), 7: raw_letter(subject="Отчёт", msgid="<m7@x>")}
    box, _ = make_box(tmp_path, store)
    assert [e.id for e in box.fetch_new()] == ["<m5@x>", "<m7@x>"]
    assert box.fetch_new() == []                       # сервер вернул 7 на запрос 8:* — отфильтровано
    store[9] = raw_letter(subject="Новое", msgid="<m9@x>")
    box2, _ = make_box(tmp_path, store)                # новый объект читает UID из state_path
    assert [e.id for e in box2.fetch_new()] == ["<m9@x>"]


def test_yandex_retries_once_on_abort(tmp_path):
    store = {1: raw_letter(msgid="<m1@x>")}
    calls = {"n": 0}

    def imap_factory():
        calls["n"] += 1
        return FakeIMAP(store, imaplib.IMAP4.abort("связь оборвалась") if calls["n"] == 1 else None)

    box = YandexMailbox("pit@yandex.ru", PASSWORD, imap_factory=imap_factory,
                        smtp_factory=lambda: FakeSMTP(), state_path=tmp_path / "s.json")
    assert [e.id for e in box.fetch_new()] == ["<m1@x>"]
    assert calls["n"] == 2


def test_yandex_send_headers_and_reply(tmp_path):
    FakeSMTP.sent.clear()
    box, _ = make_box(tmp_path, {1: raw_letter()})
    mid = box.send("olga@company.example", "Протокол: архитектура", "Решения и задачи", in_reply_to="<a1@x>")
    msg = FakeSMTP.sent[-1]
    assert msg["Message-ID"] == mid
    assert msg["In-Reply-To"] == "<a1@x>" and "<a1@x>" in msg["References"]
    assert msg["To"] == "olga@company.example"
    assert "utf-8" in msg.get_content_type() + str(msg.get_content_charset())
    assert parse_message(msg.as_bytes()).subject == "Протокол: архитектура"


def test_password_not_leaked_on_error(tmp_path):
    def bad_factory():
        return FakeIMAP({1: raw_letter()}, imaplib.IMAP4.error("AUTHENTICATIONFAILED " + PASSWORD))

    box = YandexMailbox("pit@yandex.ru", PASSWORD, imap_factory=bad_factory,
                        smtp_factory=lambda: FakeSMTP(), state_path=tmp_path / "s.json")
    with pytest.raises(Exception) as e:
        box.fetch_new()
    assert PASSWORD not in str(e.value)
