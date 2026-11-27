"""Как пишет руководитель (TASK_14). Шаблоны руководитель не составляет: помощник сам составляет себе тексты писем
в его стиле и учится на его правках.

- Правило относится к виду письма (автоответ, передача, ответ по решению…) и к адресату (клиенты и партнёры,
  руководство, команда). Решение Пита 27.11: «на все письма одного шаблона быть не может».
- Откуда правила: правки руководителя в карточке решения (журнал `decision_draft_edit`) и его слова
  («Переписка → Как я пишу → Научить помощника»).
- Когда правила меняются, помощник одним вызовом модели составляет тексты только тех писем, которых правила касаются.
- Код проверяет каждый текст: только известные переменные, обязательные на месте, ни одной своей цифры —
  цифры, даты, имена и ссылки подставляет код. Не прошёл проверку — остаётся прежний текст, причина видна на экране.
- Всё — в журнале продукта: правила, выученные правки, составленные тексты. Переживает перезапуск.
"""
from __future__ import annotations

import hashlib
import re

from .templates import DEFAULTS, TemplateError, Templates, fields_in

# без этих переменных письмо теряет смысл: ссылку, срок, исходное письмо, подпись подставляет только код
REQUIRED = {
    "delegate": {"имя", "текст"},
    "auto_reply": {"подпись"},
    "review_question": {"вопрос", "подпись"},
    "protocol_mail": {"протокол"},
    "task_go": {"имя", "задача", "срок", "подпись"},
    "decision_yes": {"руководитель"},
    "decision_no": {"руководитель"},
    "invite": {"встреча", "когда", "ссылка", "подпись"},
}
MAX_LEN = 900

# Виды писем. «*» — правило для всех писем.
KINDS = {"*": "Все письма", "auto_reply": "Автоответ", "delegate": "Передача исполнителю",
         "decision": "Ответ по решению руководителя", "invite": "Приглашение на встречу",
         "task_go": "Можно начинать", "review_question": "Вопрос ревизора", "protocol_mail": "Рассылка протокола"}
KIND_OF = {"decision_yes": "decision", "decision_no": "decision"}      # шаблон → вид письма
AUDIENCES = {"*": "любому адресату", "внешний": "клиентам и партнёрам", "руководство": "руководству",
             "команда": "команде и коллегам"}
REAL_AUDIENCES = ("внешний", "руководство", "команда")

# пример на экране: как звучит письмо на учебных данных
SAMPLE = {"имя": "Ольга", "тема": "Отчёт по продажам за ноябрь", "отправитель": "Анна Волкова",
          "текст": "Пришлите, пожалуйста, данные по возвратам до пятницы.", "вопрос": "В отчёте выручка 4,2 млн, "
          "а в CRM — 3,9 млн. Какая цифра верная?", "встреча": "Планёрка направления", "протокол": "(текст протокола)",
          "задача": "Обновить дашборд продаж", "после": "метрики", "срок": "12.12", "когда": "12.12 10:00",
          "длительность": "30", "ссылка": "https://telemost.yandex.ru/j/…",
          "подпись": "— ИИ-помощник", "руководитель": "Иван Петров"}

GEN_SYSTEM = """Ты ИИ-помощник руководителя и пишешь письма от его имени. Составь тексты писем в его стиле.
Правила:
- Это заготовки: всё, что меняется от письма к письму, пиши переменными в фигурных скобках из списка письма.
- Своих цифр, дат, сумм, имён и ссылок не пиши — их подставляет код через переменные.
- Обязательные переменные письма должны остаться в тексте.
- Учитывай правила руководителя для каждого письма и его адресата.
- Тему письма не пиши — только текст. Коротко, без канцелярита.
Верни JSON: {"letters": {"<ключ письма>": "<текст письма с переменными>"}}"""

LEARN_SYSTEM = """Руководитель поправил письмо, которое подготовил его ИИ-помощник.
Сравни черновик помощника и то, что руководитель отправил. Если правка про манеру письма (тон, длина, обращение,
приветствие, прощание, слова, которых руководитель избегает) — сформулируй одно короткое правило на будущее,
до 20 слов, обращаясь к помощнику: «Пиши…», «Не пиши…». Если правка только про факты этого случая (срок, сумма,
имя, решение) — правила нет.
Верни JSON: {"rule": "<правило>"} или {"rule": null}"""

TEACH_SYSTEM = """Руководитель говорит своему ИИ-помощнику, как писать письма от его имени.
Сформулируй из его слов одно-два коротких правила на будущее (до 20 слов каждое, обращаясь к помощнику: «Пиши…»).
Ответь руководителю одной фразой, что запомнил.
Верни JSON: {"rules": ["<правило>"], "reply": "<ответ руководителю>"}"""


def kind_of(key: str) -> str:
    return KIND_OF.get(key, key)


def vkey(key: str, audience: str | None) -> str:
    """Ключ составленного текста: шаблон или шаблон@адресат."""
    return key if audience in ("*", None) else f"{key}@{audience}"


def audience_of(org, email: str) -> str:
    """Кому письмо, по оргструктуре: нет в ней — клиент или партнёр."""
    p = org.find_by_email(email) if org and email else None
    if p is None:
        return "внешний"
    return "руководство" if p.role == "руководитель руководителя" else "команда"


def check_body(key: str, body) -> str:
    """Пустая строка — текст годится; иначе причина, почему код его не принял."""
    if not isinstance(body, str) or not body.strip():
        return "пустой текст"
    if len(body) > MAX_LEN:
        return f"слишком длинный текст ({len(body)} знаков)"
    try:
        fields = fields_in(body)
    except TemplateError as e:
        return str(e)
    unknown = sorted(fields - set(DEFAULTS[key].variables))
    if unknown:
        return "неизвестная переменная " + ", ".join("{" + u + "}" for u in unknown)
    missing = sorted(REQUIRED.get(key, set()) - fields)
    if missing:
        return "нет обязательной переменной " + ", ".join("{" + m + "}" for m in missing)
    if re.search(r"\d", re.sub(r"\{[^}]*\}", "", body)):
        return "своя цифра в тексте — цифры подставляет только код"
    return ""


class StyleBook:
    def __init__(self, llm, journal, templates: Templates, *, style: str = "", org=None):
        self.llm, self.j, self.templates, self.style, self.org = llm, journal, templates, style, org

    # ---------------- правила ----------------
    def rules(self, kind: str | None = None, audience: str | None = None) -> list[dict]:
        """Все правила; с kind/audience — только те, что действуют на письмо этого вида этому адресату."""
        out: dict[int, dict] = {}
        for a in self.j.actions():
            if a["kind"] == "style_rule_added":
                p = a["payload"]
                out[a["id"]] = {"id": a["id"], "text": p["text"], "source": p.get("source", ""),
                                "example": p.get("example"), "at": a["at"].isoformat(),
                                "kind": p.get("kind") or "*", "audience": p.get("audience") or "*"}
            elif a["kind"] == "style_rule_removed":
                out.pop(a["payload"].get("rule_id"), None)
        rules = list(out.values())
        if kind is not None:
            rules = [r for r in rules if r["kind"] in ("*", kind)]
        if audience is not None:
            rules = [r for r in rules if r["audience"] in ("*", audience)]
        return rules

    def rules_text(self, kind: str, audience: str | None = None) -> str:
        """Правила для письма вида kind («decision» — ответ по решению) адресату audience
        («внешний», «руководство», «команда»; None — только общие). Строки «- правило»; пусто — правил нет."""
        rules = [r for r in self.rules(kind, audience or "*") if audience or r["audience"] == "*"]
        return "\n".join(f"- {r['text']}" for r in rules)

    def version(self) -> str:
        raw = self.style + "\n" + "\n".join(f"{r['kind']}|{r['audience']}|{r['text']}" for r in self.rules())
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def _add(self, text: str, source: str, *, actor: str, kind: str = "*", audience: str = "*",
             example: dict | None = None) -> int:
        return self.j.log_action("style_rule_added", actor=actor, path="style",
                                 summary=f"{KINDS.get(kind, kind)} · {AUDIENCES.get(audience, audience)}: {text}",
                                 payload={"text": text, "source": source, "kind": kind, "audience": audience,
                                          "example": example})

    def teach(self, message: str, kind: str = "*", audience: str = "*") -> dict:
        if kind not in KINDS or audience not in AUDIENCES:
            raise ValueError("неизвестный вид письма или адресат")
        data = self.llm.complete_json(TEACH_SYSTEM, f"Письма: {KINDS[kind]}, {AUDIENCES[audience]}.\n"
                                                    f"Руководитель: {message.strip()}") or {}
        rules = [str(r).strip() for r in (data.get("rules") or []) if str(r).strip()][:3]
        for r in rules:
            self._add(r, "вы сказали", actor="human", kind=kind, audience=audience)
        if rules:
            self.refresh()
        return {"reply": str(data.get("reply") or ("Запомнил." if rules else "Не понял, что запомнить.")),
                "rules": rules}

    def forget(self, rule_id: int) -> bool:
        if not any(r["id"] == rule_id for r in self.rules()):
            return False
        self.j.log_action("style_rule_removed", actor="human", path="style", summary="правило забыто",
                          payload={"rule_id": rule_id})
        self.refresh()
        return True

    def learn_pending(self) -> int:
        """Правки руководителя в карточках решений, которые помощник ещё не разобрал. Возвращает, сколько разобрал.
        Правило — для вида «ответ по решению» и для адресата письма (по оргструктуре)."""
        acts = self.j.actions()
        done = {a["payload"].get("edit_id") for a in acts if a["kind"] == "style_learned"}
        todo = [a for a in acts if a["kind"] == "decision_draft_edit" and a["id"] not in done]
        added, n = False, 0
        for a in todo:
            draft, text = a["payload"].get("draft", ""), a["payload"].get("text", "")
            try:
                rule = (self.llm.complete_json(LEARN_SYSTEM, f"Черновик помощника:\n{draft}\n\n"
                                                             f"Руководитель отправил:\n{text}") or {}).get("rule")
            except Exception:                   # модель недоступна — разберём в следующий раз
                break
            rule = str(rule).strip() if rule else ""
            if rule:
                ev = self.j.event(a["event_id"]) if a.get("event_id") else None
                to = ((ev or {}).get("payload") or {}).get("from", "")
                self._add(rule, "ваша правка", actor="assistant", kind="decision",
                          audience=audience_of(self.org, to) if to else "*", example={"draft": draft, "text": text})
                added = True
            self.j.log_action("style_learned", path="style", summary=rule or "правка про факты, не про стиль",
                              payload={"edit_id": a["id"], "rule": rule or None})
            n += 1
        if added:
            self.refresh()
        return n

    # ---------------- тексты писем ----------------
    def _variants(self) -> list[tuple[str, str, list[dict]]]:
        """Что составить: (шаблон, адресат, правила). Только письма, которых правила касаются."""
        out = []
        for t in self.templates.all():
            if t.key == "protocol_mail":               # протокол уходит как есть
                continue
            k = kind_of(t.key)
            general = [r for r in self.rules(k) if r["audience"] == "*"]
            if general:
                out.append((t.key, "*", general))
            for aud in REAL_AUDIENCES:
                own = [r for r in self.rules(k) if r["audience"] == aud]
                if own:
                    out.append((t.key, aud, general + own))
        return out

    def refresh(self) -> dict:
        """Составить тексты писем под текущие правила одним вызовом модели. Без правил — стандартные тексты."""
        version, variants = self.version(), self._variants()
        if not variants:
            return {}
        ask = []
        for key, aud, rules in variants:
            t = self.templates.get(key)
            req = REQUIRED.get(key, set())
            ask.append(f"Ключ: {vkey(key, aud)} — {t.title} ({t.used_for}), адресат: {AUDIENCES[aud]}.\nПеременные: "
                       + ", ".join("{" + v + "}" for v in t.variables)
                       + ("\nОбязательные: " + ", ".join("{" + v + "}" for v in sorted(req)) if req else "")
                       + "\nПравила руководителя для этого письма:\n" + "\n".join(f"- {r['text']}" for r in rules)
                       + f"\nСейчас:\n{t.body}")
        user = f"Стиль руководителя: {self.style or 'по делу'}\n\nПисьма:\n\n" + "\n\n".join(ask)
        try:
            data = self.llm.complete_json(GEN_SYSTEM, user) or {}
        except Exception as e:                  # модель недоступна — письма уходят прежним текстом
            self.j.log_action("style_letters", path="style", summary="модель недоступна, тексты прежние",
                              payload={"version": version, "letters": {}, "problems": {"*": type(e).__name__}})
            return {}
        got = data.get("letters") or {}
        letters, problems = {}, {}
        for key, aud, _ in variants:
            vk = vkey(key, aud)
            why = check_body(key, got.get(vk)) if vk in got else "модель не прислала текст"
            if why:
                problems[vk] = why
            else:
                letters[vk] = got[vk].strip()
        self.j.log_action("style_letters", path="style",
                          summary=f"помощник переписал письма под ваши правила: {len(letters)}",
                          payload={"version": version, "letters": letters, "problems": problems})
        return letters

    def _current(self) -> dict:
        if not self.rules():
            return {"letters": {}, "problems": {}}
        version = self.version()
        for a in reversed(self.j.actions()):
            if a["kind"] == "style_letters" and a["payload"].get("version") == version:
                return a["payload"]
        return {"letters": {}, "problems": {}}

    @staticmethod
    def _body(key: str, audience: str | None, cur: dict) -> tuple[str | None, str]:
        """Свой текст под правила (для адресата, потом общий) или None — стандартный; и причина отказа кода."""
        tries = ([vkey(key, audience)] if audience not in ("*", None) else []) + [key]
        for vk in tries:
            if vk in cur["letters"]:
                return cur["letters"][vk], ""
        return None, next((cur["problems"][vk] for vk in [*tries, "*"] if vk in cur["problems"]), "")

    def render(self, key: str, audience: str | None = None, **values) -> tuple[str, str]:
        t = self.templates.get(key)
        vals = {v: "" for v in t.variables} | {k: str(v) for k, v in values.items()}
        body, _ = self._body(key, audience, self._current())
        return t.subject.format_map(vals), (body or t.body).format_map(vals)

    def overview(self) -> dict:
        """«Переписка → Как я пишу»: по видам писем — правила и как сейчас звучит письмо каждому адресату."""
        cur = self._current()
        kinds = []
        for k, title in KINDS.items():
            letters = []
            for t in self.templates.all():
                if k == "*" or kind_of(t.key) != k:
                    continue
                for aud in ("*", *REAL_AUDIENCES):
                    if aud != "*" and vkey(t.key, aud) not in cur["letters"] and vkey(t.key, aud) not in cur["problems"]:
                        continue                # своего текста для адресата нет — ему уходит общий
                    own, why = self._body(t.key, aud, cur)
                    letters.append({"key": t.key, "title": t.title, "used_for": t.used_for, "audience": aud,
                                    "source": "по вашим правилам" if own else "стандартный", "problem": why,
                                    "example": self.render(t.key, aud, **SAMPLE)[1]})
            kinds.append({"key": k, "title": title, "rules": [r for r in self.rules() if r["kind"] == k],
                          "letters": letters})
        return {"style": self.style, "rules": self.rules(), "kinds": kinds, "audiences": AUDIENCES}
