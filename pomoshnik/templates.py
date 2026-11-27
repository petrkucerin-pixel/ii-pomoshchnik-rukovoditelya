"""Шаблоны писем помощника. Руководитель правит их в кабинете и обсуждает с помощником — без кода.

Шаблоны лежат в папке профиля (`templates.json`), у каждого руководителя свои. Переменные — `{имя}`.
Код проверяет, что в шаблоне только известные переменные: опечатка не уйдёт в письмо сотруднику.
"""
from __future__ import annotations

import json
import string
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Template:
    key: str
    title: str
    used_for: str
    subject: str
    body: str
    variables: tuple[str, ...]


DEFAULTS = {
    "delegate": Template("delegate", "Передача исполнителю", "письмо передано сотруднику по правилам профиля",
                         "Fwd: {тема}", "{имя}, возьмите, пожалуйста, в работу и сообщите итог.\n\n— {отправитель}: {текст}",
                         ("имя", "тема", "отправитель", "текст", "подпись")),
    "auto_reply": Template("auto_reply", "Автоответ", "письмо, которое помощник подтверждает сам",
                           "Re: {тема}", "Принято, подтверждаю.\n\n{подпись}", ("тема", "отправитель", "подпись")),
    "review_question": Template("review_question", "Вопрос ревизора", "ревизор нашёл проблему в отчёте",
                                "Вопрос по отчёту: {тема}", "{вопрос}\n\n{подпись}", ("тема", "вопрос", "имя", "подпись")),
    "protocol_mail": Template("protocol_mail", "Рассылка протокола", "руководитель утвердил протокол встречи",
                              "Протокол: {встреча}", "{протокол}", ("встреча", "протокол", "подпись")),
    "task_go": Template("task_go", "Можно начинать", "задачи, от которых зависела задача сотрудника, выполнены",
                        "Можно начинать: {задача}", "{имя}, {после} — готово. Можно начинать «{задача}», срок {срок}."
                        "\n\n{подпись}", ("имя", "задача", "после", "срок", "подпись")),
    "decision_yes": Template("decision_yes", "Согласовано", "руководитель согласовал письмо",
                             "Re: {тема}", "Согласовано.\n\n— {руководитель} (через ИИ-помощника)",
                             ("тема", "отправитель", "руководитель")),
    "decision_no": Template("decision_no", "Отказ", "руководитель отказал",
                            "Re: {тема}", "К сожалению, не можем согласовать.\n\n— {руководитель} (через ИИ-помощника)",
                            ("тема", "отправитель", "руководитель")),
    "assign": Template("assign", "Поручение", "руководитель поручил или попросил напомнить в Telegram",
                       "Поручение: {что}", "{имя}, {руководитель} просит: {что}.{срок}\n\n"
                       "Если что-то мешает — напишите.\n\n{подпись}", ("имя", "что", "срок", "руководитель", "подпись")),
    "invite": Template("invite", "Приглашение", "руководитель назначил встречу в кабинете",
                       "Приглашение: {встреча} — {когда}",
                       "Здравствуйте!\n\nПриглашаю на встречу «{встреча}» {когда}, {длительность} мин.\n"
                       "Ссылка на встречу: {ссылка}\n\n{подпись}",
                       ("встреча", "когда", "длительность", "ссылка", "руководитель", "подпись")),
}

DISCUSS_SYSTEM = """Ты помощник руководителя и вместе с ним правишь шаблон письма.
Переменные в фигурных скобках подставляет код, их можно переставлять, но нельзя придумывать новые.
Допустимые переменные: {variables}.
Верни JSON: {{"reply": "<что изменил, одной-двумя фразами>", "subject": "<тема>", "body": "<текст>"}}"""


class TemplateError(ValueError):
    pass


def fields_in(text: str) -> set[str]:
    try:
        return {f for _, f, _, _ in string.Formatter().parse(text) if f is not None}
    except ValueError as e:
        raise TemplateError(f"в шаблоне не закрыта фигурная скобка: {e}") from e


def unknown_fields(t: Template, subject: str, body: str) -> list[str]:
    return sorted((fields_in(subject) | fields_in(body)) - set(t.variables))


class Templates:
    def __init__(self, profile_dir: str | Path):
        self.path = Path(profile_dir) / "templates.json"

    def _saved(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {}

    def all(self) -> list[Template]:
        saved = self._saved()
        out = []
        for key, d in DEFAULTS.items():
            s = saved.get(key, {})
            out.append(Template(key, d.title, d.used_for, s.get("subject", d.subject), s.get("body", d.body), d.variables))
        return out

    def get(self, key: str) -> Template:
        for t in self.all():
            if t.key == key:
                return t
        raise KeyError(key)

    def save(self, key: str, subject: str, body: str) -> Template:
        t = self.get(key)
        bad = unknown_fields(t, subject, body)
        if bad:
            raise TemplateError("неизвестные переменные: " + ", ".join("{" + b + "}" for b in bad)
                                + ". Можно: " + ", ".join("{" + v + "}" for v in t.variables))
        saved = self._saved()
        saved[key] = {"subject": subject, "body": body}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)
        return self.get(key)

    def render(self, key: str, **values) -> tuple[str, str]:
        t = self.get(key)
        vals = {v: "" for v in t.variables} | {k: str(v) for k, v in values.items()}
        return t.subject.format_map(vals), t.body.format_map(vals)


def discuss(llm, t: Template, message: str, subject: str, body: str) -> dict:
    """Помощник предлагает новый вариант шаблона. Ничего не сохраняет: решает руководитель."""
    data = llm.complete_json(DISCUSS_SYSTEM.format(variables=", ".join("{" + v + "}" for v in t.variables)),
                             f"Шаблон «{t.title}» ({t.used_for}).\nТема: {subject}\nТекст:\n{body}\n\n"
                             f"Просьба руководителя: {message}")
    new_subject = str(data.get("subject") or subject)
    new_body = str(data.get("body") or body)
    try:
        problems = [f"неизвестная переменная {{{b}}}" for b in unknown_fields(t, new_subject, new_body)]
    except TemplateError as e:
        problems = [str(e)]
    return {"reply": str(data.get("reply") or ""), "subject": new_subject, "body": new_body, "problems": problems}


def to_dict(t: Template) -> dict:
    d = asdict(t)
    d["variables"] = list(t.variables)
    return d
