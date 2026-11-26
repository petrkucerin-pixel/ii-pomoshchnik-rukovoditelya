"""Профиль руководителя: текст leader_profile.md → настройки. Новый руководитель — новая папка, код не меняется."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .org import Org

ACTIONS = ("спросить_руководителя", "делегировать", "авто", "в_сводку", "игнорировать")

# Словарь категорий писем и слова, по которым их узнают в тексте правил профиля.
CATEGORIES = {
    "деньги": ["деньги", "бюджет", "скидк", "закупк"],
    "кадры": ["кадры", "найм", "оффер", "отпуск", "увольнен"],
    "обязательства_клиентам": ["обязательства клиентам", "гарантии"],
    "руководство": ["письма руководству", "руководству", "руководство"],
    "календарь": ["встреч", "календар"],
    "рассылка": ["рассыл"],
    "приглашение": ["приглашени"],
    "отчёт": ["отчёт", "отчет"],
    "статус": ["статус"],
    "продажи": ["кп", "коммерческ"],
    "клиенты": ["жалоб"],
    "доступы": ["доступ"],
    "спам": ["спам"],
}

ALWAYS_CONFIRM_DEFAULT = ("деньги", "кадры", "обязательства_клиентам", "руководство")


@dataclass
class Rule:
    action: str
    delegate_to: str | None = None     # id человека из оргструктуры
    source: str = ""                   # строка профиля, из которой правило


@dataclass
class Profile:
    id: str
    dir: Path
    text: str
    name: str = ""
    title: str = ""
    goals: list[str] = field(default_factory=list)
    confirm: set[str] = field(default_factory=set)      # категории только с подтверждением
    rules: dict[str, Rule] = field(default_factory=dict)
    plan_time: str = "08:30"
    week_time: str = "пятница 17:00"
    style: str = ""
    org: Org | None = None

    def action_for(self, category: str) -> Rule:
        if category in self.confirm:
            return Rule("спросить_руководителя", source="никогда без подтверждения")
        return self.rules.get(category, Rule("спросить_руководителя", source="правило профиля не задано"))

    def to_json(self) -> dict:
        return {"confirm": sorted(self.confirm),
                "rules": {k: {"action": r.action, "delegate_to": r.delegate_to, "source": r.source}
                          for k, r in sorted(self.rules.items())}}


def _sections(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    cur = "_"
    for line in text.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip().lower()
            out[cur] = []
        elif line.strip():
            out.setdefault(cur, []).append(line.strip())
    return out


def _find(sections: dict[str, list[str]], *words: str) -> list[str]:
    for title, lines in sections.items():
        if all(w in title for w in words):
            return lines
    return []


def _items(lines: list[str]) -> list[str]:
    return [re.sub(r"^([-*]|\d+\.)\s*", "", l) for l in lines if re.match(r"^([-*]|\d+\.)\s", l)]


def _categories_in(text: str, exclude: tuple[str, ...] = ()) -> list[str]:
    low = text.lower().replace("ё", "е")
    found = []
    for cat, words in CATEGORIES.items():
        if cat in exclude:
            continue
        if any(re.search(r"(?<!\w)" + re.escape(w.replace("ё", "е")), low) for w in words):
            found.append(cat)
    return found


def compile_rules_by_keywords(p: Profile, sections: dict[str, list[str]]) -> None:
    """Разбор правил по ключевым словам. Работает без модели."""
    for item in _items(_find(sections, "без подтверждения", "никогда")):
        head = re.split(r"[(:;]", item)[0]
        for cat in _categories_in(head):
            if cat in ("календарь", "приглашение", "рассылка", "отчёт", "статус"):
                continue
            p.confirm.add(cat)

    for item in _items(_find(sections, "можно без подтверждения")):
        low = item.lower()
        if "→" in item or "->" in item:                       # передача по оргструктуре: «КП → директор по продажам»
            inner = re.search(r"\(([^)]*)\)", item)
            pairs = inner.group(1) if inner else item
            for pair in re.split(r",\s*", pairs):
                if "→" not in pair and "->" not in pair:
                    continue
                left, right = re.split(r"→|->", pair, maxsplit=1)
                who = p.org.find_by_position(right.strip()) if p.org else None
                for cat in _categories_in(left):
                    if cat not in p.confirm:
                        p.rules[cat] = Rule("делегировать", who.id if who else None, item)
            continue
        if any(w in low for w in ("сводк",)):
            action = "в_сводку"
        elif any(w in low for w in ("отказ", "игнор")):
            action = "игнорировать"
        else:
            action = "авто"
        head = low
        if action == "игнорировать" and "неприоритетн" in low:
            # «отказ от рассылок и неприоритетных приглашений»: приглашение бывает приоритетным — решает руководитель
            head = low.replace("неприоритетных приглашений", "")
        for cat in _categories_in(head.split("—")[0] if action == "в_сводку" else head):
            if cat not in p.confirm and cat not in p.rules:
                p.rules[cat] = Rule(action, None, item)


RULES_SYSTEM = """Ты переводишь правила руководителя из текста в настройки разбора почты.
Верни JSON: {"rules": {"<категория>": {"action": "<действие>", "delegate_to": "<id человека или null>", "source": "<строка профиля>"}}}
Категории — только из списка. Действия — только из списка. delegate_to — только id из оргструктуры и только для действия «делегировать».
Не придумывай правило, если в тексте его нет: такие категории пропусти."""


def compile_rules_by_llm(p: Profile, llm) -> None:
    """Модель уточняет правила по тексту. Код отбрасывает всё, чего нет в словарях; запреты профиля важнее модели."""
    org_lines = "\n".join(f"{x.id}: {x.name}, {x.position}, {x.role}" for x in (p.org.people if p.org else []))
    user = (f"Категории: {', '.join(CATEGORIES)}\nДействия: {', '.join(ACTIONS)}\n"
            f"Оргструктура:\n{org_lines}\n\nПрофиль:\n{p.text}")
    data = llm.complete_json(RULES_SYSTEM, user)
    for cat, r in (data.get("rules") or {}).items():
        if cat not in CATEGORIES or cat in p.confirm or not isinstance(r, dict):
            continue
        action = r.get("action")
        if action not in ACTIONS:
            continue
        who = r.get("delegate_to")
        if action == "делегировать" and (not p.org or who not in p.org.by_id):
            continue
        if cat not in p.rules:                     # правило из текста по ключевым словам важнее догадки модели
            p.rules[cat] = Rule(action, who if action == "делегировать" else None, str(r.get("source", ""))[:200])


def load_profile(directory: str | Path, *, org_path: str | Path | None = None, llm=None) -> Profile:
    d = Path(directory)
    text = (d / "profile.md").read_text(encoding="utf-8")
    sections = _sections(text)
    first = next((l for l in text.splitlines() if l.strip() and not l.startswith("#")), "")
    name, _, title = first.partition("—")
    org_file = d / "org_structure.csv" if (d / "org_structure.csv").exists() else org_path
    p = Profile(id=d.name, dir=d, text=text, name=name.strip(), title=title.strip().split(".")[0],
                org=Org.load(org_file) if org_file else None)
    p.goals = _items(_find(sections, "цели"))
    p.style = " ".join(_find(sections, "стиль"))
    rhythm = " ".join(_find(sections, "каналы"))
    m = re.search(r"план дня в (\d{1,2}:\d{2})", rhythm)
    if m:
        h, mi = m.group(1).split(":")
        p.plan_time = f"{int(h):02d}:{mi}"
    m = re.search(r"итог недели в ([а-я]+ в \d{1,2}:\d{2})", rhythm)
    if m:
        p.week_time = m.group(1).replace(" в ", " ")
    compile_rules_by_keywords(p, sections)
    if not p.confirm:                              # профиль без раздела — безопасные запреты по умолчанию
        p.confirm = set(ALWAYS_CONFIRM_DEFAULT)
    if llm is not None:
        compile_rules_by_llm(p, llm)
    (d / "rules.json").write_text(json.dumps(p.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    return p
