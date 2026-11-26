import pytest

from yadro.quotes import check_quote, check_quotes

SOURCE = (
    "Коллеги, добрый день!\n"
    "Акция «Чёрная пятница» стартует 1.12 в 10:00 по Москве.\n"
    "Скидка  на весь каталог — 15 %, промокод BLACK15.\n"
    "Ещё раз: лендинг нужен до 27.11."
)


def test_exact_quote_found_with_position():
    r = check_quote("стартует 1.12 в 10:00", SOURCE)
    assert r.found
    assert SOURCE[r.start:r.end] == "стартует 1.12 в 10:00"


def test_case_yo_quotes_dashes_and_spaces_do_not_matter():
    assert check_quote('акция "черная пятница" СТАРТУЕТ', SOURCE).found
    assert check_quote("скидка на весь каталог - 15 %", SOURCE).found
    assert check_quote("еще раз лендинг нужен", SOURCE).found


def test_span_covers_original_text_including_punctuation():
    r = check_quote("акция черная пятница", SOURCE)
    assert SOURCE[r.start:r.end] == "Акция «Чёрная пятница"


def test_paraphrase_is_rejected():
    r = check_quote("акция начнётся 1.12", SOURCE)
    assert not r.found
    assert r.reason


def test_changed_number_is_rejected():
    assert not check_quote("скидка на весь каталог 20 %", SOURCE).found
    assert not check_quote("стартует 2.12", SOURCE).found


def test_partial_word_is_rejected():
    assert not check_quote("акция старт", SOURCE).found


def test_ellipsis_fragments_in_order():
    assert check_quote("Акция … 1.12", SOURCE).found
    assert check_quote("Акция ... промокод BLACK15", SOURCE).found
    assert not check_quote("промокод BLACK15 … Акция", SOURCE).found


def test_ellipsis_span_covers_all_fragments():
    r = check_quote("Акция … 1.12", SOURCE)
    assert SOURCE[r.start:r.end].startswith("Акция")
    assert SOURCE[r.start:r.end].endswith("1.12")


@pytest.mark.parametrize("q", ["", "   ", "…", "«»"])
def test_empty_quote(q):
    r = check_quote(q, SOURCE)
    assert not r.found
    assert "пуст" in r.reason


def test_too_short_quote_by_default():
    r = check_quote("Акция", SOURCE)
    assert not r.found
    assert "корот" in r.reason
    assert check_quote("Акция", SOURCE, min_words=1).found


def test_quote_across_line_break():
    assert check_quote("по Москве. Скидка на весь", SOURCE).found


def test_repeated_word_finds_first_full_match():
    src = "срок до пятницы. Повторяю: срок до понедельника"
    r = check_quote("срок до понедельника", src)
    assert r.found
    assert src[r.start:r.end] == "срок до понедельника"


def test_empty_source():
    assert not check_quote("акция стартует", "").found


def test_check_quotes_list():
    res = check_quotes(["стартует 1.12", "скидка 50 %"], SOURCE)
    assert [r.found for r in res] == [True, False]
