"""Что должно работать:

1. запись короче порога — один чанк, текст не тронут
2. запись длиннее порога режется по предложениям, а не посередине мысли
3. ни один чанк не длиннее окна
4. склейка чанков покрывает исходный текст целиком, ничего не потеряно
5. соседние чанки перекрываются — мысль на стыке не теряется
6. предложение длиннее окна режется по границам слов, а не по буквам
7. срабатывание правила видно в логе: правило, которое молчит, не проверить

**Живой текст берётся из базы прямо в прогоне** и в репозиторий не попадает
(ADR-0002). Нет базы и нет кэша — тесты на живой речи скипаются; синтетические
проверки алгоритма при этом остаются, потому что им архив не нужен.

Разделение не косметическое. Алгоритм («ни один чанк не длиннее окна»)
проверяется на любом тексте. А вот то, что у голосовой записи нет абзацев,
что предложения — единственная граница и что она вообще существует, —
это утверждения про живую речь, и подтвердить их можно только живой речью.
"""

import logging
from itertools import pairwise

import pytest

import brain.core.retrieval.chunking as chunking
from tests.archive import Entry, entry

# Записи архива. Только номера: текст живёт в базе.
#
# 1 — 542 символа, короче порога, режется в один чанк.
SHORT = 1
# 31 — 196 секунд речи, 1958 символов. Первая в архиве запись, на которой
# правило резки вообще срабатывает.
LONG = 31

# Синтетический текст: проверяет алгоритм там, где живой архив не нужен.
# Предложения намеренно разной длины — как в речи.
SYNTHETIC = " ".join(
    f"Предложение номер {number} про то, как устроен поиск и что в нём важно."
    for number in range(80)
)


def test_live_long_entry_is_the_one_that_matters(archive: list[Entry]) -> None:
    """Оговорка вслух: без этой записи правило резки не проверялось бы вовсе."""
    text = entry(archive, LONG).text

    assert len(text) > chunking.MAX_SINGLE_CHUNK
    assert "\n" not in text, "у голосовых нет абзацев — резать можно только по предложениям"


# --- Короткая запись ---


def test_short_entry_becomes_single_chunk(archive: list[Entry]) -> None:
    text = entry(archive, SHORT).text

    assert chunking.split(text) == [text]


def test_short_entry_is_not_touched(archive: list[Entry]) -> None:
    """Текст короче порога не переписывается: чанк равен записи посимвольно."""
    text = entry(archive, SHORT).text

    assert chunking.split(text)[0] == text


def test_empty_text_gives_nothing() -> None:
    assert chunking.split("   ") == []


# --- Длинная запись ---


def test_long_entry_is_split(archive: list[Entry]) -> None:
    assert len(chunking.split(entry(archive, LONG).text)) > 1


def test_synthetic_long_text_is_split() -> None:
    """То же самое там, где архив не нужен: алгоритм от него не зависит."""
    assert len(chunking.split(SYNTHETIC)) > 1


@pytest.mark.parametrize("source", ["живая", "синтетическая"])
def test_no_chunk_exceeds_window(archive: list[Entry], source: str) -> None:
    """Окно — верхняя граница, а не пожелание: за ней эмбеддинг размывается."""
    text = entry(archive, LONG).text if source == "живая" else SYNTHETIC

    assert all(len(chunk) <= chunking.WINDOW for chunk in chunking.split(text))


@pytest.mark.parametrize("source", ["живая", "синтетическая"])
def test_every_sentence_survives(archive: list[Entry], source: str) -> None:
    """Ни одно предложение не потеряно на стыке — это и есть цена ошибки резки."""
    text = entry(archive, LONG).text if source == "живая" else SYNTHETIC
    chunks = chunking.split(text)

    for sentence in chunking.sentences(text):
        assert any(sentence in chunk for chunk in chunks), f"потеряно: {sentence!r}"


def test_sentences_keep_their_order(archive: list[Entry]) -> None:
    """Порядок сохраняется: чанк N не может начинаться раньше чанка N-1."""
    text = entry(archive, LONG).text
    chunks = chunking.split(text)
    starts = [text.index(chunk[:40]) for chunk in chunks]

    assert starts == sorted(starts)


@pytest.mark.parametrize("source", ["живая", "синтетическая"])
def test_neighbours_overlap(archive: list[Entry], source: str) -> None:
    """Соседние чанки делят хвост: мысль, попавшая на стык, есть в обоих."""
    text = entry(archive, LONG).text if source == "живая" else SYNTHETIC
    chunks = chunking.split(text)

    for previous, following in pairwise(chunks):
        head = following[:40]
        assert head in previous, "перекрытия нет — стык режет мысль пополам"


def test_boundaries_land_on_sentences(archive: list[Entry]) -> None:
    """Чанк кончается концом предложения, а не серединой слова."""
    for chunk in chunking.split(entry(archive, LONG).text):
        assert chunk.rstrip()[-1] in ".!?…"


def test_overlap_is_about_fifteen_percent() -> None:
    """Перекрытие держится около 15% окна — не ноль и не половина чанка."""
    chunks = chunking.split(SYNTHETIC)
    shares = [len(_shared_tail(previous, following)) for previous, following in pairwise(chunks)]

    assert shares, "не с чем сравнивать: текст не разрезался"
    average = sum(shares) / len(shares)
    assert 0.05 * chunking.WINDOW <= average <= 0.35 * chunking.WINDOW, (
        f"перекрытие {average:.0f} символов при окне {chunking.WINDOW}"
    )


def _shared_tail(previous: str, following: str) -> str:
    """Кусок, которым чанки перекрываются."""
    for size in range(min(len(previous), len(following)), 0, -1):
        if previous.endswith(following[:size]):
            return following[:size]
    return ""


# --- Речь без пунктуации ---


def test_sentence_longer_than_window_is_split_by_words() -> None:
    """Расшифровка может прийти вовсе без точек — тогда режем по словам.

    В архиве такого нет, но модель уже роняла окончания и съедала слоги;
    точку она может не поставить так же легко.
    """
    without_punctuation = " ".join(["слово"] * 700)

    chunks = chunking.split(without_punctuation)

    assert len(chunks) > 1
    assert all(len(chunk) <= chunking.WINDOW for chunk in chunks)
    assert all(not chunk.startswith("во ") for chunk in chunks), "слово разрезано пополам"


# --- Лог ---


def test_split_entry_logs_when_rule_fires(
    archive: list[Entry], caplog: pytest.LogCaptureFixture
) -> None:
    """Правило, которое ни разу не сработало, проверить нечем — пусть говорит."""
    with caplog.at_level(logging.INFO, logger="brain.core.retrieval.chunking"):
        chunking.split_entry(LONG, entry(archive, LONG).text)

    assert any(str(LONG) in record.message for record in caplog.records), (
        f"срабатывание резки не попало в лог: {[r.message for r in caplog.records]}"
    )


def test_split_entry_is_quiet_on_short_text(
    archive: list[Entry], caplog: pytest.LogCaptureFixture
) -> None:
    """Обычная запись в лог не пишет: иначе сообщение о резке в нём утонет."""
    with caplog.at_level(logging.INFO, logger="brain.core.retrieval.chunking"):
        chunking.split_entry(SHORT, entry(archive, SHORT).text)

    assert caplog.records == []


def test_split_entry_returns_the_same_chunks(archive: list[Entry]) -> None:
    """Лог — единственное отличие от `split`."""
    text = entry(archive, LONG).text

    assert chunking.split_entry(LONG, text) == chunking.split(text)
