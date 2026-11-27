"""Робот для Телемоста (TASK_11): браузер без экрана заходит во встречу гостем «ИИ-помощник»,
вместо камеры и микрофона — аватар LiveAvatar, вместо «ушей» аватара — звук встречи.

Встречу ведёт `run_meeting`: вход в Телемост, запуск аватара, расшифровка по ходу, выход по времени
или по команде. Протокол собирает стенд из расшифровки — как после обычной встречи.
Разведка входа без аватара: `python -m pomoshnik.robot --link <ссылка> --minutes 2 --no-avatar`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger("pomoshnik.robot")
HERE = Path(__file__).resolve().parent
VENDOR = HERE / "vendor"


def init_scripts() -> list[str]:
    events = (VENDOR / "events-3.3.0.js").read_text(encoding="utf-8")
    shim = "window['events$1'] = (() => { const module = {exports: {}}; const exports = module.exports;\n" \
           + events + "\nreturn module.exports; })();"
    return [shim, (VENDOR / "liveavatar-sdk-0.0.20.umd.js").read_text(encoding="utf-8"),
            (HERE / "bridge.js").read_text(encoding="utf-8")]


ALONE_AFTER_S = 120      # все ушли — робот выходит через 2 минуты
NOBODY_S = 15 * 60       # никто не пришёл за 15 минут — робот выходит

# опрос комнаты: состояние моста, расшифровка, имена на плитках и число участников с кнопки «Участники N»
_POLL = """() => {
  const btn = [...document.querySelectorAll('button')].find(b => (b.innerText || '').includes('Участники'));
  const m = btn && btn.innerText.match(/(\\d+)/);
  const names = [...document.querySelectorAll('[class*="TextName_"]')].map(e => e.textContent.trim());
  return {state: __bridge.state, log: __bridge.log, transcript: __bridge.transcript,
          count: m ? Number(m[1]) : null, names};
}"""


@dataclass
class RobotState:
    status: str = "ожидание"
    log: list[str] = field(default_factory=list)
    transcript: list[dict] = field(default_factory=list)
    participants: set[str] = field(default_factory=set)
    end_reason: str = ""
    stop: bool = False
    started: float = 0.0
    shots: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        self.log.append(line)
        log.info(msg)


def transcript_text(items: list[dict], started_ms: float) -> str:
    lines = []
    for x in items:
        s = max(0, int((x["t"] - started_ms) / 1000))
        who = "ИИ-помощник" if x["role"] == "avatar" else "Участник"
        lines.append(f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}\n{who}: {x['text']}")
    return "\n".join(lines)


async def _click_text(page, *texts: str, timeout: int = 3000) -> bool:
    for t in texts:
        loc = page.get_by_role("button", name=t)
        if await loc.count() == 0:
            loc = page.get_by_text(t, exact=True)
        if await loc.count():
            try:
                await loc.first.click(timeout=timeout)
                return True
            except Exception:       # кнопка могла исчезнуть между проверкой и нажатием
                continue
    return False


async def call_frame(page, timeout: float = 45):
    """Комната Телемоста живёт во вложенном фрейме private-join — ищем его."""
    end = time.time() + timeout
    while time.time() < end:
        for f in page.frames:
            if "/private-join/" in f.url or "/join/" in f.url:
                try:
                    if await f.evaluate("() => !!document.body && document.body.innerText.length > 0"):
                        return f
                except Exception:
                    pass
        await asyncio.sleep(1)
    raise TimeoutError("не найден фрейм комнаты Телемоста")


async def join_telemost(page, link: str, name: str, st: RobotState, shots_dir: Path | None = None):
    async def shot(tag):
        if shots_dir:
            p = shots_dir / f"robot_{tag}.png"
            await page.screenshot(path=str(p))
            st.shots.append(str(p))

    st.status = "вход в Телемост"
    await page.goto(link, wait_until="domcontentloaded", timeout=60000)
    room = await call_frame(page)
    join = room.get_by_text("Подключиться", exact=True).first
    await join.wait_for(timeout=45000)
    await shot("1_lobby")
    # имя на встрече: по умолчанию «Гость», нажатие открывает поле
    try:
        field_ = room.locator('[data-testid="orb-textinput-input"]').first
        await field_.fill(name, timeout=5000)
        st.note(f"имя на встрече: {name}")
    except Exception as e:
        st.note(f"имя не изменено ({type(e).__name__})")
    await page.wait_for_timeout(500)
    # камера и микрофон должны быть включены — Телемост получит аватара
    for label in ("Включить камеру", "Включить микрофон"):
        if await _click_text(room, label, timeout=2000):
            st.note(label.lower())
    await shot("2_ready")
    await join.click()
    await page.wait_for_timeout(6000)
    await shot("3_in_call")
    st.status = "во встрече"
    st.note("робот вошёл во встречу")
    return room


async def run_meeting(link: str, *, name: str, token: str | None, until: float, st: RobotState,
                      shots_dir: Path | None = None, headless: bool = True) -> list[dict]:
    """Ведёт встречу до `until` (unix-время) или до st.stop. Возвращает расшифровку."""
    from playwright.async_api import async_playwright
    st.started = time.time() * 1000
    async with async_playwright() as p:
        # канал chrome: в Google Chrome есть H.264, в котором LiveAvatar отдаёт видео; в Chromium его нет
        browser = await p.chromium.launch(headless=headless, channel="chrome", args=[
            "--autoplay-policy=no-user-gesture-required", "--use-fake-ui-for-media-stream",
            "--disable-features=WebRtcHideLocalIpsWithMdns"])
        # bypass_csp: политика страницы Телемоста не пускает запросы к LiveAvatar и LiveKit
        ctx = await browser.new_context(locale="ru-RU", viewport={"width": 1280, "height": 800}, bypass_csp=True,
                                        permissions=["camera", "microphone"])
        for script in init_scripts():
            await ctx.add_init_script(script)
        page = await ctx.new_page()
        page.on("console", lambda m: m.type == "error" and log.debug("console: %s", m.text[:200]))
        seen_log = 0
        try:
            room = await join_telemost(page, link, name, st, shots_dir)
            if token:
                st.status = "запуск аватара"
                await room.evaluate("t => { window.__bridge.start(t); }", token)
            joined_at, alone_since, others_seen = time.time(), None, False
            while not st.stop and time.time() < until:
                try:
                    b = await room.evaluate(_POLL)
                except Exception:              # комнаты больше нет: встречу завершили
                    st.end_reason = "встреча завершена"
                    st.note("комната Телемоста закрыта — встреча завершена")
                    break
                for line in b["log"][seen_log:]:
                    st.note("мост: " + line[9:])
                seen_log = len(b["log"])
                st.transcript = b["transcript"]          # копим по ходу: при обрыве расшифровка не теряется
                st.participants |= {n for n in b["names"] if n and n != name}
                st.status = {"live": "аватар во встрече", "error": "ошибка аватара", "ended": "аватар отключился"}.get(
                    b["state"], st.status)
                if b["state"] == "ended":
                    st.end_reason = "аватар отключился"
                    break
                now = time.time()
                count = b["count"] or 0
                if count > 1:
                    others_seen, alone_since = True, None
                elif alone_since is None:
                    alone_since = now
                if others_seen and alone_since and now - alone_since > ALONE_AFTER_S:
                    st.end_reason = "все участники ушли"
                    st.note("все участники ушли — робот выходит")
                    break
                if not others_seen and now - joined_at > NOBODY_S:
                    st.end_reason = "никто не пришёл"
                    st.note("на встречу никто не пришёл — робот выходит")
                    break
                await asyncio.sleep(3)
            else:
                st.end_reason = st.end_reason or ("остановлен" if st.stop else "время встречи вышло")
            if shots_dir:
                await page.screenshot(path=str(shots_dir / "robot_4_end.png"))
            try:
                await room.evaluate("() => __bridge.stop()")
                await _click_text(room, "Завершить звонок", "Покинуть встречу", "Выйти")
            except Exception:
                pass
            st.note(f"робот вышел из встречи: {st.end_reason}")
        except Exception as e:
            st.status = "ошибка"
            st.note(f"ошибка робота: {type(e).__name__}: {str(e)[:200]}")
            if shots_dir:
                try:
                    await page.screenshot(path=str(shots_dir / "robot_error.png"))
                except Exception:
                    pass
        finally:
            await browser.close()
    st.status = "завершено" if st.status != "ошибка" else st.status
    return st.transcript


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Робот для Телемоста: проверка входа")
    ap.add_argument("--link", required=True)
    ap.add_argument("--minutes", type=float, default=2)
    ap.add_argument("--name", default="ИИ-помощник Ивана Петрова")
    ap.add_argument("--no-avatar", action="store_true")
    ap.add_argument("--token", default=None)
    ap.add_argument("--shots", default="/tmp")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    st = RobotState()
    if not a.no_avatar and not a.token:              # проверочная сессия: короткий контекст из .env стенда
        import os
        from ..avatar import LiveAvatar, build_context_prompt, opening_text
        from ..llm import load_dotenv
        load_dotenv("/root/ii-pomoshnik/.env")
        la = LiveAvatar(os.environ["HEYGEN_LiveAvatar_API_KEY"])
        gen = os.environ.get("LEADER_NAME_GENITIVE") or "Ивана Петрова"
        ctx = la.create_context("Проверка робота Телемоста",
                                build_context_prompt(leader=gen, title="Проверка робота", questions=[], open_tasks=[],
                                                     style=""), opening_text(gen))
        t = la.create_session_token(os.environ["LIVEAVATAR_AVATAR_ID"], ctx, la.voice_id())
        a.token = t["session_token"]
        st.note(f"сессия аватара {t['session_id'][:8]}…")
    asyncio.run(run_meeting(a.link, name=a.name, token=None if a.no_avatar else a.token,
                            until=time.time() + a.minutes * 60, st=st, shots_dir=Path(a.shots)))
    print("\n".join(st.log))
    print("расшифровка:", len(st.transcript), "реплик")
    print(transcript_text(st.transcript[:12], st.started))


if __name__ == "__main__":
    main()
