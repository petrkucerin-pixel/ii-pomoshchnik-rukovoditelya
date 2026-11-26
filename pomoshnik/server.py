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
        self.status = {"bot": "не запущен", "last_poll": None, "error": ""}

    def _new_assistant(self) -> Assistant:
        return Assistant(self.profile, self.llm, self.pult, self.mail, self.journal, self.protocols,
                         calendar_path=TASK / "calendar.ics", tasks_path=TASK / "tasks.csv", memory=self.memory)

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
        last_plan_day = None
        while True:
            try:
                for u in pult.poll(timeout=25):
                    try:
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
                now = datetime.now(TZ_EKB)
                if now.strftime("%H:%M") >= self.profile.plan_time and last_plan_day != now.date() and now.weekday() < 5:
                    last_plan_day = now.date()
                    with self.lock:
                        self.assistant.morning_plan(self.assistant.today())
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
app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")


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
    questions = _protocol_questions()
    history, history_questions = meeting_brief(stand.journal, stand.memory, work_day(stand.clock))
    tasks = [f"{t.task} — {t.owner}, до {t.due.strftime('%d.%m')}" for t in a.tasks]
    leader = stand.profile.name
    leader_gen = os.environ.get("LEADER_NAME_GENITIVE", "Ивана Петрова")
    ctx = la.create_context(f"{title} {datetime.now(TZ_EKB):%d.%m %H:%M}",
                            build_context_prompt(leader=leader_gen, title=title, questions=questions,
                                                 open_tasks=tasks, style=stand.profile.style,
                                                 history=history, history_questions=history_questions),
                            opening_text(leader_gen))
    emb = la.create_embed(avatar_id, ctx, la.voice_id())
    embed_id = emb.get("id") or emb.get("embed_id")
    url = emb.get("url") or emb.get("embed_url") or ""
    (RUNTIME / "avatar.json").write_text(json.dumps({"embed_id": embed_id, "title": title, "context_id": ctx,
                                                     "meeting_uid": uid},
                                                    ensure_ascii=False), encoding="utf-8")
    stand.journal.log_action("avatar_meeting_ready",
                             summary=f"{title}: вопросов {len(questions)}, из истории {len(history_questions)}",
                             payload={"embed_id": embed_id, "questions": len(questions),
                                      "history_questions": len(history_questions), "history_chars": len(history)})
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
    with stand.lock:
        stand.assistant.new_meeting(text, state["title"], end, meeting_uid=state.get("meeting_uid"))
    return RedirectResponse("/#meetings", status_code=303)


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
        if not stand.assistant.decide_letter(event_id, d.verdict, notify=False):
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
