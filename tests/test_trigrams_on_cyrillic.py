"""Что должно работать: pg_trgm видит буквы в кириллице.

Написано по инциденту 18 августа 2026, найденному первым же прогоном eval'а.

`docker-compose.yml` создавал базу с локалью `C` — «ускоряет инициализацию
и держит сортировку предсказуемой». pg_trgm решает, что считать словом,
по `isalpha` из локали базы, и под `C` кириллица буквами не считается вовсе:

    show_trgm('кошка')                        → {}
    similarity('поразгоняли','порасгоняли')   → 0

То есть третий канал поиска из ADR-0006 — заведённый **ровно ради русских
словоформ и опечаток расшифровки** — не работал ни одной секунды. Заметить
это было нечем: «PGVector» латиницей находился прекрасно, тесты были зелёными,
выдача не пустела, потому что два других канала работали.

Под `C.utf8` те же вызовы дают шесть триграмм и 0.6. Сортировка остаётся
байтовой — меняется только определение «буквы».

Урок прежний и записан в `status.md` дважды: **границы с чужим миром надо
трогать, а не описывать по памяти.** Здесь чужим миром оказалась локаль
контейнера, про которую в compose стояла уверенная фраза про предсказуемость.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

# Расшифровка записи 31 содержит «порасгоняли» — модель услышала «с» вместо
# «з». Именно за такими случаями третий канал и заведён.
TYPO = ("поразгоняли", "порасгоняли")


async def _scalar(url: str, sql: str, params: dict[str, object] | None = None) -> object:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return await connection.scalar(text(sql), params or {})
    finally:
        await engine.dispose()


async def test_cyrillic_produces_trigrams(migrated_database: str) -> None:
    """Под локалью `C` здесь пустой массив, и весь канал молчит."""
    count = await _scalar(migrated_database, "SELECT array_length(show_trgm('кошка'), 1)")

    assert count, "у кириллицы нет триграмм — база создана с локалью C"


async def test_latin_still_works(migrated_database: str) -> None:
    """Проверка проверки: латиница работала и под `C`, поэтому дыру не было видно."""
    count = await _scalar(migrated_database, "SELECT array_length(show_trgm('PGVector'), 1)")

    assert count


async def test_transcription_typo_is_caught(migrated_database: str) -> None:
    """То, ради чего канал существует: «порасгоняли» против «поразгоняли»."""
    score = await _scalar(
        migrated_database,
        "SELECT similarity(:a, :b)",
        {"a": TYPO[0], "b": TYPO[1]},
    )

    assert isinstance(score, float)
    assert score > 0.5, f"опечатка расшифровки не ловится: сходство {score}"


async def test_word_form_is_caught(migrated_database: str) -> None:
    """Словоформа внутри длинного текста — работа `word_similarity`."""
    score = await _scalar(
        migrated_database,
        "SELECT word_similarity('напоминание', 'напомни мне уйти домой в семь сорок')",
    )

    assert isinstance(score, float)
    assert score > 0.3


async def test_database_locale_is_utf8(migrated_database: str) -> None:
    """Причина, а не следствие: пусть в ошибке будет видно саму локаль."""
    ctype = await _scalar(
        migrated_database,
        "SELECT datctype FROM pg_database WHERE datname = current_database()",
    )

    assert ctype != "C", "локаль базы `C` — pg_trgm не увидит кириллицу"
