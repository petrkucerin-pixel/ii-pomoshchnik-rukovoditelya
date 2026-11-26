"""Стенд: веб-экран, проверка живости, Telegram-бот в фоне, план дня по расписанию.

Запуск: python -m pomoshnik.server  (порт PORT, по умолчанию 8080)
"""
from __future__ import annotations

import csv
import html
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from yadro.clock import TZ_EKB, SystemClock
from yadro.journal import Journal

from .assistant import Assistant
from .channels.telegram import TelegramError, TelegramPult, TelegramRateLimit
from .llm import from_env, load_dotenv
from .metrics import compute_metrics
from .profile import load_profile
from .protocol import ProtocolStore

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
TASK = ROOT / "data" / "task"
log = logging.getLogger("pomoshnik")


class DemoMailbox:
    """Учебный ящик из inbox.csv. Отправленное пишется в runtime/<профиль>_outbox.jsonl (заглушка Outlook)."""

    def __init__(self, csv_path: Path, out_path: Path):
        self.csv_path, self.out_path, self.given = csv_path, out_path, False

    def fetch_new(self):
        if self.given:
            return []
        self.given = True
        return [SimpleNamespace(id=r["id"], from_name=r["from_name"], from_email=r["from_email"],
                                subject=r["subject"], body=r["body"])
                for r in csv.DictReader(open(self.csv_path, encoding="utf-8"))]

    def send(self, to, subject, body, in_reply_to=None):
        rec = {"to": to, "subject": subject, "body": body, "in_reply_to": in_reply_to,
               "at": datetime.now(TZ_EKB).isoformat()}
        with open(self.out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return f"<stub-{time.time_ns()}@outlook.stub>"


def _bind_chat(token: str) -> str:
    """Чат руководителя: из .env, из runtime/chat.json или по первому /start в личном чате."""
    if os.environ.get("TELEGRAM_CHAT_ID"):
        return os.environ["TELEGRAM_CHAT_ID"]
    f = RUNTIME / "chat.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))["chat_id"]
    offset = 0
    with httpx.Client(timeout=40) as c:
        while True:
            try:
                r = c.post(f"https://api.telegram.org/bot{token}/getUpdates", json={"offset": offset, "timeout": 25}).json()
            except httpx.HTTPError:
                time.sleep(5)
                continue
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                if m.get("chat", {}).get("type") == "private" and (m.get("text") or "").startswith("/start"):
                    cid = str(m["chat"]["id"])
                    f.write_text(json.dumps({"chat_id": cid}), encoding="utf-8")
                    c.post(f"https://api.telegram.org/bot{token}/getUpdates", json={"offset": offset, "timeout": 0})
                    return cid
            if not r.get("ok"):
                time.sleep(10)


class Stand:
    def __init__(self):
        load_dotenv(str(ROOT / ".env"))
        RUNTIME.mkdir(exist_ok=True)
        self.clock = SystemClock()
        self.profile_id = os.environ.get("PROFILE", "ivan_petrov")
        pdir = ROOT / "profiles" / self.profile_id
        self.journal = Journal(RUNTIME / f"{self.profile_id}.sqlite", self.clock)
        self.protocols = ProtocolStore(RUNTIME / f"{self.profile_id}_protocols.sqlite", self.clock)
        self.llm = from_env(on_usage=lambda u: self.journal.log_llm_call(
            "openai-compatible", u.model, u.input_tokens, u.output_tokens, u.cost_usd, u.duration_ms))
        self.profile = load_profile(pdir, org_path=TASK / "org_structure.csv")
        self.mail = DemoMailbox(TASK / "inbox.csv", RUNTIME / f"{self.profile_id}_outbox.jsonl")
        self.assistant: Assistant | None = None
        self.status = {"bot": "не запущен", "last_poll": None, "error": ""}

    def start_bot(self):
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            self.status["bot"] = "нет токена"
            return
        threading.Thread(target=self._bot_loop, args=(token,), daemon=True).start()

    def _bot_loop(self, token: str):
        self.status["bot"] = "ждёт /start руководителя"
        chat = _bind_chat(token)
        pult = TelegramPult(token, chat)
        self.assistant = Assistant(self.profile, self.llm, pult, self.mail, self.journal, self.protocols,
                                   calendar_path=TASK / "calendar.ics", tasks_path=TASK / "tasks.csv")
        self.status["bot"] = "работает"
        last_plan_day = None
        while True:
            try:
                for u in pult.poll(timeout=25):
                    try:
                        self.assistant.handle(u)
                    except Exception as e:          # одна ошибка не должна ронять бота
                        log.exception("ошибка обработки")
                        self.status["error"] = f"{type(e).__name__}: {e}"[:200]
                        try:
                            pult.send_text("Не получилось выполнить действие, попробуйте ещё раз.")
                        except TelegramError:
                            pass
                self.status["last_poll"] = datetime.now(TZ_EKB).isoformat(timespec="seconds")
                now = datetime.now(TZ_EKB)
                if now.strftime("%H:%M") >= self.profile.plan_time and last_plan_day != now.date() and now.weekday() < 5:
                    last_plan_day = now.date()
                    self.assistant.morning_plan(now.date())
            except TelegramRateLimit as e:
                time.sleep(max(e.retry_after, 1))
            except TelegramError as e:
                self.status["error"] = str(e)[:200]
                time.sleep(5)


stand: Stand | None = None


@asynccontextmanager
async def _lifespan(_app):
    global stand
    stand = Stand()
    stand.start_bot()
    yield


app = FastAPI(title="ИИ-помощник руководителя", lifespan=_lifespan)


@app.get("/health")
def health():
    return {"ok": True, "bot": stand.status["bot"] if stand else "нет", "last_poll": stand.status["last_poll"] if stand else None}


@app.get("/api/metrics")
def api_metrics():
    return JSONResponse(compute_metrics(stand.journal, stand.protocols))


@app.post("/meeting")
def post_meeting(title: str = Form(...), transcript: str = Form(...), minutes_ago: int = Form(0)):
    if not stand.assistant:
        return JSONResponse({"error": "бот ещё не подключён к руководителю: напишите боту /start"}, status_code=409)
    end = datetime.now(TZ_EKB) - timedelta(minutes=minutes_ago)
    stand.assistant.new_meeting(transcript, title, end)
    return RedirectResponse("/", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index():
    m = compute_metrics(stand.journal, stand.protocols)
    e = html.escape
    acts = stand.journal.actions()[-25:][::-1]
    rows = "".join(f"<tr><td>{a['at'].strftime('%d.%m %H:%M')}</td><td>{e(a['actor'])}</td><td>{e(a['kind'])}</td>"
                   f"<td>{e(a['summary'][:120])}</td></tr>" for a in acts)
    cards = "".join(f"<div class=card><div class=v>{e(str(v))}</div><div class=k>{e(k)}</div></div>"
                    for k, v in m["cards"].items())
    return f"""<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>ИИ-помощник руководителя</title>
<style>
body{{font:15px system-ui,sans-serif;margin:0;background:#f4f6f8;color:#1b2a33}}
main{{max-width:960px;margin:auto;padding:16px}} h1{{font-size:20px}} h2{{font-size:16px;margin-top:24px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
.card{{background:#fff;border-radius:12px;padding:12px;box-shadow:0 1px 3px #0001}} .v{{font-size:22px;font-weight:700}} .k{{font-size:12px;color:#5a6b75}}
.tbl{{overflow-x:auto;background:#fff;border-radius:12px}} table{{border-collapse:collapse;width:100%;font-size:13px}}
td{{padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}}
textarea,input{{width:100%;box-sizing:border-box;font:inherit;padding:8px;border:1px solid #ccd;border-radius:8px}}
button{{min-height:44px;padding:0 18px;border:0;border-radius:10px;background:#245c73;color:#fff;font:inherit;margin-top:8px}}
</style><main>
<h1>ИИ-помощник руководителя · {e(stand.profile.name)}</h1>
<p>Бот: {e(stand.status['bot'])}. Я — ИИ-помощник, представляюсь так в переписке и на встречах.</p>
<div class=cards>{cards}</div>
<h2>Новая встреча → протокол на утверждение в Telegram</h2>
<form method=post action=/meeting><input name=title placeholder="Название встречи" required>
<textarea name=transcript rows=6 placeholder="Расшифровка встречи" required></textarea>
<button>Сделать протокол</button></form>
<h2>Журнал</h2><div class=tbl><table>{rows}</table></div>
</main></html>"""


def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
