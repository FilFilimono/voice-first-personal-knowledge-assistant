"""Что должно работать:

1. запись с текстом, но без чанков, попадает в список на индексацию
2. обычная запись с чанком без вектора — тоже
3. приватная запись с чанком без вектора — **нет**: у неё так и должно быть
4. приватная запись, у которой вектор откуда-то взялся, — да, чтобы его убрать
5. голосовая без расшифровки в список не попадает: индексировать нечем
6. проиндексированная запись в списке не появляется
7. `--dry-run` ничего не ставит

Список считается по данным, а не по журналу очереди, — так же и по той же
причине, что у `requeue-transcripts`: задание может исчезнуть, данные нет.
Именно поэтому пункт 3 отделён от пункта 2: перепутав их, команда каждый раз
находила бы все приватные записи и каждый раз честно их «чинила».
"""

import pytest
from sqlalchemy import insert, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from brain.core.retrieval import repository
from brain.core.retrieval.indexing import index_entry
from brain.maintenance import reindex
from brain.storage.models.chunk import Chunk
from brain.storage.models.entry import Entry, EntryKind, EntrySource
from brain.storage.models.transcript import Transcript
from tests.fakes import StubEmbedder

pytestmark = pytest.mark.integration


async def _entry(
    url: str,
    *,
    key: str,
    raw_text: str | None = "мысль про поиск",
    kind: EntryKind = EntryKind.TEXT,
    is_private: bool = False,
) -> int:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            entry_id = await connection.scalar(
                insert(Entry)
                .values(
                    source=EntrySource.TELEGRAM,
                    kind=kind,
                    raw_text=raw_text,
                    audio_path="var/audio/x.oga" if kind is EntryKind.VOICE else None,
                    is_private=is_private,
                    timezone="Europe/Moscow",
                    dedup_key=key,
                    meta={},
                )
                .returning(Entry.id)
            )
        return int(entry_id or 0)
    finally:
        await engine.dispose()


async def _chunk(url: str, entry_id: int, *, vector: list[float] | None) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                insert(Chunk).values(
                    source_entry_id=entry_id,
                    ordinal=0,
                    text="мысль про поиск",
                    embedding=vector,
                    embedding_model="stub" if vector else None,
                )
            )
    finally:
        await engine.dispose()


async def _jobs(url: str) -> int:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            count = await connection.scalar(
                text("SELECT count(*) FROM pgqueuer WHERE entrypoint = 'index-entry'")
            )
        return int(count or 0)
    finally:
        await engine.dispose()


async def test_entry_without_chunks_needs_index(migrated_database: str) -> None:
    entry_id = await _entry(migrated_database, key="a")

    assert await repository.entries_needing_index() == [entry_id]


async def test_entry_with_chunk_but_no_vector_needs_index(migrated_database: str) -> None:
    entry_id = await _entry(migrated_database, key="b")
    await _chunk(migrated_database, entry_id, vector=None)

    assert await repository.entries_needing_index() == [entry_id]


async def test_indexed_entry_is_left_alone(migrated_database: str) -> None:
    entry_id = await _entry(migrated_database, key="c")
    await _chunk(migrated_database, entry_id, vector=StubEmbedder().vector("мысль про поиск"))

    assert await repository.entries_needing_index() == []


async def test_private_entry_without_vector_is_left_alone(migrated_database: str) -> None:
    """У приватной вектора и не должно быть — это не поломка (инвариант 5).

    Спутав этот случай с обычным, команда каждый раз находила бы все приватные
    записи и каждый раз бодро их «чинила», ничего не меняя.
    """
    entry_id = await _entry(migrated_database, key="d", is_private=True)
    await _chunk(migrated_database, entry_id, vector=None)

    assert await repository.entries_needing_index() == []


async def test_private_entry_with_a_stray_vector_needs_index(migrated_database: str) -> None:
    """А вот вектор у приватной — поломка, и её надо увидеть."""
    entry_id = await _entry(migrated_database, key="e", is_private=True)
    await _chunk(migrated_database, entry_id, vector=StubEmbedder().vector("мысль про поиск"))

    assert await repository.entries_needing_index() == [entry_id]


async def test_voice_without_transcript_is_not_indexable(migrated_database: str) -> None:
    """Индексировать нечем: текста ещё нет. Это ждёт расшифровки, а не индекса."""
    await _entry(migrated_database, key="f", raw_text=None, kind=EntryKind.VOICE)

    assert await repository.entries_needing_index() == []


async def test_voice_with_transcript_needs_index(migrated_database: str) -> None:
    entry_id = await _entry(migrated_database, key="g", raw_text=None, kind=EntryKind.VOICE)

    engine = create_async_engine(migrated_database)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                insert(Transcript).values(
                    source_entry_id=entry_id,
                    text="мысль про поиск",
                    provider="gemini",
                    model="gemini-3.5-flash-lite",
                    latency_ms=900,
                )
            )
    finally:
        await engine.dispose()

    assert await repository.entries_needing_index() == [entry_id]


async def test_reindex_enqueues_one_job_per_entry(migrated_database: str) -> None:
    await _entry(migrated_database, key="h")
    await _entry(migrated_database, key="i")

    queued = await reindex(dry_run=False)

    assert queued == 2
    assert await _jobs(migrated_database) == 2


async def test_dry_run_changes_nothing(migrated_database: str) -> None:
    await _entry(migrated_database, key="j")

    queued = await reindex(dry_run=True)

    assert queued == 0
    assert await _jobs(migrated_database) == 0


async def test_reindex_is_quiet_when_everything_is_indexed(migrated_database: str) -> None:
    entry_id = await _entry(migrated_database, key="k")
    await _chunk(migrated_database, entry_id, vector=StubEmbedder().vector("мысль про поиск"))

    assert await reindex(dry_run=False) == 0


async def test_reindex_replaces_chunks_without_duplicating(migrated_database: str) -> None:
    """Пересчёт заменяет чанки, а не дописывает: иначе архив удваивается молча."""
    entry_id = await _entry(migrated_database, key="l")
    embedder = StubEmbedder()

    await index_entry(embedder, entry_id=entry_id)
    await index_entry(embedder, entry_id=entry_id)

    assert await repository.chunk_count(entry_id) == 1


async def test_reindex_updates_the_unique_constraint_path(migrated_database: str) -> None:
    """Тот же порядковый номер второй раз — не ошибка, а перезапись."""
    entry_id = await _entry(migrated_database, key="m")

    await repository.replace_chunks(entry_id, ["раз", "два"], [None, None], model=None)
    await repository.replace_chunks(entry_id, ["раз"], [None], model=None)

    assert await repository.chunk_count(entry_id) == 1


async def test_private_flag_clears_a_stray_vector(migrated_database: str) -> None:
    """Пересчёт приватной записи убирает вектор, который не должен был лечь."""
    entry_id = await _entry(migrated_database, key="n", is_private=True)
    await _chunk(migrated_database, entry_id, vector=StubEmbedder().vector("мысль про поиск"))

    await index_entry(StubEmbedder(), entry_id=entry_id)

    engine = create_async_engine(migrated_database)
    try:
        async with engine.connect() as connection:
            stored = await connection.scalar(
                text("SELECT embedding FROM chunks WHERE source_entry_id = :id"), {"id": entry_id}
            )
    finally:
        await engine.dispose()

    assert stored is None


async def test_updating_privacy_needs_no_trigger_workaround(migrated_database: str) -> None:
    """Оговорка вслух: сюда `entries` правится только тестом, и это видно.

    Штатный путь смены приватности — `entry_overrides` из ADR-0003, и он
    появится в Фазе 3. До тех пор проверка живёт на прямом UPDATE с временно
    снятым триггером, и притворяться, что это не так, не стоит.
    """
    entry_id = await _entry(migrated_database, key="o")

    engine = create_async_engine(migrated_database)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("ALTER TABLE entries DISABLE TRIGGER USER"))
            await connection.execute(
                update(Entry).where(Entry.id == entry_id).values(is_private=True)
            )
            await connection.execute(text("ALTER TABLE entries ENABLE TRIGGER USER"))
    finally:
        await engine.dispose()

    assert await repository.entries_needing_index() == [entry_id]
