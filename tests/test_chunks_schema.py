"""Что должно работать:

1. размерность вектора в базе совпадает с константой в коде
2. у каждого канала поиска есть свой индекс — иначе канал работает перебором
3. `tsv` считает база: колонка генерируемая, а не заполняемая приложением
4. номер чанка уникален внутри записи — пересчёт обязан быть идемпотентным

Первый пункт — не педантизм. Размерность записана в трёх местах: в миграции,
в модели и в ответе провайдера. Разъехавшись, они дадут ошибку не при старте,
а при первой вставке вектора в проде.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from brain.storage.models.chunk import DIMENSIONS, FTS_CONFIG

pytestmark = pytest.mark.integration


async def _fetch(url: str, sql: str) -> list[tuple[object, ...]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(sql))
            return [tuple(row) for row in result]
    finally:
        await engine.dispose()


async def test_embedding_dimension_matches_the_constant(migrated_database: str) -> None:
    """Схема и код обязаны говорить одно и то же про размерность."""
    rows = await _fetch(
        migrated_database,
        """
        SELECT atttypmod
        FROM pg_attribute
        WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'
        """,
    )

    assert rows, "колонки embedding нет вовсе"
    assert rows[0][0] == DIMENSIONS


async def test_every_channel_has_an_index(migrated_database: str) -> None:
    """Три канала — три индекса. Канал без индекса это последовательный перебор."""
    rows = await _fetch(
        migrated_database,
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'chunks'",
    )
    definitions = " ".join(str(row[0]) for row in rows)

    assert "gin" in definitions and "tsv" in definitions, "нет индекса под полнотекст"
    assert "gin_trgm_ops" in definitions, "нет индекса под триграммы"
    assert "hnsw" in definitions, "нет индекса под вектора"


async def test_tsvector_is_computed_by_the_database(migrated_database: str) -> None:
    """Колонку, которую заполняет приложение, однажды забудут заполнить."""
    rows = await _fetch(
        migrated_database,
        """
        SELECT attgenerated, pg_get_expr(adbin, adrelid)
        FROM pg_attribute
        LEFT JOIN pg_attrdef ON adrelid = attrelid AND adnum = attnum
        WHERE attrelid = 'chunks'::regclass AND attname = 'tsv'
        """,
    )

    # `attgenerated` — тип "char", asyncpg отдаёт его байтами, а не строкой.
    assert rows and rows[0][0] == b"s", "tsv не генерируемая колонка"
    assert FTS_CONFIG in str(rows[0][1])


async def test_ordinal_is_unique_within_entry(migrated_database: str) -> None:
    """Без этого повторный пересчёт удвоил бы чанки записи молча."""
    rows = await _fetch(
        migrated_database,
        """
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'chunks'::regclass AND contype = 'u'
        """,
    )

    assert [row[0] for row in rows] == ["uq_chunks_entry_ordinal"]
