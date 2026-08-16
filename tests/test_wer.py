"""Что должно работать:

1. дословное совпадение — ноль ошибок
2. замена, вставка и удаление слова считаются одной ошибкой каждая
3. регистр, пунктуация и «ё» не считаются ошибками: для поиска и разметки
   они не важны, а провайдеры расставляют их по-разному
4. WER по корпусу складывает ошибки, а не усредняет проценты — иначе короткая
   фраза весила бы столько же, сколько минутная запись
"""

import pytest

from evals.stt.wer import corpus_wer, score

REFERENCE = "надо переписать ретривал на гибридный поиск"


def test_exact_match_has_no_errors() -> None:
    result = score(REFERENCE, REFERENCE)

    assert result.errors == 0
    assert result.wer == 0.0


@pytest.mark.parametrize(
    ("hypothesis", "errors"),
    [
        ("надо переписать ретривал на гибридный поиск", 0),
        ("надо переписать ретривал на гибридный запрос", 1),  # замена
        ("надо переписать ретривал на быстрый гибридный поиск", 1),  # вставка
        ("надо переписать ретривал гибридный поиск", 1),  # удаление
        ("совсем другие слова тут", 6),
    ],
)
def test_edits_are_counted(hypothesis: str, errors: int) -> None:
    assert score(REFERENCE, hypothesis).errors == errors


def test_case_punctuation_and_yo_are_not_errors() -> None:
    result = score("Всё ещё думаю про поиск.", "все еще думаю про поиск")

    assert result.errors == 0


def test_empty_hypothesis_is_a_complete_miss() -> None:
    assert score(REFERENCE, "").wer == 1.0


def test_corpus_wer_weighs_by_words_not_by_records() -> None:
    """Одна ошибка в двух словах и одна в двадцати — это не 27,5%, а 9,1%."""
    short = score("два слова", "два слово")
    long = score(" ".join(f"слово{i}" for i in range(20)), " ".join(f"слово{i}" for i in range(19)))

    assert corpus_wer([short, long]) == pytest.approx(2 / 22)
