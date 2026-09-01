"""Что должно работать:

1. значения enum'ов в базе совпадают со значениями в коде — все десять типов
   разметки, оба enum'а привычки, состояние и действие оверрайда
2. имя темы уникально по нормализованному виду, а не по показываемому
3. размерность вектора темы та же, что у чанков
4. `entries` по-прежнему не поддаётся UPDATE — таблица оверрайдов её не расколдовала

Первый пункт не педантизм: значения enum'а модель возвращает буквально
(ADR-0011), и разъехавшись, они дадут ошибку не при старте, а при первой
вставке разметки в проде.

**Сверяется и порядок, а не только набор**, и это не строгость ради строгости:
`ALTER TYPE ... ADD VALUE` кладёт новое значение в конец, а в коде оно может
стоять где угодно. `habit` стоит третьим (`AnnotationType`), поэтому миграция
добавляет его `AFTER 'task'` — без этого списки разошлись бы, ничего не сломав
и нигде не проявившись, кроме этой строки.

Четвёртый — про то, чего легко лишиться незаметно. `entry_overrides` появилась
затем, чтобы состояние записи менялось **не** правкой строки; если при этом
права на `entries` поедут, никто не заметит, потому что всё продолжит работать.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from brain.storage.models.annotation import AnnotationState, AnnotationType
from brain.storage.models.chunk import DIMENSIONS
from brain.storage.models.habit import HabitStatus, Rhythm
from brain.storage.models.override import OverrideAction

pytestmark = pytest.mark.integration


async def _fetch(url: str, sql: str) -> list[tuple[object, ...]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(sql))
            return [tuple(row) for row in result]
    finally:
        await engine.dispose()


async def _enum_values(url: str, name: str) -> list[str]:
    rows = await _fetch(
        url,
        f"""
        SELECT enumlabel
        FROM pg_enum
        JOIN pg_type ON pg_type.oid = pg_enum.enumtypid
        WHERE typname = '{name}'
        ORDER BY enumsortorder
        """,
    )
    return [str(row[0]) for row in rows]


@pytest.mark.parametrize(
    ("enum_name", "expected"),
    [
        ("annotation_type", [member.value for member in AnnotationType]),
        ("annotation_state", [member.value for member in AnnotationState]),
        ("override_action", [member.value for member in OverrideAction]),
        # Привычки. Их enum'ы значениями наружу не ходят — их не возвращает
        # модель и не присылает Telegram, — но в `callback_data` кнопки ритма
        # уезжает ровно `Rhythm.value`, и разъехавшись с базой, оно упало бы
        # на нажатии, а не при старте.
        ("habit_rhythm", [member.value for member in Rhythm]),
        ("habit_status", [member.value for member in HabitStatus]),
    ],
)
async def test_enum_values_match_the_code(
    migrated_database: str, enum_name: str, expected: list[str]
) -> None:
    assert await _enum_values(migrated_database, enum_name) == expected


async def test_topic_name_is_unique_by_normalized_form(migrated_database: str) -> None:
    """«учеба» и «учёба» — одна тема. Замер 19.08.2026 дал обе за один прогон."""
    rows = await _fetch(
        migrated_database,
        """
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'topics'::regclass AND contype = 'u'
        """,
    )
    assert [row[0] for row in rows] == ["uq_topics_slug"]


async def test_topic_vector_matches_chunk_dimension(migrated_database: str) -> None:
    """Вектора тем и чанков живут в одном пространстве или не сравниваются вовсе."""
    rows = await _fetch(
        migrated_database,
        """
        SELECT atttypmod
        FROM pg_attribute
        WHERE attrelid = 'topics'::regclass AND attname = 'embedding'
        """,
    )
    assert rows, "колонки embedding у тем нет вовсе"
    assert rows[0][0] == DIMENSIONS


async def test_entries_still_reject_update(migrated_database: str) -> None:
    """Инвариант 3 не должен был пошатнуться от появления оверрайдов."""
    engine = create_async_engine(migrated_database)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO entries"
                    " (source, kind, raw_text, is_private, timezone, dedup_key, meta)"
                    " VALUES ('telegram', 'text', 'проверка', false, 'UTC', 'schema-test', '{}')"
                )
            )
        with pytest.raises(Exception, match="entries"):
            async with engine.begin() as connection:
                await connection.execute(text("UPDATE entries SET raw_text = 'другое'"))
    finally:
        await engine.dispose()
