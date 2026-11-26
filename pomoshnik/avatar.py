"""Аватар руководителя на встрече: HeyGen LiveAvatar, режим FULL, встраиваемое окно.

Помощник перед встречей загружает в аватар контекст: кто он, правила, вопросы по прошлому протоколу,
открытые задачи. После встречи забирает расшифровку сессии и делает из неё протокол.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx

API = "https://api.liveavatar.com"
VOICE_DAVID = "09ca964c"   # префикс id голоса David — проверен Питом на русском 25.11


@dataclass(frozen=True)
class Session:
    id: str
    created_at: str
    end_at: str | None
    duration: int
    source: str


class LiveAvatarError(RuntimeError):
    pass


def build_context_prompt(*, leader: str, title: str, questions: list[str], open_tasks: list[str], style: str) -> str:
    q = "\n".join(f"{i}. {x}" for i, x in enumerate(questions, 1)) or "Прошлого протокола нет — начни с повестки."
    t = "\n".join(f"- {x}" for x in open_tasks[:15]) or "- открытых задач нет"
    return f"""Ты — ИИ-помощник руководителя {leader}. Ты участвуешь во встрече вместо него и всегда честно представляешься ИИ-помощником.
Говори по-русски, коротко: одна-две фразы за раз. Стиль руководителя: {style or 'по делу, с конкретным следующим шагом и сроком'}.

Встреча: {title}.
Твоя задача на встрече:
1. Поздоровайся и представься: «Я ИИ-помощник {leader}, веду встречу вместо него, протокол пришлю ему на утверждение».
2. Пройди по вопросам из прошлого протокола по очереди. Выслушай ответ, уточни срок и ответственного, если их не назвали.
3. Фиксируй новые договорённости: кто, что, к какому сроку. Переспроси, если срок или ответственный не ясны.
4. Не принимай решений про деньги, бюджеты, кадры, обязательства клиентам — скажи, что вынесешь их руководителю.
5. В конце коротко повтори договорённости и скажи, что протокол уйдёт на утверждение.

Вопросы по прошлому протоколу:
{q}

Открытые задачи команды:
{t}"""


def opening_text(leader: str) -> str:
    return f"Здравствуйте! Я ИИ-помощник {leader}. Веду встречу вместо него, протокол пришлю ему на утверждение. Начнём с прошлых договорённостей?"


def transcript_to_text(entries: list[dict], avatar_name: str = "ИИ-помощник") -> str:
    lines = []
    for e in sorted(entries, key=lambda x: x.get("relative_timestamp", 0)):
        text = (e.get("transcript") or "").strip()
        if not text:
            continue
        who = avatar_name if e.get("role") == "avatar" else "Участник"
        s = int(e.get("relative_timestamp", 0))
        lines.append(f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}\n{who}: {text}")
    return "\n".join(lines)


class LiveAvatar:
    def __init__(self, api_key: str, *, client: httpx.Client | None = None):
        self._h = {"X-API-KEY": api_key}
        self._c = client or httpx.Client(base_url=API, timeout=60)

    def _req(self, method: str, path: str, **kw) -> dict:
        try:
            r = self._c.request(method, path, headers=self._h, **kw)
        except httpx.HTTPError as e:
            raise LiveAvatarError(f"LiveAvatar недоступен ({type(e).__name__})") from None
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 400 or body.get("code") not in (None, 100, 1000):
            raise LiveAvatarError(f"LiveAvatar {method} {path}: {r.status_code} {str(body.get('message', ''))[:200]}")
        return body.get("data", body)

    def avatar_status(self, avatar_id: str) -> str:
        return self._req("GET", f"/v1/avatars/{avatar_id}")["status"]

    def voice_id(self, prefix: str = VOICE_DAVID) -> str:
        page = 1
        while page:
            d = self._req("GET", "/v1/voices", params={"page": page, "page_size": 100})
            for v in d.get("results", []):
                if v["id"].startswith(prefix):
                    return v["id"]
            page = page + 1 if d.get("next") else None
        raise LiveAvatarError("голос не найден")

    def create_context(self, name: str, prompt: str, opening: str) -> str:
        return self._req("POST", "/v1/contexts", json={"name": name[:64], "prompt": prompt, "opening_text": opening})["id"]

    def create_embed(self, avatar_id: str, context_id: str, voice_id: str, *, language: str = "ru",
                     max_seconds: int = 1200) -> dict:
        return self._req("POST", "/v2/embeddings", json={
            "avatar_id": avatar_id, "context_id": context_id, "voice_id": voice_id,
            "default_language": language, "max_session_duration": max_seconds})

    def sessions(self, *, embed_id: str | None = None, historic: bool = True) -> list[Session]:
        params = {"type": "historic" if historic else "active", "page_size": 20}
        if embed_id:
            params["embed_id"] = embed_id
        d = self._req("GET", "/v1/sessions", params=params)
        rows = d.get("results", d if isinstance(d, list) else [])
        return [Session(r["id"], r.get("created_at", ""), r.get("end_at"), int(r.get("duration") or 0),
                        r.get("source", "")) for r in rows]

    def transcript(self, session_id: str) -> list[dict]:
        out, ts = [], None
        for _ in range(50):
            params = {"start_timestamp": ts} if ts is not None else {}
            d = self._req("GET", f"/v1/sessions/{session_id}/transcript", params=params)
            out += d.get("transcript_data", [])
            nxt = d.get("next_timestamp")
            if not nxt or nxt == ts:
                break
            ts = nxt
        return out

    def credits(self) -> float:
        return float(self._req("GET", "/v1/users/credits")["credits_left"])


def latest_finished(sessions: list[Session]) -> Session | None:
    done = [s for s in sessions if s.end_at]
    return max(done, key=lambda s: s.end_at) if done else None


def end_time(s: Session, tz) -> datetime:
    return datetime.fromisoformat(s.end_at.replace("Z", "+00:00")).astimezone(tz) if s.end_at else datetime.now(tz)
