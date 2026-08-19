"""Что должно работать:

1. «не туда» пишет строку в `entry_overrides` и **не трогает** `entries`
2. разметка при этом остаётся: она материал для eval'а, а не мусор
3. текущим типом становится поправленный
4. «сделать приватной» обнуляет вектора и удаляет разметку с темами
5. запись остаётся в архиве целиком — приватность это не удаление
6. бот честно говорит, что текст уже уходил наружу
7. после этого запись не попадает ни в разметку, ни в пересчёт векторов

Седьмой пункт — самый опасный. `entries.is_private` после кнопки остаётся
`false`: таблица не правится (инвариант 3). Если приватность где-то читается
из колонки, первый же `just reindex` посчитает запись обычной и **отправит
её текст наружу второй раз** — молча и через неделю после нажатия.

Требует поднятого Postgres: `just up`.
"""

from typing import Any

import pytest
from sqlalchemy import Select, insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from brain.core.enrich import repository as enrich_repository
from brain.core.memory import overrides
from brain.core.memory.repository import is_entry_private
from brain.core.retrieval import repository as retrieval_repository
from brain.storage.models.annotation import Annotation, AnnotationState, AnnotationType
from brain.storage.models.chunk import DIMENSIONS, Chunk
from brain.storage.models.entry import Entry, EntryKind, EntrySource
from brain.storage.models.override import EntryOverride, OverrideAction
from brain.storage.models.topic import EntryTopic, Topic

pytestmark = pytest.mark.integration

TEXT = "мысль про то, как считать связи без вызова модели"


async def _rows(url: str, statement: Select[Any]) -> list[tuple[Any, ...]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return [tuple(row) for row in await connection.execute(statement)]
    finally:
        await engine.dispose()


async def _annotated_entry(url: str) -> int:
    """Запись с чанком, вектором, разметкой и темой — то есть уже «наружу сходившая»."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            entry_id = int(
                await connection.scalar(
                    insert(Entry)
                    .values(
                        source=EntrySource.TELEGRAM,
                        kind=EntryKind.TEXT,
                        raw_text=TEXT,
                        is_private=False,
                        timezone="Europe/Moscow",
                        dedup_key="override:1",
                        meta={},
                    )
                    .returning(Entry.id)
                )
                or 0
            )
            await connection.execute(
                insert(Chunk).values(
                    source_entry_id=entry_id,
                    ordinal=0,
                    text=TEXT,
                    embedding=[0.0] * (DIMENSIONS - 1) + [1.0],
                    embedding_model="stub-embedding-1",
                )
            )
            annotation_id = int(
                await connection.scalar(
                    insert(Annotation)
                    .values(
                        source_entry_id=entry_id,
                        type=AnnotationType.THOUGHT,
                        state=AnnotationState.SURE,
                        entities=[],
                        provider="stub",
                        model="stub-flash-lite",
                    )
                    .returning(Annotation.id)
                )
                or 0
            )
            topic_id = int(
                await connection.scalar(
                    insert(Topic)
                    .values(
                        source_entry_id=entry_id,
                        name="связи",
                        slug="связи",
                    )
                    .returning(Topic.id)
                )
                or 0
            )
            await connection.execute(
                insert(EntryTopic).values(
                    source_entry_id=entry_id,
                    topic_id=topic_id,
                    annotation_id=annotation_id,
                )
            )
        return entry_id
    finally:
        await engine.dispose()


async def test_reclassify_writes_an_override_and_keeps_the_original(
    migrated_database: str,
) -> None:
    entry_id = await _annotated_entry(migrated_database)

    was = await overrides.reclassify(entry_id, to=AnnotationType.INSIGHT)

    assert was is AnnotationType.THOUGHT
    stored = await _rows(migrated_database, select(EntryOverride.action, EntryOverride.payload))
    assert stored == [(OverrideAction.RECLASSIFY, {"type": "insight", "was": "thought"})]

    # Текст записи не тронут, и разметка на месте: поправка ложится сверху.
    assert await _rows(migrated_database, select(Entry.raw_text)) == [(TEXT,)]
    assert await _rows(migrated_database, select(Annotation.type)) == [(AnnotationType.THOUGHT,)]


async def test_current_type_follows_the_owner(migrated_database: str) -> None:
    """Правым остаётся владелец, а не модель."""
    entry_id = await _annotated_entry(migrated_database)
    assert await overrides.current_type(entry_id) is AnnotationType.THOUGHT

    await overrides.reclassify(entry_id, to=AnnotationType.INSIGHT)

    assert await overrides.current_type(entry_id) is AnnotationType.INSIGHT


async def test_make_private_clears_what_can_be_cleared(migrated_database: str) -> None:
    entry_id = await _annotated_entry(migrated_database)

    result = await overrides.make_private(entry_id)

    assert result.cleared_vectors == 1
    assert result.removed_annotations == 1
    assert result.went_outside is True

    assert await _rows(migrated_database, select(Chunk.embedding, Chunk.embedding_model)) == [
        (None, None)
    ]
    assert await _rows(migrated_database, select(Annotation.id)) == []
    assert await _rows(migrated_database, select(EntryTopic.topic_id)) == []
    # Текст и сама запись целы: приватность — это не удаление.
    assert await _rows(migrated_database, select(Entry.raw_text)) == [(TEXT,)]


async def test_private_entry_is_private_although_the_column_says_otherwise(
    migrated_database: str,
) -> None:
    """`entries` не правится, поэтому колонка после кнопки по-прежнему `false`."""
    entry_id = await _annotated_entry(migrated_database)
    await overrides.make_private(entry_id)

    assert await _rows(migrated_database, select(Entry.is_private)) == [(False,)]
    assert await is_entry_private(entry_id) is True


async def test_reindex_does_not_send_the_text_out_again(migrated_database: str) -> None:
    """Самая тихая из возможных утечек: пересчёт вернул бы вектор приватной записи."""
    entry_id = await _annotated_entry(migrated_database)
    await overrides.make_private(entry_id)

    needing = await retrieval_repository.entries_needing_index()

    # Вектор уже убран, приватной записи он не положен — чинить нечего.
    assert needing == []


async def test_private_entry_is_never_annotated_again(migrated_database: str) -> None:
    entry_id = await _annotated_entry(migrated_database)
    await overrides.make_private(entry_id)

    pending = await enrich_repository.entries_needing_annotation(limit=25)

    assert [item.entry_id for item in pending] == []
