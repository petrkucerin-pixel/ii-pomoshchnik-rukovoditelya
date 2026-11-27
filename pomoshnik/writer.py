"""Как пишет руководитель (TASK_14). Шаблоны руководитель не составляет: помощник сам составляет себе тексты писем
в его стиле и учится на его правках.

- Правила стиля: из правок руководителя в карточке решения (журнал `decision_draft_edit`) и из его слов
  («Профиль → Как я пишу → Научить помощника»).
- Когда правила меняются, помощник одним вызовом модели составляет тексты всех писем с переменными в скобках.
- Код проверяет каждый текст: только известные переменные, обязательные на месте, ни одной своей цифры —
  цифры, даты, имена и ссылки подставляет код. Не прошёл проверку — остаётся прежний текст, причина видна в профиле.
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

# пример для профиля: как звучит письмо на учебных данных
SAMPLE = {"имя": "Ольга", "тема": "Отчёт по продажам за ноябрь", "отправитель": "Анна Волкова",
          "текст": "Пришлите, пожалуйста, данные по возвратам до пятницы.", "вопрос": "В отчёте выручка 4,2 млн, "
          "а в CRM — 3,9 млн. Какая цифра верная?", "встреча": "Планёрка направления", "протокол": "(текст протокола)",
          "задача": "Обновить дашборд продаж", "после": "метрики", "срок": "12.12", "когда": "12.12 10:00",
          "длительность": "30", "ссылка": "https://telemost.yandex.ru/j/…"}

GEN_SYSTEM = """Ты ИИ-помощник руководителя и пишешь письма от его имени. Составь тексты писем в его стиле.
Правила:
- Это заготовки: всё, что меняется от письма к письму, пиши переменными в фигурных скобках из списка письма.
- Своих цифр, дат, сумм, имён и ссылок не пиши — их подставляет код через переменные.
- Обязательные переменные письма должны остаться в тексте.
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


def check_body(key: str, body: str) -> str:
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
    def __init__(self, llm, journal, templates: Templates, *, style: str = ""):
        self.llm, self.j, self.templates, self.style = llm, journal, templates, style

    # ---------------- правила ----------------
    def rules(self) -> list[dict]:
        out: dict[int, dict] = {}
        for a in self.j.actions():
            if a["kind"] == "style_rule_added":
                p = a["payload"]
                out[a["id"]] = {"id": a["id"], "text": p["text"], "source": p.get("source", ""),
                                "example": p.get("example"), "at": a["at"].isoformat()}
            elif a["kind"] == "style_rule_removed":
                out.pop(a["payload"].get("rule_id"), None)
        return list(out.values())

    def rules_text(self) -> str:
        """Правила одной строкой — для черновиков решений и для составления писем."""
        return "\n".join(f"- {r['text']}" for r in self.rules())

    def version(self) -> str:
        raw = self.style + "\n" + "\n".join(r["text"] for r in self.rules())
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def _add(self, text: str, source: str, *, actor: str, example: dict | None = None) -> int:
        return self.j.log_action("style_rule_added", actor=actor, path="style", summary=text,
                                 payload={"text": text, "source": source, "example": example})

    def teach(self, message: str) -> dict:
        data = self.llm.complete_json(TEACH_SYSTEM, f"Руководитель: {message.strip()}") or {}
        rules = [str(r).strip() for r in (data.get("rules") or []) if str(r).strip()][:3]
        for r in rules:
            self._add(r, "вы сказали", actor="human")
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
        """Правки руководителя в карточках решений, которые помощник ещё не разобрал. Возвращает, сколько разобрал."""
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
                self._add(rule, "ваша правка", actor="assistant", example={"draft": draft, "text": text})
                added = True
            self.j.log_action("style_learned", path="style", summary=rule or "правка про факты, не про стиль",
                              payload={"edit_id": a["id"], "rule": rule or None})
            n += 1
        if added:
            self.refresh()
        return n

    # ---------------- тексты писем ----------------
    def refresh(self) -> dict:
        """Составить тексты всех писем под текущие правила. Без правил — стандартные тексты, модель не нужна."""
        version, rules = self.version(), self.rules()
        if not rules:
            return {}
        ask = []
        for t in self.templates.all():
            req = REQUIRED.get(t.key, set())
            ask.append(f"Ключ: {t.key} — {t.title} ({t.used_for}).\nПеременные: "
                       + ", ".join("{" + v + "}" for v in t.variables)
                       + ("\nОбязательные: " + ", ".join("{" + v + "}" for v in sorted(req)) if req else "")
                       + f"\nСейчас:\n{t.body}")
        user = (f"Стиль руководителя: {self.style or 'по делу'}\nПравила, которым научил руководитель:\n"
                f"{self.rules_text()}\n\nПисьма:\n\n" + "\n\n".join(ask))
        try:
            data = self.llm.complete_json(GEN_SYSTEM, user) or {}
        except Exception as e:                  # модель недоступна — письма уходят прежним текстом
            self.j.log_action("style_letters", path="style", summary="модель недоступна, тексты прежние",
                              payload={"version": version, "letters": {}, "problems": {"*": type(e).__name__}})
            return {}
        letters, problems = {}, {}
        for key, body in (data.get("letters") or {}).items():
            if key not in DEFAULTS or key == "protocol_mail":      # протокол уходит как есть
                continue
            why = check_body(key, body)
            if why:
                problems[key] = why
            else:
                letters[key] = body.strip()
        self.j.log_action("style_letters", path="style",
                          summary=f"помощник переписал письма под ваши правила: {len(letters)}",
                          payload={"version": version, "letters": letters, "problems": problems})
        return letters

    def _current(self) -> dict:
        version = self.version()
        for a in reversed(self.j.actions()):
            if a["kind"] == "style_letters" and a["payload"].get("version") == version:
                return a["payload"]
        return {"letters": {}, "problems": {}}

    def render(self, key: str, **values) -> tuple[str, str]:
        t = self.templates.get(key)
        vals = {v: "" for v in t.variables} | {k: str(v) for k, v in values.items()}
        body = self._current()["letters"].get(key) if self.rules() else None
        return t.subject.format_map(vals), (body or t.body).format_map(vals)

    def overview(self) -> dict:
        cur = self._current() if self.rules() else {"letters": {}, "problems": {}}
        letters = []
        for t in self.templates.all():
            own = cur["letters"].get(t.key)
            letters.append({"key": t.key, "title": t.title, "used_for": t.used_for,
                            "source": "по вашим правилам" if own else "стандартный",
                            "problem": cur["problems"].get(t.key) or cur["problems"].get("*") or "",
                            "example": self.render(t.key, **SAMPLE, подпись="— ИИ-помощник",
                                                   руководитель="Иван Петров")[1]})
        return {"style": self.style, "rules": self.rules(), "letters": letters}
