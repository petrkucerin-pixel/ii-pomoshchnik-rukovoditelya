"""Почта руководителя: учебный CSV, локальный Outlook и Яндекс по IMAP/SMTP."""

import csv
import hashlib
import imaplib
import json
import os
import smtplib
import ssl
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import make_msgid
from html.parser import HTMLParser
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from weakref import WeakValueDictionary


@dataclass(frozen=True)
class Email:
    id: str
    from_name: str
    from_email: str
    to: str
    subject: str
    body: str
    received: datetime
    in_reply_to: str | None = None
    attachments: tuple = ()          # (имя файла, тип, байты)


def _attachments(message: EmailMessage) -> tuple:
    out = []
    for part in message.iter_attachments():
        name = part.get_filename()
        if not name:
            continue
        data = part.get_payload(decode=True) or b""
        out.append((name, part.get_content_type(), data))
    return tuple(out)


def compose(sender: str, to: str, subject: str, body: str, in_reply_to: str | None = None,
            attachments=()) -> EmailMessage:
    """Письмо для SMTP: текст и вложения (имя, тип, байты)."""
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain=message["From"].addresses[0].domain)
    message["Date"] = datetime.now(timezone.utc)
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    message.set_content(body, charset="utf-8")
    for name, ctype, data in attachments or ():
        main, _, sub = (ctype or "application/octet-stream").partition("/")
        message.add_attachment(data, maintype=main, subtype=sub or "octet-stream", filename=name)
    return message


class _VisibleText(HTMLParser):
    _blocks = {"br", "div", "p", "li", "tr", "td", "th", "blockquote",
               "h1", "h2", "h3", "h4", "h5", "h6"}
    _hidden = {"head", "style", "script"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._hidden:
            self.hidden_depth += 1
        elif tag in self._blocks and not self.hidden_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._hidden:
            self.hidden_depth = max(0, self.hidden_depth - 1)
        elif tag in self._blocks and not self.hidden_depth:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden_depth:
            self.parts.append(data)

    def text(self):
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)


def _body(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except LookupError:
        text = part.get_payload(decode=True).decode("utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        parser = _VisibleText()
        parser.feed(text)
        parser.close()
        return parser.text()
    return text


def parse_message(raw: bytes) -> Email:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    sender = message.get("From")
    address = sender.addresses[0] if sender and sender.addresses else None
    date = message.get("Date")
    received = date.datetime if date and date.datetime else datetime.now(timezone.utc)
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    # Для писем без Message-ID повторное чтение тех же байтов даёт тот же id.
    message_id = message.get("Message-ID") or f"<{hashlib.sha256(raw).hexdigest()}@mail.invalid>"
    return Email(
        id=str(message_id),
        from_name=address.display_name if address else "",
        from_email=address.addr_spec if address else "",
        to=str(message.get("To", "")),
        subject=str(message.get("Subject", "")),
        body=_body(message),
        received=received,
        in_reply_to=message.get("In-Reply-To"),
        attachments=_attachments(message),
    )


class CsvMailbox:
    def __init__(self, path: str | Path, tz: tzinfo | None = None):
        self._path = Path(path)
        self._tz = tz if tz is not None else timezone(timedelta(hours=5))
        self._read = False
        self._lock = Lock()

    def fetch_new(self) -> list[Email]:
        with self._lock:
            if self._read:
                return []
            messages = []
            with self._path.open(encoding="utf-8-sig", newline="") as stream:
                for row in csv.DictReader(stream):
                    received = datetime.fromisoformat(row["received"])
                    if received.tzinfo is None:
                        received = received.replace(tzinfo=self._tz)
                    messages.append(Email(
                        id=row["id"], from_name=row["from_name"], from_email=row["from_email"],
                        to=row["to"], subject=row["subject"], body=row["body"], received=received,
                        in_reply_to=row.get("in_reply_to") or None,
                    ))
            self._read = True
            return messages

    def send(self, to: str, subject: str, body: str, in_reply_to: str | None = None) -> str:
        raise NotImplementedError("Учебный ящик CSV доступен только для чтения")


# Экземпляры одного процесса, работающие с одним файлом, разделяют блокировку.
_file_locks = WeakValueDictionary()
_registry_lock = Lock()


def _file_lock(path: Path):
    with _registry_lock:
        return _file_locks.setdefault(path.resolve(), Lock())


class OutlookStub:
    def __init__(self, inbox: CsvMailbox, outbox_path: str | Path):
        self._inbox = inbox
        self._path = Path(outbox_path)
        self._lock = _file_lock(self._path)

    def fetch_new(self) -> list[Email]:
        return self._inbox.fetch_new()

    def send(self, to: str, subject: str, body: str, in_reply_to: str | None = None, attachments=()) -> str:
        message_id = make_msgid(domain="outlook.invalid")
        record = {"id": message_id, "to": to, "subject": subject,
                  "body": body, "in_reply_to": in_reply_to}
        if attachments:
            record["attachments"] = [name for name, _, _ in attachments]
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return message_id

    def sent(self) -> list[dict]:
        with self._lock:
            if not self._path.exists():
                return []
            with self._path.open(encoding="utf-8") as stream:
                return [json.loads(line) for line in stream if line.strip()]


@dataclass(frozen=True)
class _Cursor:
    last_uid: int = 0
    uidvalidity: int | None = None


def _save_cursor(path: Path, cursor: _Cursor) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)
    try:
        with temporary:
            json.dump({"last_uid": cursor.last_uid, "uidvalidity": cursor.uidvalidity}, temporary)
        os.replace(temporary.name, path)
    finally:
        Path(temporary.name).unlink(missing_ok=True)


class _ImapResponseError(RuntimeError):
    """Сообщение содержит только наше название операции."""


def _imap_data(result, operation):
    status, data = result
    if status != "OK":
        raise _ImapResponseError(f"Яндекс отклонил команду IMAP {operation}")
    return data


class YandexMailbox:
    def __init__(self, address, password, *, imap_factory=None, smtp_factory=None,
                 state_path=None, imap_host="imap.yandex.ru", smtp_host="smtp.yandex.ru"):
        self._address = address
        self._password = password
        self._imap_factory = imap_factory or (
            lambda: imaplib.IMAP4_SSL(imap_host, 993, ssl_context=ssl.create_default_context(), timeout=30)
        )
        self._smtp_factory = smtp_factory or (
            lambda: smtplib.SMTP_SSL(smtp_host, 465, context=ssl.create_default_context(), timeout=30)
        )
        self._state_path = Path(state_path) if state_path is not None else None
        self._lock = _file_lock(self._state_path) if self._state_path is not None else Lock()
        self._cursor = _Cursor()

    def _load_cursor(self) -> _Cursor:
        if self._state_path is None:
            return self._cursor
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _Cursor()
        last_uid = data["last_uid"]
        if type(last_uid) is not int or last_uid < 0:
            raise ValueError("Некорректный last_uid в файле состояния IMAP")
        return _Cursor(last_uid, data.get("uidvalidity"))

    def _read_batch(self, cursor: _Cursor) -> tuple[list[Email], _Cursor]:
        client = self._imap_factory()
        try:
            if isinstance(client, imaplib.IMAP4):
                client.debug = 0
            _imap_data(client.login(self._address, self._password), "LOGIN")
            _imap_data(client.select("INBOX", readonly=True), "SELECT INBOX")
            validity = cursor.uidvalidity
            if hasattr(client, "response"):
                _, values = client.response("UIDVALIDITY")
                if values and values[0]:
                    validity = int(values[0])
            last = 0 if cursor.uidvalidity not in (None, validity) else cursor.last_uid
            groups = _imap_data(client.uid("search", None, "UID", f"{last + 1}:*"), "UID SEARCH")
            uids = sorted({int(uid) for group in groups if group for uid in group.split()})
            messages = []
            for uid in uids:
                if uid <= last:
                    continue
                data = _imap_data(client.uid("fetch", str(uid), "(RFC822)"), "UID FETCH")
                # OK без литерала и без данных означает, что письмо уже удалено.
                if data and data != [None]:
                    raw = next((item[1] for item in data if isinstance(item, tuple)
                                and len(item) == 2 and isinstance(item[1], bytes)), None)
                    if raw is None:
                        raise _ImapResponseError("IMAP UID FETCH вернул некорректное письмо")
                    messages.append(parse_message(raw))
                last = uid
            return messages, _Cursor(last, validity)
        finally:
            with suppress(imaplib.IMAP4.error, OSError):
                client.logout()

    def fetch_new(self) -> list[Email]:
        with self._lock:
            cursor = self._load_cursor()
            for attempt in range(2):
                try:
                    messages, updated = self._read_batch(cursor)
                except _ImapResponseError:
                    raise
                except (imaplib.IMAP4.abort, OSError) as exc:
                    if attempt == 0:
                        continue
                    failure = type(exc).__name__
                except Exception as exc:
                    failure = type(exc).__name__
                else:
                    if self._state_path is not None and updated != cursor:
                        _save_cursor(self._state_path, updated)
                    self._cursor = updated
                    return messages
                # Ответ сервера может содержать пароль. Исключаем его и из traceback.
                raise RuntimeError(f"Не удалось прочитать почту Яндекса по IMAP ({failure})")

    def send(self, to: str, subject: str, body: str, in_reply_to: str | None = None, attachments=()) -> str:
        message = compose(self._address, to, subject, body, in_reply_to, attachments)
        message_id = message["Message-ID"]

        try:
            client = self._smtp_factory()
            try:
                if isinstance(client, smtplib.SMTP):
                    client.set_debuglevel(0)
                client.login(self._address, self._password)
                refused = client.send_message(message)
            finally:
                # После принятого DATA сбой при QUIT не должен вызвать повторную отправку.
                with suppress(smtplib.SMTPException, OSError):
                    client.quit()
        except Exception as exc:
            failure = type(exc).__name__
        else:
            if refused:
                raise RuntimeError("SMTP Яндекса отклонил одного или нескольких получателей")
            return message_id
        raise RuntimeError(f"Не удалось отправить письмо через SMTP Яндекса ({failure})")
