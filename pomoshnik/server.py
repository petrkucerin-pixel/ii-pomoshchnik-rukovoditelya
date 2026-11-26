"""Стенд: веб-экран, проверка живости, Telegram-бот в фоне, план дня по расписанию.

Запуск: python -m pomoshnik.server  (порт PORT, по умолчанию 8080)
"""
from __future__ import annotations

import html
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from yadro.clock import TZ_EKB, SystemClock
from yadro.journal import Journal

from .assistant import Assistant
from .channels.mail import CsvMailbox, OutlookStub, YandexMailbox
from .channels.telegram import TelegramError, TelegramPult, TelegramRateLimit
from .llm import from_env, load_dotenv
from .metrics import compute_metrics
from .profile import load_profile
from .protocol import ProtocolStore

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
TASK = ROOT / "data" / "task"
log = logging.getLogger("pomoshnik")


class CombinedMailbox:
    """Учебные письма заказчика (заглушка Outlook) + настоящий ящик Яндекса.

    Вымышленные адреса *.example уходят в заглушку Outlook, настоящие — через Яндекс.
    """

    def __init__(self, stub: OutlookStub, real: YandexMailbox | None):
        self.stub, self.real = stub, real
        self.last_error = ""

    def fetch_new(self):
        letters = self.stub.fetch_new()
        if self.real:
            try:
                letters += self.real.fetch_new()
            except RuntimeError as e:          # сбой Яндекса не должен останавливать разбор учебной почты
                self.last_error = str(e)
        return letters

    def send(self, to, subject, body, in_reply_to=None):
        if self.real and not to.lower().endswith(".example"):
            return self.real.send(to, subject, body, in_reply_to=in_reply_to)
        return self.stub.send(to, subject, body, in_reply_to=in_reply_to)


def make_mailbox(profile_id: str) -> CombinedMailbox:
    stub = OutlookStub(CsvMailbox(TASK / "inbox.csv"), RUNTIME / f"{profile_id}_outlook_sent.jsonl")
    real = None
    if os.environ.get("MAIL_PROVIDER") == "yandex" and os.environ.get("YANDEX_APP_PASSWORD"):
        real = YandexMailbox(os.environ["YANDEX_ADDRESS"], os.environ["YANDEX_APP_PASSWORD"].replace(" ", ""),
                             state_path=RUNTIME / f"{profile_id}_imap.json")
    return CombinedMailbox(stub, real)


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
        self.mail = make_mailbox(self.profile_id)
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


def _avatar_client():
    from .avatar import LiveAvatar
    key = os.environ.get("HEYGEN_LiveAvatar_API_KEY", "")
    return LiveAvatar(key) if key else None


@app.get("/avatar", response_class=HTMLResponse)
def avatar_page(title: str = "Еженедельная встреча команды"):
    from .avatar import build_context_prompt, opening_text
    la = _avatar_client()
    avatar_id = os.environ.get("LIVEAVATAR_AVATAR_ID", "")
    if not la or not avatar_id:
        return HTMLResponse("<p>Аватар не настроен: нет ключа LiveAvatar или id аватара.</p>", status_code=503)
    status = la.avatar_status(avatar_id)
    if status != "ACTIVE":
        return HTMLResponse(f"<p>Аватар ещё готовится: {html.escape(status)}. Обновите страницу через минуту.</p>",
                            status_code=503)
    a = stand.assistant
    questions = a.meeting_questions() if a else []
    tasks = [f"{t.task} — {t.owner}, до {t.due.strftime('%d.%m')}" for t in (a.tasks if a else [])]
    leader = stand.profile.name
    leader_gen = os.environ.get("LEADER_NAME_GENITIVE", "Ивана Петрова")
    ctx = la.create_context(f"{title} {datetime.now(TZ_EKB):%d.%m %H:%M}",
                            build_context_prompt(leader=leader_gen, title=title, questions=questions,
                                                 open_tasks=tasks, style=stand.profile.style),
                            opening_text(leader_gen))
    emb = la.create_embed(avatar_id, ctx, la.voice_id())
    embed_id = emb.get("id") or emb.get("embed_id")
    url = emb.get("url") or emb.get("embed_url") or ""
    (RUNTIME / "avatar.json").write_text(json.dumps({"embed_id": embed_id, "title": title, "context_id": ctx},
                                                    ensure_ascii=False), encoding="utf-8")
    stand.journal.log_action("avatar_meeting_ready", summary=f"{title}: вопросов {len(questions)}",
                             payload={"embed_id": embed_id, "questions": len(questions)})
    e = html.escape
    qs = "".join(f"<li>{e(q)}</li>" for q in questions) or "<li>прошлого протокола нет</li>"
    return f"""<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Встреча с ИИ-помощником</title>
<style>body{{font:15px system-ui,sans-serif;margin:0;background:#0f1a20;color:#e8eef1}} main{{max-width:1100px;margin:auto;padding:16px}}
.frame{{position:relative;width:100%;aspect-ratio:16/9;background:#000;border-radius:12px;overflow:hidden}}
iframe{{position:absolute;inset:0;width:100%;height:100%;border:0}}
button{{min-height:44px;padding:0 18px;border:0;border-radius:10px;background:#2f855a;color:#fff;font:inherit;margin-top:12px}}
a{{color:#9cd}}</style><main>
<h1 style="font-size:18px">{e(title)} · ИИ-помощник {e(leader)}</h1>
<div class=frame><iframe src="{e(url)}" allow="camera; microphone; autoplay; fullscreen"></iframe></div>
<form method=post action=/avatar/finish><button>Встреча окончена — протокол на утверждение в Telegram</button></form>
<h2 style="font-size:15px">Вопросы по прошлому протоколу</h2><ol>{qs}</ol>
<p><a href="/">← экран помощника</a></p></main></html>"""


@app.post("/avatar/finish")
def avatar_finish():
    from .avatar import end_time, latest_finished, transcript_to_text
    la = _avatar_client()
    state = json.loads((RUNTIME / "avatar.json").read_text(encoding="utf-8"))
    sessions = la.sessions(embed_id=state["embed_id"])
    s = latest_finished(sessions)
    if not s:
        active = la.sessions(embed_id=state["embed_id"], historic=False)
        s = active[0] if active else None
    if not s:
        return HTMLResponse("<p>Сессия аватара не найдена. Подождите 10–20 секунд после завершения и нажмите ещё раз.</p>",
                            status_code=404)
    text = transcript_to_text(la.transcript(s.id))
    if not text.strip():
        return HTMLResponse("<p>Расшифровка пустая: во встрече никто не говорил.</p>", status_code=422)
    end = end_time(s, TZ_EKB)
    stand.journal.log_action("avatar_meeting_done", summary=f"{state['title']}: {s.duration} с",
                             payload={"session_id": s.id, "duration": s.duration})
    if stand.assistant:
        stand.assistant.new_meeting(text, state["title"], end)
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
<p><a href="/avatar">Встреча с аватаром →</a></p><p>Бот: {e(stand.status["bot"])}. Я — ИИ-помощник, представляюсь так в переписке и на встречах.</p>
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
