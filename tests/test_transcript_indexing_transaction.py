"""Инвариант 7 во втором применении: расшифровка и задание на индексацию.

Первое применение — запись и задание на расшифровку — стоило нам дыры
16 августа 2026: задание исчезало, запись оставалась целой, и заметить это
можно было только по отсутствию ответа от бота.

Здесь то же самое, но незаметнее. Расшифровка без задания на индексацию
выглядит совершенно здоровой: текст на месте, бот ответил, владелец доволен.
И только через месяц выяснится, что эта запись не находится и не находилась
никогда. Поэтому они пишутся вместе или не пишутся вовсе.

Требует поднятого Postgres.
"""

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import create_async_engine

from brain.core.memory.unit_of_work import capture_transaction
from brain.core.ports import Transcription
from brain.core.scheduler.queue import INDEX
from brain.storage.models.entry import Entry, EntryKind, EntrySource

pytestmark = pytest.mark.integration

HEARD = Transcription(
    text="Мы сейчас разрабатываем фазу два, то бишь внедрение поиска.",
    provider="gemini",
    model="gemini-3.5-flash-lite",
    latency_ms=1200,
)


async def _entry(url: str) -> int:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            entry_id = await connection.scalar(
                insert(Entry)
                .values(
                    source=EntrySource.TELEGRAM,
                    kind=EntryKind.VOICE,
                    audio_path="var/audio/1.oga",
                    duration_sec=12,
                    is_private=False,
                    timezone="Europe/Moscow",
                    dedup_key="telegram:1:1",
                    meta={},
                )
                .returning(Entry.id)
            )
        return int(entry_id or 0)
    finally:
        await engine.dispose()


async def _counts(url: str, entry_id: int) -> tuple[int, int]:
    """Расшифровок и заданий на индексацию — глядя со стороны."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            transcripts = await connection.scalar(
                text("SELECT count(*) FROM transcripts WHERE source_entry_id = :id"),
                {"id": entry_id},
            )
            jobs = await connection.scalar(
                text("SELECT count(*) FROM pgqueuer WHERE entrypoint = :entrypoint"),
                {"entrypoint": INDEX},
            )
        return int(transcripts or 0), int(jobs or 0)
    finally:
        await engine.dispose()


async def test_transcript_and_indexing_land_together(migrated_database: str) -> None:
    entry_id = await _entry(migrated_database)

    async with capture_transaction() as uow:
        await uow.add_transcript(entry_id, HEARD)
        await uow.enqueue(INDEX, {"entry_id": entry_id})

    assert await _counts(migrated_database, entry_id) == (1, 1)


async def test_neither_lands_when_the_transaction_fails(migrated_database: str) -> None:
    """Упав между ними, мы получили бы ненаходимую расшифровку и ни следа."""
    entry_id = await _entry(migrated_database)

    with pytest.raises(RuntimeError, match="упало на полпути"):
        async with capture_transaction() as uow:
            await uow.add_transcript(entry_id, HEARD)
            await uow.enqueue(INDEX, {"entry_id": entry_id})
            raise RuntimeError("упало на полпути")

    assert await _counts(migrated_database, entry_id) == (0, 0)


async def test_nothing_is_visible_before_the_commit(migrated_database: str) -> None:
    """Пока транзакция открыта, снаружи не видно ни того, ни другого."""
    entry_id = await _entry(migrated_database)

    async with capture_transaction() as uow:
        await uow.add_transcript(entry_id, HEARD)
        await uow.enqueue(INDEX, {"entry_id": entry_id})

        assert await _counts(migrated_database, entry_id) == (0, 0)

    assert await _counts(migrated_database, entry_id) == (1, 1)
