"""Стенд: кабинет руководителя и его API, проверка живости, Telegram-бот в фоне, план дня по расписанию.

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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from yadro.clock import TZ_EKB, SystemClock
from yadro.journal import Journal

from . import cabinet
from .assistant import Assistant, meeting_brief, work_day
from .channels.mail import CsvMailbox, OutlookStub, YandexMailbox
from .channels.telegram import TelegramError, TelegramPult, TelegramRateLimit
from .llm import from_env, load_dotenv
from .metrics import compute_metrics
from .profile import load_profile
from .memory import Memory, collapse, period, topics
from .protocol import ProtocolStore, questions_from_previous
from .robot.manager import RobotManager, links_from_journal, set_link
from .studio import Autopilot, prepare_avatar
from .telemost import CREATE_URL, TelemostError, create_conference
from .templates import TemplateError, discuss as discuss_template, to_dict as template_dict

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
TASK = ROOT / "data" / "task"
DEMO = ROOT / "data" / "demo"
PROFILES = ROOT / "profiles"
STATIC = Path(__file__).resolve().parent / "static"
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


class CsvInboxes:
    """Учебная почта заказчика плюс наши демо-письма (спам) — одним ящиком."""

    def __init__(self, *paths: Path):
        self.boxes = [CsvMailbox(p) for p in paths if p.exists()]

    def fetch_new(self):
        return [e for b in self.boxes for e in b.fetch_new()]


def make_mailbox(profile_id: str) -> CombinedMailbox:
    stub = OutlookStub(CsvInboxes(TASK / "inbox.csv", DEMO / "spam.csv"), RUNTIME / f"{profile_id}_outlook_sent.jsonl")
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


class DeferredPult:
    """Telegram-пульт, который подключается позже — после /start руководителя. До этого сообщения не уходят."""

    def __init__(self):
        self.target: TelegramPult | None = None

    def send_text(self, text):
        return self.target.send_text(text) if self.target else 0

    def send_buttons(self, text, buttons):
        return self.target.send_buttons(text, buttons) if self.target else 0

    def answer_callback(self, cid, text=""):
        if self.target:
            self.target.answer_callback(cid, text)

    def download(self, file_id):
        if not self.target:
            raise RuntimeError("Telegram не подключён")
        return self.target.download(file_id)


class Stand:
    def __init__(self):
        load_dotenv(str(ROOT / ".env"))
        RUNTIME.mkdir(exist_ok=True)
        self.clock = SystemClock()
        self.profile_id = os.environ.get("PROFILE", "ivan_petrov")
        pdir = PROFILES / self.profile_id
        self.journal = Journal(RUNTIME / f"{self.profile_id}.sqlite", self.clock)
        self.protocols = ProtocolStore(RUNTIME / f"{self.profile_id}_protocols.sqlite", self.clock)
        self.memory = Memory(RUNTIME / f"{self.profile_id}_memory.sqlite", self.clock)
        self.llm = from_env(on_usage=lambda u: self.journal.log_llm_call(
            "openai-compatible", u.model, u.input_tokens, u.output_tokens, u.cost_usd, u.duration_ms))
        self.profile = load_profile(pdir, org_path=TASK / "org_structure.csv")
        self.mail = make_mailbox(self.profile_id)
        self.pult = DeferredPult()
        self.lock = threading.RLock()        # бот и кабинет работают с одним помощником
        self.assistant: Assistant = self._new_assistant()
        self.status = {"bot": "не запущен", "last_poll": None, "error": "", "autopilot": "не запущен"}
        self.robot = RobotManager(lambda: self.assistant, _avatar_client, self.lock,
                                  avatar_id=os.environ.get("LIVEAVATAR_AVATAR_ID", ""), shots_dir=RUNTIME)
        self.autopilot = Autopilot(lambda: self.assistant, _avatar_client, robot=self.robot,
                                   avatar_id=os.environ.get("LIVEAVATAR_AVATAR_ID", ""),
                                   base_url=os.environ.get("STAND_URL", "https://194-87-110-81.sslip.io"),
                                   categories={c.strip() for c in os.environ.get("AVATAR_CATEGORIES", "операционная").split(",")
                                               if c.strip()})

    def _new_assistant(self) -> Assistant:
        a = Assistant(self.profile, self.llm, self.pult, self.mail, self.journal, self.protocols,
                      calendar_path=TASK / "calendar.ics", tasks_path=TASK / "tasks.csv", memory=self.memory)
        a.recheck_queue()                   # правила профиля могли поменяться: очередь пересматривается по новым
        from .voice import LocalWhisper, available
        a.transcribe = LocalWhisper() if available() else None     # голос в Telegram (TASK_16)
        return a

    def start_preparer(self):
        """Фоном готовит карточки решений: черновик, совет, факты. Включается PREPARE_DECISIONS=1 в .env."""
        if os.environ.get("PREPARE_DECISIONS", "").lower() not in ("1", "true", "yes"):
            self.status["preparer"] = "выключен"
            return
        self.status["preparer"] = "работает"
        threading.Thread(target=self._preparer_loop, daemon=True).start()

    def _preparer_loop(self):
        while True:
            try:
                with self.lock:             # по одной карточке, чтобы кабинет не ждал всю очередь
                    cabinet.chase_overdue(self.assistant)     # просроченные задачи: помощник сам спрашивает исполнителя
                    n = self.assistant.prepare_decisions(limit=1)
                time.sleep(1 if n else 30)
            except Exception as e:          # сбой модели не должен ронять стенд
                log.exception("подготовка решений")
                self.status["preparer"] = f"ошибка: {type(e).__name__}"
                time.sleep(60)

    def start_autopilot(self):
        """Аватар сам по календарю (TASK_09): шаг раз в 20 секунд, если настроен LiveAvatar."""
        avatar_id = os.environ.get("LIVEAVATAR_AVATAR_ID", "")
        if not (avatar_id and os.environ.get("HEYGEN_LiveAvatar_API_KEY")):
            self.status["autopilot"] = "LiveAvatar не настроен"
            return
        self.status["autopilot"] = "работает"
        threading.Thread(target=self._autopilot_loop, daemon=True).start()

    def _autopilot_loop(self):
        while True:
            try:
                with self.lock:
                    self.autopilot.tick()
            except Exception as e:          # шаг автопилота не должен ронять стенд
                log.exception("автопилот аватара")
                self.status["autopilot"] = f"ошибка: {type(e).__name__}"
            time.sleep(20)

    def restart_assistant(self):
        """Как после перезапуска стенда: всё, что не в базах, теряется."""
        with self.lock:
            llm = self.assistant.llm
            self.assistant = self._new_assistant()
            self.assistant.llm = llm

    def close(self):
        for db in (self.journal, self.protocols, self.memory):
            db.close()

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
        self.pult.target = pult
        self.status["bot"] = "работает"
        while True:
            try:
                for u in pult.poll(timeout=25):
                    try:
                        if u.kind == "voice":           # Whisper — вне замка: кабинет не ждёт распознавания
                            u = self.assistant.voice_to_text(u)
                        with self.lock:
                            self.assistant.handle(u)
                    except Exception as e:          # одна ошибка не должна ронять бота
                        log.exception("ошибка обработки")
                        self.status["error"] = f"{type(e).__name__}: {e}"[:200]
                        try:
                            pult.send_text("Не получилось выполнить действие, попробуйте ещё раз.")
                        except TelegramError:
                            pass
                self.status["last_poll"] = datetime.now(TZ_EKB).isoformat(timespec="seconds")
                with self.lock:                  # утро блоками: приветствие с решениями, график, сводка (TASK_15)
                    self.assistant.morning_tick()
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
    stand.start_autopilot()
    stand.start_preparer()
    yield


app = FastAPI(title="ИИ-помощник руководителя", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")


@app.middleware("http")
async def _revalidate(request, call_next):
    """Страница и статика кабинета: браузер сверяет версию по ETag при каждом открытии — новая версия видна сразу."""
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p == "/classic" or p.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/health")
def health():
    return {"ok": True, "bot": stand.status["bot"] if stand else "нет", "last_poll": stand.status["last_poll"] if stand else None}


@app.get("/api/metrics")
def api_metrics():
    return JSONResponse(compute_metrics(stand.journal, stand.protocols))


@app.post("/meeting")
def post_meeting(title: str = Form(...), transcript: str = Form(...), minutes_ago: int = Form(0)):
    end = datetime.now(TZ_EKB) - timedelta(minutes=minutes_ago)
    with stand.lock:
        stand.assistant.new_meeting(transcript, title, end)
    return RedirectResponse("/classic", status_code=303)


def _avatar_client():
    from .avatar import LiveAvatar
    key = os.environ.get("HEYGEN_LiveAvatar_API_KEY", "")
    return LiveAvatar(key) if key else None


@app.get("/avatar", response_class=HTMLResponse)
def avatar_page(title: str = "Еженедельная встреча команды", uid: str | None = None):
    la = _avatar_client()
    avatar_id = os.environ.get("LIVEAVATAR_AVATAR_ID", "")
    if not la or not avatar_id:
        return HTMLResponse("<p>Аватар не настроен: нет ключа LiveAvatar или id аватара.</p>", status_code=503)
    status = la.avatar_status(avatar_id)
    if status != "ACTIVE":
        return HTMLResponse(f"<p>Аватар ещё готовится: {html.escape(status)}. Обновите страницу через минуту.</p>",
                            status_code=503)
    with stand.lock:
        r = prepare_avatar(stand.assistant, la, avatar_id, title, uid)
    leader = stand.profile.name
    embed_id, url, questions, history_questions = r["embed_id"], r["url"], r["questions"], r["history_questions"]
    (RUNTIME / "avatar.json").write_text(json.dumps({"embed_id": embed_id, "title": title, "context_id": r["context_id"],
                                                     "meeting_uid": uid},
                                                    ensure_ascii=False), encoding="utf-8")
    stand.journal.log_action("avatar_meeting_ready",
                             summary=f"{title}: вопросов {len(questions)}, из истории {len(history_questions)}",
                             payload={"embed_id": embed_id, "questions": len(questions),
                                      "history_questions": len(history_questions), "history_chars": r["history_chars"]})
    e = html.escape
    qs = "".join(f"<li>{e(q)}</li>" for q in questions) or "<li>Утверждённого прошлого протокола нет.</li>"
    hqs = "".join(f"<li>{e(q)}</li>" for q in history_questions) or "<li>Открытых вопросов нет.</li>"
    # На http браузер не даёт встроенному окну доступ к микрофону и камере («Unable to load embed»).
    # Поэтому аватар открывается отдельным окном на своём https. Если стенд на https — окно встраивается в страницу.
    return f"""<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Встреча с ИИ-помощником</title>
<link rel="icon" href="/static/theme/img/logo.svg"><link rel="stylesheet" href="/static/theme/theme.css">
<style>.stage{{position:relative;width:100%;aspect-ratio:16/9;border-radius:22px;overflow:hidden;display:grid;place-items:center;
text-align:center;padding:24px;background:linear-gradient(135deg,#c9d8f2,#7f9fdc);border:1px solid var(--glass-border);box-shadow:var(--shadow)}}
.stage img{{width:min(46%,340px)}} .stage iframe{{position:absolute;inset:0;width:100%;height:100%;border:0}}
.stage .t-btn{{margin-top:14px}} ol{{margin:6px 0 0;padding-left:1.2em}} li{{margin:5px 0}}
@media (max-width:600px){{.stage{{aspect-ratio:auto;min-height:340px}}}}</style>
<div class="t-page">
<div class="t-kicker">Встреча с ИИ-помощником · {e(leader)}</div>
<h1 style="font-size:clamp(24px,4vw,40px);margin:6px 0 16px">{e(title)}</h1>
<div class="stage" id="stage" data-src="{e(url)}">
  <div><img src="/static/theme/img/hero.svg" alt="">
  <div><a class="t-btn t-btn--coral" href="{e(url)}" target="avatar" rel="noopener">Открыть аватара и начать встречу</a></div>
  <p class="t-small" style="color:var(--ink);max-width:560px;margin:12px auto 0">Аватар откроется в отдельном окне: на этом адресе
  браузер не даёт встроенному окну микрофон. Разговаривайте там, затем вернитесь сюда и нажмите «Встреча окончена».</p></div>
</div>
<form method=post action=/avatar/finish style="margin-top:16px"><button class="t-btn">Встреча окончена — протокол на утверждение в Telegram</button></form>
<div class="t-grid" style="margin-top:18px">
  <section class="t-card t-span-6"><header><h2>Вопросы по прошлому протоколу</h2></header><ol>{qs}</ol></section>
  <section class="t-card t-span-6"><header><h2>Вопросы из истории</h2></header><ol>{hqs}</ol></section>
</div>
<p class="t-footer"><a href="/#meetings">← к совещаниям</a> · аватар представляется ИИ-помощником</p>
</div>
<script>
if (window.isSecureContext) {{
  var stage = document.getElementById("stage"), frame = document.createElement("iframe");
  frame.src = stage.dataset.src;
  frame.allow = "camera; microphone; autoplay; fullscreen";
  stage.replaceChildren(frame);
}}
</script></html>"""


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
    with stand.lock:
        stand.assistant.new_meeting(text, state["title"], end, meeting_uid=state.get("meeting_uid"))
    return RedirectResponse("/#meetings", status_code=303)


# ---------------- новая встреча из кабинета (TASK_10) ----------------
class MeetingIn(BaseModel):
    title: str
    start: str                       # "YYYY-MM-DDTHH:MM", время Екатеринбурга
    minutes: int = 30
    category: str = "операционная"
    attendees: list[str] = []
    emails: list[str] = []
    link: str = ""
    avatar: bool = False


@app.get("/api/people")
def api_people():
    org = stand.assistant.p.org
    people = [p for p in (org.people if org else []) if p.role != "руководитель"]
    return {"people": [{"name": p.name, "position": p.position, "email": org.email_of(p)} for p in people]}


@app.post("/api/meetings")
def api_meeting_create(body: MeetingIn):
    import re
    a = stand.assistant
    title = body.title.strip()
    if not title:
        raise HTTPException(422, "нужно название встречи")
    try:
        start = datetime.fromisoformat(body.start).replace(tzinfo=TZ_EKB)
    except ValueError:
        raise HTTPException(422, "время встречи в формате 2026-12-11T17:00") from None
    if not 5 <= body.minutes <= 480:
        raise HTTPException(422, "длительность от 5 минут до 8 часов")
    link = body.link.strip()
    if link and not link.startswith("https://"):
        raise HTTPException(422, "ссылка на встречу должна начинаться с https://")
    names = {p.name for p in a.p.org.people} if a.p.org else set()
    unknown = [n for n in body.attendees if n not in names]
    if unknown:
        raise HTTPException(422, "нет в оргструктуре: " + ", ".join(unknown))
    bad = [e for e in body.emails if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e.strip())]
    if bad:
        raise HTTPException(422, "не похоже на адрес почты: " + ", ".join(bad))
    with stand.lock:
        m = a.create_meeting(title=title, start=start, minutes=body.minutes, category=body.category.strip() or "операционная",
                             attendees=body.attendees, emails=[e.strip() for e in body.emails], link=link,
                             avatar=body.avatar)
        return cabinet.meeting_short(m, a)


# ---------------- ссылка Телемоста: вручную или автоматически с токеном Яндекс 360 ----------------
@app.get("/api/telemost")
def api_telemost():
    return {"auto": bool(os.environ.get("TELEMOST_OAUTH_TOKEN", "").strip()), "create_url": CREATE_URL}


@app.post("/api/telemost/conference")
def api_telemost_conference():
    token = os.environ.get("TELEMOST_OAUTH_TOKEN", "").strip()
    if not token:
        raise HTTPException(501, "Автосоздание выключено: впишите TELEMOST_OAUTH_TOKEN (Яндекс 360 для бизнеса) в .env")
    try:
        return {"link": create_conference(token)}
    except TelemostError as e:
        raise HTTPException(502, str(e)) from None


# ---------------- робот Телемоста (TASK_11) ----------------
class LinkIn(BaseModel):
    link: str


class RobotIn(BaseModel):
    link: str
    title: str = "Встреча с ИИ-помощником"
    uid: str | None = None
    minutes: int = 60


@app.put("/api/meetings/{uid}/link")
def api_meeting_link(uid: str, body: LinkIn):
    try:
        set_link(stand.journal, uid, body.link)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return {"ok": True}


class LeadIn(BaseModel):
    avatar: bool


@app.put("/api/meetings/{uid}/lead")
def api_meeting_lead(uid: str, body: LeadIn):
    """Кто ведёт встречу: руководитель сам или его ИИ-аватар (тогда робот зайдёт в Телемост сам)."""
    from .lead import set_lead
    if uid not in {m.uid for m in stand.assistant.meetings}:
        raise HTTPException(404, "нет такой встречи")
    set_lead(stand.journal, uid, body.avatar)
    return {"ok": True}


@app.get("/api/robot")
def api_robot(uid: str | None = None):
    """Все роботы (до ROBOT_MAX встреч одновременно) или робот одной встречи — с расшифровкой и черновиком протокола."""
    return stand.robot.status(uid)


@app.post("/api/robot/start")
def api_robot_start(body: RobotIn):
    if not stand.robot.avatar_id or not _avatar_client():
        raise HTTPException(503, "LiveAvatar не настроен")
    try:
        return stand.robot.start(link=body.link, title=body.title, uid=body.uid, minutes=max(5, min(body.minutes, 180)))
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from None


@app.post("/api/robot/stop")
def api_robot_stop(uid: str | None = None):
    return {"ok": stand.robot.stop(uid)}


# ---------------- студия и экран аватара (TASK_09) ----------------
@app.get("/api/studio")
def api_studio():
    with stand.lock:
        return stand.autopilot.state() | {"autopilot": stand.status.get("autopilot")}


_HEAD = """<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<link rel="icon" href="/static/theme/img/logo.svg"><link rel="stylesheet" href="/static/theme/theme.css">"""


@app.get("/studio", response_class=HTMLResponse)
def studio_page():
    return _HEAD + """<title>Студия аватара</title>
<div class="t-page">
<div class="t-kicker">Студия · компьютер, через который аватар выходит в Телемост</div>
<h1 style="font-size:clamp(26px,4vw,44px);margin:6px 0 16px">Аватар на встречах</h1>
<div class="t-grid">
<section class="t-card t-span-7"><header><h2 id="st-title">Ждём встречу</h2><span class="t-chip" id="st-chip">ожидание</span></header>
<p id="st-text" class="t-muted">Страница сама откроет аватара за минуту до встречи и закроет после неё.</p>
<div style="display:flex;flex-wrap:wrap;gap:10px;margin-top:12px">
<button class="t-btn t-btn--coral" id="st-open" hidden>Открыть аватара</button>
<a class="t-btn t-btn--ghost" id="st-screen" target="avatar-screen" hidden>Экран аватара</a></div>
<p class="t-error" id="st-warn" hidden>Браузер не дал открыть окно. Разрешите всплывающие окна для этого сайта и нажмите «Открыть аватара».</p>
</section>
<section class="t-card t-span-5"><header><h2>Сегодня</h2><span class="t-note" id="st-cats"></span></header><ul class="t-timeline" id="st-list"></ul></section>
<section class="t-card t-span-12"><header><h2>Как это работает в Телемосте</h2></header><ol class="t-steps">
<li><span class="t-num">01</span><span class="t-row-title">Разрешите всплывающие окна для стенда — один раз</span><span></span></li>
<li><span class="t-num">02</span><span class="t-row-title">В окне аватара выберите Russian и нажмите «Chat now» — так требует LiveAvatar</span><span></span></li>
<li><span class="t-num">03</span><span class="t-row-title">OBS захватывает окно аватара → виртуальная камера в Телемосте, звук через VB-Cable</span><span></span>
<span class="t-row-sub">Подробно — ТЕЛЕМОСТ.md. Видео аватара можно свернуть в «картинку в картинке» кнопкой браузера.</span></li>
<li><span class="t-num">04</span><span class="t-row-title">«Экран аватара» показывайте в Телемосте демонстрацией экрана; руководитель показывает свой экран сам</span><span></span></li>
<li><span class="t-num">05</span><span class="t-row-title">После встречи окно закроется само, протокол уйдёт руководителю в Telegram</span><span></span></li>
</ol></section>
</div></div>
<script>
let win = null, openedUrl = "";
const $ = id => document.getElementById(id);
function openAvatar(url) {
  win = window.open(url, "avatar", "width=640,height=400");
  openedUrl = url;
  $("st-warn").hidden = !!win;
}
$("st-open").addEventListener("click", () => openAvatar($("st-open").dataset.url));
async function poll() {
  try {
    const s = await (await fetch("/api/studio", {cache: "no-store"})).json();
    $("st-cats").textContent = "категории: " + s.categories.join(", ");
    const list = $("st-list");
    list.replaceChildren(...s.upcoming.map(m => {
      const li = document.createElement("li"), t = document.createElement("span"), n = document.createElement("span");
      t.className = "t-time"; t.textContent = m.start.slice(11, 16); n.textContent = m.title;
      li.append(t, n); return li;
    }));
    if (!s.upcoming.length) { const li = document.createElement("li"); li.style.display = "block"; li.textContent = "Встреч для аватара сегодня больше нет."; list.append(li); }
    const m = s.meeting;
    $("st-title").textContent = m ? m.title + " · " + m.start.slice(11, 16) + "–" + m.end.slice(11, 16) : "Ждём встречу";
    $("st-chip").textContent = {ready: s.open_now ? "аватар на встрече" : "аватар готов", done: "встреча закончилась", idle: "ожидание"}[s.state];
    $("st-text").textContent = s.state === "done" ? "Протокол собирается сам и уходит руководителю в Telegram." :
      s.state === "ready" ? "Аватар знает историю, вопросы ревизора и задачи участников." :
      "Страница сама откроет аватара за минуту до встречи и закроет после неё.";
    $("st-open").hidden = s.state !== "ready";
    $("st-open").dataset.url = s.url || "";
    $("st-screen").hidden = !s.screen_url;
    if (s.screen_url) $("st-screen").href = s.screen_url;
    if (s.open_now && s.url && s.url !== openedUrl) openAvatar(s.url);
    if (!s.open_now && win && !win.closed) { win.close(); win = null; openedUrl = ""; }
  } catch (e) { $("st-text").textContent = "Нет связи со стендом, повторяю…"; }
}
poll(); setInterval(poll, 10000);
</script></html>"""


@app.get("/avatar/screen", response_class=HTMLResponse)
def avatar_screen(uid: str):
    with stand.lock:
        card = cabinet.meeting(stand.assistant, uid)
    if card is None:
        raise HTTPException(404, "нет такой встречи")
    e = html.escape
    m = card["meeting"]
    li = lambda items, empty: "".join(f"<li>{e(x)}</li>" for x in items) or f"<li class=t-muted>{empty}</li>"
    tasks = [f"{t['task']} — {t['owner']}, до {t['due'][8:10]}.{t['due'][5:7]}" for t in card["tasks"]]
    people = ", ".join(a["name"] for a in m["attendees"])
    return _HEAD + f"""<title>Экран аватара · {e(m['title'])}</title><meta http-equiv="refresh" content="30">
<style>.screen{{aspect-ratio:16/9;max-width:1280px;margin:0 auto}} .screen ol{{margin:4px 0 0;padding-left:1.2em}} .screen li{{margin:6px 0}}</style>
<div class="t-page screen">
<section class="t-hero" style="min-height:0;padding:28px 36px">
<div class="t-hero-text"><div class="t-kicker">ИИ-помощник {e(stand.profile.name)} · {e(m['start'][11:16])}–{e(m['end'][11:16])}</div>
<h1 style="margin-top:8px;font-size:clamp(28px,4vw,48px)">{e(m['title'])}</h1><div class="t-rule"></div>
<div class="t-kicker">Участники: {e(people)}</div></div>
<img class="t-hero-art" src="/static/theme/img/chat.svg" alt="" style="width:min(22%,200px)"></section>
<div class="t-grid" style="margin-top:16px">
<section class="t-card t-span-4"><header><h2>Вопросы по прошлому протоколу</h2></header><ol>{li(card['previous_questions'], 'Утверждённого прошлого протокола нет.')}</ol></section>
<section class="t-card t-span-4"><header><h2>Вопросы из истории</h2></header><ol>{li(card['history_questions'], 'Открытых вопросов нет.')}</ol></section>
<section class="t-card t-span-4"><header><h2>Задачи участников</h2></header><ol>{li(tasks, 'Открытых задач нет.')}</ol></section>
</div></div></html>"""


def _protocol_questions() -> list[str]:
    pid = stand.protocols.last_approved()
    return questions_from_previous(stand.protocols.protocol(pid, stand.profile.org)) if pid else []


def _fact_rows(facts) -> str:
    e = html.escape
    return "".join(f"<tr><td>{e(period(g))}</td><td>{e(g[0].who)}</td><td>{e(g[-1].what)}<br>"
                   f"<i>«{e(g[0].quote)}»</i> <span class=k>{e(', '.join(x.source for x in g))}</span></td></tr>"
                   for g in collapse(facts))


def _memory_block(q: str) -> str:
    e = html.escape
    all_topics = topics(stand.memory)
    n = sum(len(fs) for fs in all_topics)
    head = (f"<h2>Память помощника</h2><p class=k>Фактов {n}, тем {len(all_topics)}. Каждый факт сверен с цитатой "
            f"из письма или протокола.</p><form method=get action=/classic><input name=q value=\"{e(q)}\" "
            f"placeholder=\"Что известно по… (например, Облако Плюс)\"><button>Найти</button></form>")
    if q.strip():
        facts = stand.memory.history(q) or stand.memory.search(q)
        body = (f"<div class=tbl><table>{_fact_rows(facts)}</table></div>" if facts
                else f"<p>В памяти ничего нет по «{e(q)}».</p>")
        return head + body
    if not all_topics:
        return head + "<p>Память пока пуста: помощник наполняет её при разборе почты и утверждении протоколов.</p>"
    items = "".join(f"<details><summary><b>{e(fs[-1].topic)}</b> <span class=k>{e(fs[-1].kind)} · "
                    f"{fs[-1].when:%d.%m}</span> — {e(fs[-1].what)}</summary>"
                    f"<div class=tbl><table>{_fact_rows(fs)}</table></div></details>" for fs in all_topics[:30])
    return head + items


@app.get("/", response_class=HTMLResponse)
def root(q: str = ""):
    page = STATIC / "cabinet" / "index.html"
    return FileResponse(page, media_type="text/html") if page.exists() else classic(q)


@app.get("/classic", response_class=HTMLResponse)
def classic(q: str = ""):
    m = compute_metrics(stand.journal, stand.protocols)
    e = html.escape
    _, history_questions = meeting_brief(stand.journal, None, work_day(stand.clock))
    aq = [*_protocol_questions(), *history_questions]
    avatar_q = "".join(f"<li>{e(x)}</li>" for x in aq) or "<li>Открытых вопросов нет.</li>"
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
details{{background:#fff;border-radius:12px;padding:10px 12px;margin:6px 0;box-shadow:0 1px 3px #0001}}
summary{{cursor:pointer;min-height:24px}} li{{margin:4px 0}}
</style><main>
<h1>ИИ-помощник руководителя · {e(stand.profile.name)}</h1>
<p><a href="/avatar">Встреча с аватаром →</a></p><p>Бот: {e(stand.status["bot"])}. Я — ИИ-помощник, представляюсь так в переписке и на встречах.</p>
<div class=cards>{cards}</div>
<h2>Что аватар спросит на встрече</h2><ol>{avatar_q}</ol>
<p class=k>Плюс вся история из памяти ниже: если участник поднимет тему, аватар напомнит, что и когда сообщали раньше.</p>
{_memory_block(q)}
<h2>Новая встреча → протокол на утверждение в Telegram</h2>
<form method=post action=/meeting><input name=title placeholder="Название встречи" required>
<textarea name=transcript rows=6 placeholder="Расшифровка встречи" required></textarea>
<button>Сделать протокол</button></form>
<h2>Журнал</h2><div class=tbl><table>{rows}</table></div>
</main></html>"""


# ---------------- API кабинета (TASK_07, контракт в TASK_08_КАБИНЕТ_GPT.md) ----------------
class DecisionIn(BaseModel):
    verdict: Literal["yes", "no", "later"]
    text: str | None = None          # ответ руководителя вместо черновика помощника


class TemplateIn(BaseModel):
    subject: str
    body: str


class DiscussIn(BaseModel):
    message: str
    subject: str
    body: str


class TranscriptIn(BaseModel):
    title: str
    transcript: str
    minutes_ago: int = 0
    meeting_uid: str | None = None


class EditIn(BaseModel):
    instruction: str


@app.get("/api/today")
def api_today():
    with stand.lock:
        return cabinet.today(stand.assistant, stand.protocols)


@app.post("/api/decisions/{event_id}")
def api_decide(event_id: int, d: DecisionIn):
    with stand.lock:
        if not stand.assistant.decide_letter(event_id, d.verdict, notify=False, text=d.text):
            raise HTTPException(404, "такого письма нет среди ждущих решения")
    return {"ok": True}


@app.get("/api/mail")
def api_mail():
    with stand.lock:
        return cabinet.mail(stand.assistant)


@app.get("/api/templates")
def api_templates():
    return {"templates": [template_dict(t) for t in stand.assistant.templates.all()]}


def _template(key: str):
    try:
        return stand.assistant.templates.get(key)
    except KeyError:
        raise HTTPException(404, "нет такого шаблона") from None


@app.put("/api/templates/{key}")
def api_template_save(key: str, t: TemplateIn):
    old = _template(key)
    with stand.lock:
        try:
            new = stand.assistant.templates.save(key, t.subject, t.body)
        except TemplateError as e:
            raise HTTPException(422, str(e)) from None
        last_edit = max((x["id"] for x in stand.journal.actions()
                         if x["kind"] == "template_edit" and x["payload"].get("key") == key), default=0)
        asks = [x["payload"]["message"] for x in stand.journal.actions()
                if x["kind"] == "template_discuss" and x["payload"].get("key") == key and x["id"] > last_edit]
        stand.journal.log_action("template_edit", actor="human", path="now",
                                 summary=asks[-1] if asks else "правка вручную",
                                 payload={"key": key, "title": old.title, "subject": t.subject, "body": t.body,
                                          "old_subject": old.subject, "old_body": old.body})
    return template_dict(new)


@app.post("/api/templates/{key}/discuss")
def api_template_discuss(key: str, d: DiscussIn):
    t = _template(key)
    res = discuss_template(stand.assistant.llm, t, d.message, d.subject, d.body)
    stand.journal.log_action("template_discuss", actor="human", summary=d.message[:200],
                             payload={"key": key, "message": d.message, "reply": res["reply"]})
    return res


# ---------------- как пишет руководитель (TASK_14): правила стиля вместо шаблонов ----------------
class TeachIn(BaseModel):
    message: str
    kind: str = "*"            # вид письма: auto_reply, delegate, decision, invite… или «*» — все
    audience: str = "*"        # внешний | руководство | команда | «*» — любому


@app.get("/api/style")
def api_style():
    with stand.lock:
        w = stand.assistant.writer
        w.learn_pending()                   # свежие правки руководителя — сразу в правила
        return w.overview()


@app.post("/api/style/teach")
def api_style_teach(d: TeachIn):
    if not d.message.strip():
        raise HTTPException(422, "напишите, как писать письма")
    with stand.lock:
        try:
            return stand.assistant.writer.teach(d.message, d.kind, d.audience)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None


@app.delete("/api/style/rules/{rule_id}")
def api_style_forget(rule_id: int):
    with stand.lock:
        if not stand.assistant.writer.forget(rule_id):
            raise HTTPException(404, "нет такого правила")
    return {"ok": True}


@app.get("/api/meetings")
def api_meetings(start: date | None = None, days: int = 7):
    with stand.lock:
        a = stand.assistant
        return cabinet.meetings(a, start or a.today() - timedelta(days=a.today().weekday()), max(1, min(days, 42)))


@app.get("/api/meetings/{uid}")
def api_meeting(uid: str):
    with stand.lock:
        m = cabinet.meeting(stand.assistant, uid)
    if m is None:
        raise HTTPException(404, "нет такой встречи")
    return m


@app.post("/api/meetings/transcript")
def api_transcript(t: TranscriptIn):
    end = datetime.now(TZ_EKB) - timedelta(minutes=t.minutes_ago)
    with stand.lock:
        pid = stand.assistant.new_meeting(t.transcript, t.title, end, meeting_uid=t.meeting_uid)
    return {"protocol_id": pid}


@app.get("/api/protocols")
def api_protocols():
    with stand.lock:
        return cabinet.protocols(stand.assistant)


def _draft(pid: int) -> dict:
    rec = stand.protocols.get(pid)
    if rec is None:
        raise HTTPException(404, "нет такого протокола")
    if rec["status"] != "черновик":
        raise HTTPException(409, f"протокол уже {rec['status']}")
    return rec


@app.post("/api/protocols/{pid}/approve")
def api_protocol_approve(pid: int):
    with stand.lock:
        _draft(pid)
        stand.assistant.approve_protocol(pid)
    return {"ok": True}


@app.post("/api/protocols/{pid}/reject")
def api_protocol_reject(pid: int):
    with stand.lock:
        _draft(pid)
        stand.protocols.reject(pid)
        stand.journal.log_action("protocol_rejected", actor="human", path="decision", payload={"protocol_id": pid})
    return {"ok": True}


@app.post("/api/protocols/{pid}/edit")
def api_protocol_edit(pid: int, e: EditIn):
    with stand.lock:
        _draft(pid)
        a = stand.assistant
        a.protocols.apply_edit(pid, e.instruction, a.llm, a.p.org)
        stand.journal.log_action("protocol_edit", actor="human", path="decision", summary=e.instruction,
                                 payload={"protocol_id": pid})
        return cabinet.protocol_dict(a, pid)


@app.get("/api/tasks")
def api_tasks():
    with stand.lock:
        return cabinet.tasks(stand.assistant)


class TaskActionIn(BaseModel):
    action: Literal["remind", "move", "done"]
    due: date | None = None


@app.get("/api/tasks/{task_id}")
def api_task(task_id: str):
    with stand.lock:
        page = cabinet.task_page(stand.assistant, task_id)
    if not page:
        raise HTTPException(404, "такой задачи нет")
    return page


@app.post("/api/tasks/{task_id}/action")
def api_task_action(task_id: str, x: TaskActionIn):
    if x.action == "move" and not x.due:
        raise HTTPException(422, "для переноса нужен новый срок")
    with stand.lock:
        a = stand.assistant
        found = next((t for t, _ in cabinet.all_tasks(a) if t.id == task_id), None)
        if not found:
            raise HTTPException(404, "такой задачи нет")
        try:
            a.task_action(task_id, found.task, found.owner, found.due, x.action, x.due)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return cabinet.task_page(a, task_id)


@app.get("/api/memory")
def api_memory(q: str = ""):
    with stand.lock:
        return cabinet.memory(stand.assistant, q)


@app.get("/api/edits")
def api_edits():
    with stand.lock:
        return cabinet.edits(stand.assistant)


@app.get("/api/profile")
def api_profile():
    return cabinet.profile(stand.assistant)


@app.get("/api/profile/journal")
def api_profile_journal(who: str = "", group: str = ""):
    if who not in ("", "human", "assistant") or (group and group not in cabinet.JOURNAL_GROUPS):
        raise HTTPException(422, "неизвестный фильтр")
    with stand.lock:
        return cabinet.journal_feed(stand.assistant, who=who, group=group)


@app.get("/api/profile/rules")
def api_profile_rules():
    with stand.lock:
        return cabinet.rules(stand.assistant)


@app.get("/api/journal")
def api_journal(limit: int = 50):
    with stand.lock:
        return cabinet.journal(stand.assistant, limit)


def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
