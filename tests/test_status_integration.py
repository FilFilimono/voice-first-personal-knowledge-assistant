"""Сбор состояния на живой базе.

Считать тут нечего, кроме правды: `/status` существует ради вопроса «всё ли
работает», и ошибка здесь опаснее отсутствия команды — она сообщит, что всё
хорошо, когда это не так.

Требует поднятого Postgres: `just up`.
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from brain.core import ingest, status
from brain.core.memory import repository
from brain.core.memory.unit_of_work import capture_transaction
from brain.core.scheduler.queue import HEARTBEAT

pytestmark = pytest.mark.integration

TIMEZONE = "Europe/Moscow"
CHAT_ID = 111


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


@pytest.fixture
async def engine(database: str) -> AsyncGenerator[AsyncEngine]:
    engine = create_async_engine(database)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _beat(engine: AsyncEngine, *, ago: timedelta) -> None:
    """Отметка пульса так, как её ставит воркер, — строкой в расписаниях."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO pgqueuer_schedules (expression, entrypoint, next_run, last_run) "
                "VALUES ('* * * * *', :name, now(), :last_run)"
            ),
            {"name": HEARTBEAT, "last_run": datetime.now(UTC) - ago},
        )


async def _capture(body: str, message_id: int = 10) -> None:
    async with capture_transaction() as uow:
        await ingest.capture_text(
            uow,
            ingest.IncomingText(
                chat_id=CHAT_ID,
                message_id=message_id,
                ack_message_id=message_id + 1,
                text=body,
            ),
            is_private=False,
            timezone=TIMEZONE,
            now=datetime.now(UTC),
        )


async def test_worker_is_dead_when_it_never_reported(engine: AsyncEngine) -> None:
    """Пустая таблица расписаний — воркер не запускался ни разу."""
    collected = await status.collect()

    assert not collected.worker.alive
    assert collected.worker.last_beat is None


async def test_fresh_beat_means_alive(engine: AsyncEngine) -> None:
    await _beat(engine, ago=timedelta(seconds=5))

    assert (await status.collect()).worker.alive


async def test_stale_beat_means_dead(engine: AsyncEngine) -> None:
    """Ровно тот случай 16 августа: бот отвечает, воркер лежит."""
    await _beat(engine, ago=timedelta(minutes=30))

    collected = await status.collect()

    assert not collected.worker.alive
    assert collected.worker.silent_for is not None
    assert collected.worker.silent_for >= timedelta(minutes=29)


async def test_counts_reflect_the_archive(engine: AsyncEngine, tmp_path: Path) -> None:
    await _capture("первая мысль", message_id=10)
    await _capture("вторая мысль", message_id=11)

    async with capture_transaction() as uow:
        await ingest.capture_voice(
            uow,
            ingest.IncomingVoice(
                chat_id=CHAT_ID,
                message_id=20,
                ack_message_id=21,
                file_id="file-abc",
                duration_sec=12,
            ),
            is_private=False,
            timezone=TIMEZONE,
            audio_dir=tmp_path,
        )

    collected = await status.collect()

    assert collected.entries == 3
    assert collected.voice_without_transcript == 1
    # Пять заданий: каждая текстовая запись поставила индексацию и разметку,
    # голосовая — расшифровку. Свои индексацию и разметку она поставит потом,
    # вместе с расшифровкой: текста у неё сейчас нет, размечать нечего.
    assert collected.jobs_queued == 5
    assert collected.entries_without_index == 2
    assert collected.entries_without_annotation == 2
    assert collected.jobs_failed == 0


async def test_failed_jobs_are_counted_separately(engine: AsyncEngine) -> None:
    """Упавшие ждут решения и не должны теряться среди активных."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO pgqueuer (priority, status, entrypoint, payload) "
                "VALUES (0, 'failed', 'transcribe', '{}')"
            )
        )

    collected = await status.collect()

    assert collected.jobs_failed == 1
    assert collected.jobs_queued == 0


async def test_next_reminder_is_the_earliest_unfired(engine: AsyncEngine) -> None:
    await _capture("напомни через 3 часа про созвон", message_id=10)
    await _capture("напомни через 1 час выпить воды", message_id=11)

    collected = await status.collect()

    assert collected.next_reminder is not None
    assert "выпить воды" in collected.next_reminder.text


async def test_fired_reminders_are_not_shown(engine: AsyncEngine) -> None:
    await _capture("напомни через 1 час выпить воды", message_id=11)
    assert await repository.mark_reminder_fired(1)

    assert (await status.collect()).next_reminder is None


async def test_private_mode_is_reported(engine: AsyncEngine) -> None:
    assert not (await status.collect()).private_mode

    await repository.switch_private_mode(enabled=True)

    assert (await status.collect()).private_mode
