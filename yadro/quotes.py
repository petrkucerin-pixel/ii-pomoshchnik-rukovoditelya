"""Сверка цитаты с источником. Факт в сообщении не должен расходиться с тем, откуда он взят.

Сравниваем слова целиком. Регистр, ё/е, кавычки, тире, пробелы и знаки препинания не важны.
Пересказ, подмена числа и обрывок слова не проходят. Многоточие делит цитату на части по порядку.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_WORD = re.compile(r"\w+")
_ELLIPSIS = re.compile(r"…|\.\.\.")


@dataclass(frozen=True)
class QuoteCheck:
    quote: str
    found: bool
    start: int = -1
    end: int = -1
    reason: str = ""


def _norm(word: str) -> str:
    return word.lower().replace("ё", "е")


def _tokens(text: str) -> list[tuple[str, int, int]]:
    return [(_norm(m.group()), m.start(), m.end()) for m in _WORD.finditer(text)]


def check_quote(quote: str, source: str, min_words: int = 2) -> QuoteCheck:
    fragments = [[t for t, _, _ in _tokens(part)] for part in _ELLIPSIS.split(quote or "")]
    fragments = [f for f in fragments if f]
    total = sum(len(f) for f in fragments)
    if total == 0:
        return QuoteCheck(quote, False, reason="пустая цитата")
    if total < min_words:
        return QuoteCheck(quote, False, reason=f"слишком короткая цитата: меньше {min_words} слов")

    src = _tokens(source or "")
    words = [t for t, _, _ in src]
    pos, first, last = 0, None, None
    for frag in fragments:
        idx = _find(words, frag, pos)
        if idx is None:
            return QuoteCheck(quote, False, reason="цитата не найдена в источнике")
        if first is None:
            first = idx
        last = idx + len(frag) - 1
        pos = idx + len(frag)
    return QuoteCheck(quote, True, src[first][1], src[last][2])


def check_quotes(quotes: list[str], source: str, min_words: int = 2) -> list[QuoteCheck]:
    return [check_quote(q, source, min_words) for q in quotes]


def _find(words: list[str], frag: list[str], start: int) -> int | None:
    n = len(frag)
    for i in range(start, len(words) - n + 1):
        if words[i:i + n] == frag:
            return i
    return None
