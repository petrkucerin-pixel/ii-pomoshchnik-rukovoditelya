"""Проверки повторного чтения, составных писем и ошибок почтовых серверов."""

import csv
import imaplib
import smtplib
import traceback
from datetime import timedelta, timezone
from email.message import EmailMessage
from unittest.mock import Mock

import pytest

from pomoshnik.channels.mail import CsvMailbox, OutlookStub, YandexMailbox, parse_message


def letter(text):
    message = EmailMessage()
    message.set_content(text)
    return message


def test_multipart_prefers_plain_and_skips_forwarded_attachments():
    mixed = EmailMessage()
    mixed.make_mixed()
    attachment = letter("содержимое файла")
    attachment.add_header("Content-Disposition", "attachment", filename="readme.txt")
    mixed.attach(attachment)
    body = letter("решение в тексте")
    body.add_alternative("<p>альтернатива</p>", subtype="html")
    mixed.attach(body)

    raw = mixed.as_bytes()
    parsed = parse_message(raw)
    assert parsed.body.strip() == "решение в тексте"
    assert parsed.id == parse_message(raw).id
    assert parsed.received.utcoffset() is not None


def test_html_body_contains_visible_text_and_paragraph_breaks():
    message = EmailMessage()
    message.set_content("<head><title>заголовок</title></head><p>Срок &amp; план</p>"
                        "<script>скрыто</script><p>Два <b>дня</b></p>", subtype="html")
    assert parse_message(message.as_bytes()).body == "Срок & план\nДва дня"


def test_csv_read_can_be_retried_after_bad_row(tmp_path):
    path = tmp_path / "inbox.csv"
    box = CsvMailbox(path, tz=timezone(timedelta(hours=3)))

    def write(second_date):
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["id", "from_name", "from_email", "to", "subject", "body", "received"])
            for mid, date in enumerate(["2026-12-07 10:00", second_date]):
                writer.writerow([mid, "Тест", "a@example.org", "b@example.org", "Итог", "План", date])

    write("ошибочная дата")
    with pytest.raises(ValueError):
        box.fetch_new()
    write("2026-12-07T10:00:00+02:00")
    first, second = box.fetch_new()
    assert first.received.utcoffset() == timedelta(hours=3)
    assert second.received.utcoffset() == timedelta(hours=2)
    assert box.fetch_new() == []


def test_outlook_reads_sent_after_reopening(tmp_path):
    path = tmp_path / "office" / "out.jsonl"
    box = OutlookStub(Mock(), path)
    assert box.sent() == []
    mid = box.send("a@example.org", "Итог", "Строка 1\nСтрока 2")
    reopened = OutlookStub(Mock(), path)
    reply = reopened.send("b@example.org", "Ответ", "Да", in_reply_to=mid)
    first, second = reopened.sent()
    assert first["body"] == "Строка 1\nСтрока 2"
    assert (first["id"], second["id"], second["in_reply_to"]) == (mid, reply, mid)
    assert mid != reply


def imap(validity, *replies):
    client = Mock(spec=imaplib.IMAP4)
    client.login.return_value = client.select.return_value = ("OK", [])
    client.response.return_value = ("UIDVALIDITY", [str(validity).encode()])
    client.uid.side_effect = replies
    return client


def found(*uids):
    return "OK", [" ".join(map(str, uids)).encode()]


def fetched(text):
    return "OK", [(b"RFC822", letter(text).as_bytes()), b")"]


def test_imap_retries_whole_batch_and_persists_only_delivered_uids(tmp_path):
    path = tmp_path / "state.json"
    secret = "unit-test-password"
    broken = [imap(8, found(2, 3), fetched("а"), OSError(secret)) for _ in range(2)]
    good = imap(8, found(2, 3), fetched("а"), fetched("б"))
    factory = Mock(side_effect=[*broken, good])
    box = YandexMailbox("test@example.org", secret, imap_factory=factory, state_path=path)
    with pytest.raises(RuntimeError) as error:
        box.fetch_new()
    assert secret not in "".join(traceback.format_exception(error.value))
    assert factory.call_count == 2
    assert not path.exists()
    assert [msg.body.strip() for msg in box.fetch_new()] == ["а", "б"]
    for client in [*broken, good]:
        client.logout.assert_called_once_with()
        assert client.uid.call_args_list[0].args == ("search", None, "UID", "1:*")


def test_uidvalidity_change_starts_at_first_uid_again(tmp_path):
    path = tmp_path / "state.json"
    original = imap(10, found(90), fetched("старое"))
    renewed = imap(11, found(1), fetched("новое"))
    for client, expected in [(original, "старое"), (renewed, "новое")]:
        box = YandexMailbox("test@example.org", "dummy", imap_factory=lambda: client, state_path=path)
        assert [msg.body.strip() for msg in box.fetch_new()] == [expected]
        assert client.uid.call_args_list[0].args == ("search", None, "UID", "1:*")


def test_smtp_error_hides_credentials_and_is_not_retried():
    secret = "unit-test-password"
    client = Mock(spec=smtplib.SMTP)
    client.login.side_effect = smtplib.SMTPAuthenticationError(535, secret.encode())
    factory = Mock(return_value=client)
    box = YandexMailbox("test@example.org", secret, smtp_factory=factory)
    with pytest.raises(RuntimeError) as error:
        box.send("user@example.org", "Тема", "Текст")
    assert secret not in "".join(traceback.format_exception(error.value))
    factory.assert_called_once_with()
    client.send_message.assert_not_called()
    client.quit.assert_called_once_with()


def test_smtp_disconnect_after_accepted_data_still_returns_message_id():
    client = Mock(spec=smtplib.SMTP)
    client.send_message.return_value = {}
    client.quit.side_effect = smtplib.SMTPServerDisconnected("QUIT")
    box = YandexMailbox("test@example.org", "dummy", smtp_factory=lambda: client)
    mid = box.send("user@example.org", "Протокол", "Первый пункт")
    client.send_message.assert_called_once()
    sent = client.send_message.call_args.args[0]
    assert sent["Message-ID"] == mid
    assert sent["From"] == "test@example.org"
    assert sent["To"] == "user@example.org"
    assert b"=?utf-8?" in sent.as_bytes().lower()
