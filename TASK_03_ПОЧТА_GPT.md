# TASK_03_GPT: Почта — Яндекс (настоящая), Outlook (заглушка), учебный ящик из CSV

**Исполнитель:** Codex (GPT). **Приёмка:** Claude.
**Ветка:** `gpt/03-mail`. Сливает Claude после приёмки.

## Зачем

Агент читает входящие письма руководителя и отправляет письма: протоколы, делегирование, ответы. По ТЗ почта заказчика — Outlook, на хакатоне допустима заглушка. Настоящую связь показываем через Яндекс 360 (IMAP/SMTP). Все три источника отдают письма в одном формате, поэтому агенту неважно, откуда письмо.

## Что создать

- `pomoshnik/channels/mail.py`

**Не трогать:** `yadro/`, `tests/yadro/`, `tests/test_mail_contract.py` (контракт), `data/task/`, любые `*.md` кроме этого TASK. Свои тесты кладите в `tests/test_mail_extra.py`.

## Интерфейс (стык)

```python
from pomoshnik.channels.mail import Email, parse_message, CsvMailbox, OutlookStub, YandexMailbox

@dataclass(frozen=True)
class Email:
    id: str                 # Message-ID, для CSV — значение колонки id
    from_name: str
    from_email: str
    to: str
    subject: str
    body: str               # только текст: text/plain, иначе text/html без тегов
    received: datetime      # с часовым поясом
    in_reply_to: str | None = None

parse_message(raw: bytes) -> Email   # RFC 822: кодированные заголовки, multipart, utf-8 / koi8-r / windows-1251

class CsvMailbox:                      # учебный ящик из data/task/inbox.csv
    def __init__(self, path, tz=None)  # tz по умолчанию UTC+5
    def fetch_new(self) -> list[Email]          # при первом вызове все письма, потом []
    def send(self, to, subject, body, in_reply_to=None) -> str   # NotImplementedError

class OutlookStub:                     # заглушка Outlook: входящие из CSV, отправленные — в JSONL-файл
    def __init__(self, inbox: CsvMailbox, outbox_path)
    def fetch_new(self) -> list[Email]
    def send(self, to, subject, body, in_reply_to=None) -> str   # пишет строку JSON в outbox_path, возвращает id
    def sent(self) -> list[dict]

class YandexMailbox:                   # настоящая почта: IMAP 993 и SMTP 465 (SSL)
    def __init__(self, address, password, *, imap_factory=None, smtp_factory=None, state_path=None,
                 imap_host="imap.yandex.ru", smtp_host="smtp.yandex.ru")
    def fetch_new(self) -> list[Email]          # только письма с UID больше запомненного; UID хранится в state_path (JSON)
    def send(self, to, subject, body, in_reply_to=None) -> str   # возвращает Message-ID отправленного письма
```

Требования:
- `imap_factory()` и `smtp_factory()` возвращают объекты с интерфейсом `imaplib.IMAP4_SSL` и `smtplib.SMTP_SSL`. По умолчанию это настоящие классы. В тестах — подделки.
- `fetch_new` Яндекса: `login` → `select("INBOX", readonly=True)` → `uid("search", None, "UID", f"{last+1}:*")` → `uid("fetch", uid, "(RFC822)")`. IMAP на диапазоне `N:*` может вернуть последний UID, даже если он не больше запомненного, — такие отфильтровать. Один повтор при обрыве связи (`imaplib.IMAP4.abort`, `OSError`).
- `send`: письмо `text/plain; charset=utf-8`, заголовки кодируются, Message-ID генерируется, при ответе ставятся `In-Reply-To` и `References`.
- Пароль нигде не логируется и не попадает в исключения.

## Как проверить

```
.venv/Scripts/python -m pytest tests/test_mail_contract.py
.venv/Scripts/python -m pytest
```

## Вне объёма

Календарь (CalDAV), разбор писем по смыслу, вложения, OAuth для Outlook.

## Проверка Codex, 26 ноября 2026

Реализация находится в [почтовом модуле](pomoshnik/channels/mail.py). Учебный ящик читает 80 писем из CSV. Заглушка Outlook сохраняет исходящие в заданный JSONL. Яндекс читает INBOX в режиме только для чтения и отправляет письма через SMTP с проверкой TLS-сертификата.

Для каждого ящика Яндекса можно передать отдельный `state_path`. Курсор сохраняется после успешного чтения всей пачки. Смена IMAP UIDVALIDITY начинает отсчёт заново. При работе без файла курсор хранится в экземпляре ящика.

Команды для PowerShell из корня рабочей копии:

```powershell
& '..\Кейс_3_ИИ_помощник_руководителя\.venv\Scripts\python' -m pytest tests/test_mail_contract.py
& '..\Кейс_3_ИИ_помощник_руководителя\.venv\Scripts\python' -m pytest
```

Прошли 10 почтовых контрактных тестов и полный набор из 172 тестов. IMAP и SMTP проверены без сети. Среди дополнительных проверок — повтор чтения после обрыва посреди пачки, перезапуск после смены UIDVALIDITY, скрытие пароля в исключениях и завершение SMTP после принятия письма.

## Приёмка Claude, 26 ноября 2026 — принято

1. Дифф прочитан целиком: `mail.py` (311 строк), `test_mail_extra.py`. Сверх задания учтён UIDVALIDITY (сервер пересоздал ящик — номера писем сбрасываются), атомарная запись курсора, блокировка файла, без повторной отправки при сбое на QUIT.
2. Контракт не менялся, `skip` и `xfail` нет.
3. Весь набор: 172 теста в ветке; после слияния с main — см. ниже.
4. Проверка на прочность: «старые UID не фильтруются», «нет повтора при обрыве», «нет References», «исходная ошибка с паролем уходит наружу» — все пойманы.
5. Секретов в коде нет, новых зависимостей нет (только стандартная библиотека).
6. Экрана нет.
7. Живая проверка: настоящий ящик Яндекса, только чтение — 2 письма, повторно 0, курсор сохранён.
