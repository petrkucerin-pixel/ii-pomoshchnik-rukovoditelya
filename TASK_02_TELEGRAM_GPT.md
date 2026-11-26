# TASK_02_GPT: Telegram-пульт руководителя

**Исполнитель:** Codex (GPT). **Приёмка:** Claude.
**Ветка:** `gpt/02-telegram`. Сливает Claude после приёмки.

## Зачем

Руководитель получает в Telegram план дня, решения с кнопками и протокол на утверждение. Агент работает на сервере и опрашивает Telegram (long polling, без вебхука). Этот модуль — только транспорт: отправить текст, отправить сообщение с кнопками, получить обновления. Логику «что отправлять» пишет Claude.

## Что создать

- `pomoshnik/__init__.py` (пустой, если его ещё нет)
- `pomoshnik/channels/__init__.py` (пустой)
- `pomoshnik/channels/telegram.py`

**Не трогать:** `yadro/`, `tests/yadro/`, `tests/test_telegram_contract.py` (это контракт, его пишет Claude), любые `*.md` кроме этого TASK, `ЖУРНАЛ_РАЗРАБОТЧИКА.md`, `СЕССИЯ.md`, `CLAUDE.md`. Свои дополнительные тесты можно класть в `tests/test_telegram_extra.py`.

## Интерфейс (стык)

```python
from pomoshnik.channels.telegram import TelegramPult, Update, Button

pult = TelegramPult(token: str, chat_id: int | str, *, client: httpx.Client | None = None,
                    api_base: str = "https://api.telegram.org")
pult.send_text(text: str) -> int                      # message_id, parse_mode HTML
pult.send_buttons(text: str, buttons: list[list[Button]]) -> int   # inline-клавиатура, ряды кнопок
pult.edit_text(message_id: int, text: str, buttons: list[list[Button]] | None = None) -> None
pult.answer_callback(callback_id: str, text: str = "") -> None
pult.poll(timeout: int = 25) -> list[Update]           # getUpdates c offset, offset хранится внутри

@dataclass(frozen=True)
class Button:
    label: str
    data: str          # callback_data, не длиннее 64 байт UTF-8, иначе ValueError

@dataclass(frozen=True)
class Update:
    update_id: int
    kind: str          # "text" | "callback" | "voice" | "other"
    chat_id: int | None
    text: str          # текст сообщения или callback data
    message_id: int | None
    callback_id: str | None = None
    file_id: str | None = None   # для голосового сообщения
```

Требования:
- Все запросы — через переданный `httpx.Client` (в тестах это `httpx.MockTransport`). Если клиент не передан, создать свой с таймаутом 35 с.
- Ответ `ok: false` → `TelegramError(description, error_code)`. Код 429 → `TelegramRateLimit(retry_after)`, наследник `TelegramError`.
- `poll()` после каждого вызова двигает offset на `max(update_id) + 1`. Повторный `poll()` не возвращает уже отданные обновления.
- Сообщения не из `chat_id` руководителя не пропускать дальше: `kind = "other"`.
- Текст длиннее 4096 символов режется на части: `send_text` отправляет несколько сообщений и возвращает id последнего.
- Токен нигде не логируется и не попадает в текст исключений.

## Как проверить

```
.venv/Scripts/python -m pytest tests/test_telegram_contract.py
.venv/Scripts/python -m pytest
```

Весь набор зелёный. Сеть в тестах не используется.

## Вне объёма

Вебхук, голос в текст, логика плана дня и решений, запуск бота как сервиса.

## Проверка Codex, 26 ноября 2026

Реализация — [TelegramPult](pomoshnik/channels/telegram.py). Пульт отправляет HTML-сообщения и клавиатуры, редактирует сообщения, отвечает на нажатия кнопок и получает обновления чата руководителя. При разбиении длинного текста сохраняются вложенные теги и HTML-сущности. При повторном опросе уже выданные обновления отбрасываются.

Из корня этой рабочей копии в PowerShell можно воспроизвести обе проверки.

```powershell
& '..\Кейс_3_ИИ_помощник_руководителя\.venv\Scripts\python' -m pytest tests/test_telegram_contract.py tests/test_telegram_extra.py tests/yadro
& '..\Кейс_3_ИИ_помощник_руководителя\.venv\Scripts\python' -m pytest
```

Первая команда прошла без сети, 131 тест. Вторая остановилась при сборе `tests/test_mail_contract.py`, поскольку в ветке `gpt/02-telegram` отсутствует модуль `pomoshnik.channels.mail` из TASK_03. Коммит отложен до успешного полного прогона.
