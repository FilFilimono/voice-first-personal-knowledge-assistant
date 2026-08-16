"""Приём записи на живой базе.

Здесь проверяется то, что на заглушках проверить нельзя: что инвариант 7
держится не в нашем представлении о транзакциях, а в Postgres; что уникальность
`dedup_key` защищает от двойной доставки на уровне базы; что `entries`
действительно не поддаётся правке; что напоминание срабатывает вовремя;
что упавшая расшифровка не уносит с собой запись.

Работает в одноразовой базе (`migrated_database`), рабочую не трогает.
Требует поднятого Postgres: `just up`.
"""

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from brain.core import ingest
from brain.core.memory import repository
from brain.core.memory.unit_of_work import capture_transaction
from brain.core.scheduler import jobs
from brain.core.scheduler.queue import REMINDER, TRANSCRIBE
from tests.fakes import FailingTranscriber, FakeAudioSource, FakeNotifier, StubTranscriber

pytestmark = pytest.mark.integration

TIMEZONE = "Europe/Moscow"
CHAT_ID = 111
NOW = datetime(2026, 8, 15, 6, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    """Своя пустая база на каждый тест этого модуля.

    Автоматически, а не по запросу: тест без неё молча возьмёт движок,
    оставшийся от предыдущего, и упадёт на чужом event loop — далеко
    от настоящей причины.
    """
    return migrated_database


@pytest.fixture
async def engine(database: str) -> AsyncGenerator[AsyncEngine]:
    """Отдельное соединение, чтобы смотреть на базу со стороны."""
    engine = create_async_engine(database)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _counts(engine: AsyncEngine) -> tuple[int, int]:
    """Сколько записей и заданий видно со стороны."""
    async with engine.connect() as connection:
        entries = await connection.scalar(text("SELECT count(*) FROM entries"))
        queued = await connection.scalar(text("SELECT count(*) FROM pgqueuer"))
    return int(entries or 0), int(queued or 0)


def _voice(message_id: int = 20) -> ingest.IncomingVoice:
    return ingest.IncomingVoice(
        chat_id=CHAT_ID,
        message_id=message_id,
        ack_message_id=message_id + 1,
        file_id="file-abc",
        duration_sec=12,
    )


def _text(body: str, message_id: int = 10) -> ingest.IncomingText:
    return ingest.IncomingText(
        chat_id=CHAT_ID, message_id=message_id, ack_message_id=message_id + 1, text=body
    )


async def _capture_voice(audio_dir: Path, message_id: int = 20) -> int | None:
    async with capture_transaction() as uow:
        result = await ingest.capture_voice(
            uow, _voice(message_id), is_private=False, timezone=TIMEZONE, audio_dir=audio_dir
        )
    return result.entry_id


# --- Инвариант 7 ---


async def test_entry_and_job_are_invisible_until_the_transaction_commits(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """Задание живёт внутри транзакции записи, а не в своей собственной."""
    async with capture_transaction() as uow:
        await ingest.capture_voice(
            uow, _voice(), is_private=False, timezone=TIMEZONE, audio_dir=tmp_path
        )
        # Ещё не коммит: снаружи не должно быть видно ни записи, ни задания.
        assert await _counts(engine) == (0, 0)

    assert await _counts(engine) == (1, 1)


async def test_rollback_takes_the_job_with_it(engine: AsyncEngine, tmp_path: Path) -> None:
    """Откат уносит задание: записей без задания не остаётся."""
    with pytest.raises(RuntimeError):
        async with capture_transaction() as uow:
            await ingest.capture_voice(
                uow, _voice(), is_private=False, timezone=TIMEZONE, audio_dir=tmp_path
            )
            raise RuntimeError("падение между постановкой задания и коммитом")

    assert await _counts(engine) == (0, 0)


async def test_job_payload_holds_identifiers_only(engine: AsyncEngine, tmp_path: Path) -> None:
    entry_id = await _capture_voice(tmp_path)

    async with engine.connect() as connection:
        payload = await connection.scalar(
            text("SELECT payload FROM pgqueuer WHERE entrypoint = :name"), {"name": TRANSCRIBE}
        )

    assert json.loads(payload) == {
        "entry_id": entry_id,
        "chat_id": CHAT_ID,
        "ack_message_id": 21,
    }


# --- Дедупликация и неизменяемость ---


async def test_redelivered_update_does_not_create_a_second_entry(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    first = await _capture_voice(tmp_path)
    second = await _capture_voice(tmp_path)

    assert first is not None
    assert second is None
    entries, _ = await _counts(engine)
    assert entries == 1


async def test_duplicate_dedup_key_is_rejected_by_the_database(engine: AsyncEngine) -> None:
    """Защита живёт в базе, а не в проверке «сначала посмотрим, нет ли»."""
    async with capture_transaction() as uow:
        await ingest.capture_text(uow, _text("мысль"), is_private=False, timezone=TIMEZONE, now=NOW)

    insert = text(
        "INSERT INTO entries (source, kind, raw_text, is_private, timezone, dedup_key, meta) "
        "VALUES ('telegram', 'text', 'подделка', false, :tz, :key, '{}'::jsonb)"
    )
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(insert, {"tz": TIMEZONE, "key": "telegram:111:10"})


async def test_entries_cannot_be_changed(engine: AsyncEngine) -> None:
    """Инвариант 3 держится базой, а не дисциплиной."""
    async with capture_transaction() as uow:
        await ingest.capture_text(
            uow, _text("оригинал"), is_private=False, timezone=TIMEZONE, now=NOW
        )

    for statement in (
        "UPDATE entries SET raw_text = 'подмена'",
        "DELETE FROM entries",
        "TRUNCATE entries CASCADE",
    ):
        with pytest.raises(DBAPIError, match="только на вставку"):
            async with engine.begin() as connection:
                await connection.execute(text(statement))

    async with engine.connect() as connection:
        stored = await connection.scalar(text("SELECT raw_text FROM entries"))
    assert stored == "оригинал"


# --- Расшифровка ---


async def test_voice_is_transcribed_and_the_acknowledgement_is_edited(tmp_path: Path) -> None:
    entry_id = await _capture_voice(tmp_path)
    assert entry_id is not None
    notifier = FakeNotifier()

    await jobs.transcribe(
        entry_id=entry_id,
        chat_id=CHAT_ID,
        ack_message_id=21,
        transcriber=StubTranscriber("надо переписать ретривал"),
        audio_source=FakeAudioSource(),
        notifier=notifier,
        max_duration_sec=900,
    )

    assert await repository.get_transcript(entry_id) == "надо переписать ретривал"
    # Правится то самое подтверждение, а не отправляется новое сообщение.
    assert notifier.edited == [(CHAT_ID, 21, "надо переписать ретривал")]
    assert notifier.sent == []


async def test_failing_provider_keeps_the_entry_and_the_audio(tmp_path: Path) -> None:
    """Падение STT не теряет запись: она уже в базе вместе с файлом."""
    entry_id = await _capture_voice(tmp_path)
    assert entry_id is not None
    transcriber = FailingTranscriber()

    with pytest.raises(Exception, match="провайдер не ответил"):
        await jobs.transcribe(
            entry_id=entry_id,
            chat_id=CHAT_ID,
            ack_message_id=21,
            transcriber=transcriber,
            audio_source=FakeAudioSource(),
            notifier=FakeNotifier(),
            max_duration_sec=900,
        )

    # Запись цела, аудио скачано и лежит, расшифровки нет — задание повторится.
    entry = await repository.get_entry(entry_id)
    assert entry is not None
    assert entry.audio_path is not None
    assert Path(entry.audio_path).exists()
    assert await repository.get_transcript(entry_id) is None

    # Повтор доходит до провайдера снова, а не считает работу сделанной.
    with pytest.raises(Exception, match="провайдер не ответил"):
        await jobs.transcribe(
            entry_id=entry_id,
            chat_id=CHAT_ID,
            ack_message_id=21,
            transcriber=transcriber,
            audio_source=FakeAudioSource(),
            notifier=FakeNotifier(),
            max_duration_sec=900,
        )
    assert transcriber.calls == 2


async def test_reminder_spoken_by_voice_is_scheduled(tmp_path: Path) -> None:
    """«Напомни через 3 минуты», сказанное голосом, заводит напоминание.

    До 16 августа 2026 не заводило: разбор времени стоял только на приёме
    текста, а у голосового текста в этот момент ещё нет. Запись сохранялась,
    напоминание — нет, и выглядело это как случайно сломавшаяся функция.
    """
    entry_id = await _capture_voice(tmp_path)
    assert entry_id is not None
    notifier = FakeNotifier()

    await jobs.transcribe(
        entry_id=entry_id,
        chat_id=CHAT_ID,
        ack_message_id=21,
        transcriber=StubTranscriber("Напомни мне через 3 минуты сходить за посылкой"),
        audio_source=FakeAudioSource(),
        notifier=notifier,
        max_duration_sec=900,
    )

    reminder = await repository.get_reminder(1)
    assert reminder is not None
    assert "сходить за посылкой" in reminder.text
    assert timedelta(minutes=2) < reminder.fire_at - datetime.now(UTC) <= timedelta(minutes=3)

    # В подтверждении и расшифровка, и обещание — одним сообщением.
    _, _, shown = notifier.edited[0]
    assert "сходить за посылкой" in shown
    assert "Напомню" in shown


async def test_ordinary_voice_note_creates_no_reminder(tmp_path: Path) -> None:
    entry_id = await _capture_voice(tmp_path)
    assert entry_id is not None

    await jobs.transcribe(
        entry_id=entry_id,
        chat_id=CHAT_ID,
        ack_message_id=21,
        transcriber=StubTranscriber("завтра будет тяжёлый день"),
        audio_source=FakeAudioSource(),
        notifier=FakeNotifier(),
        max_duration_sec=900,
    )

    assert await repository.get_reminder(1) is None


async def test_repeated_job_does_not_pay_for_a_second_transcription(tmp_path: Path) -> None:
    """Задание может выполниться дважды — платить за расшифровку дважды нельзя."""
    entry_id = await _capture_voice(tmp_path)
    assert entry_id is not None
    transcriber = StubTranscriber("уже расшифровано")

    for _ in range(2):
        await jobs.transcribe(
            entry_id=entry_id,
            chat_id=CHAT_ID,
            ack_message_id=21,
            transcriber=transcriber,
            audio_source=FakeAudioSource(),
            notifier=FakeNotifier(),
            max_duration_sec=900,
        )

    assert len(transcriber.calls) == 1


# --- Напоминания ---


async def test_reminder_job_is_deferred_to_the_right_moment(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)

    async with capture_transaction() as uow:
        await ingest.capture_text(
            uow,
            _text("напомни через 2 часа посмотреть логи"),
            is_private=False,
            timezone=TIMEZONE,
            now=now,
        )

    async with engine.connect() as connection:
        execute_after = await connection.scalar(
            text("SELECT execute_after FROM pgqueuer WHERE entrypoint = :name"), {"name": REMINDER}
        )

    assert abs((execute_after - now) - timedelta(hours=2)) < timedelta(seconds=5)


async def test_due_reminder_is_delivered_once() -> None:
    """Сработало — значит сработало один раз, даже если задание повторилось."""
    async with capture_transaction() as uow:
        await ingest.capture_text(
            uow,
            _text("напомни через 1 минуту выпить воды"),
            is_private=False,
            timezone=TIMEZONE,
            now=datetime.now(UTC),
        )

    notifier = FakeNotifier()
    await jobs.remind(reminder_id=1, chat_id=CHAT_ID, notifier=notifier)
    await jobs.remind(reminder_id=1, chat_id=CHAT_ID, notifier=notifier)

    assert notifier.sent == [(CHAT_ID, "⏰ выпить воды")]


async def test_reminder_fires_on_time(engine: AsyncEngine) -> None:
    """Отложенное задание становится доступным ровно тогда, когда обещано.

    Секунда вместо завтрашнего утра — проверяется механика отложенного
    выполнения, а не арифметика: её проверяет `test_reminders.py`.
    """
    async with capture_transaction() as uow:
        uow_result = await ingest.capture_text(
            uow, _text("напомни выпить воды"), is_private=False, timezone=TIMEZONE, now=NOW
        )
        await uow.enqueue(
            REMINDER, {"reminder_id": 1, "chat_id": CHAT_ID}, execute_after=timedelta(seconds=1)
        )
    assert uow_result.entry_id is not None

    due = text(
        "SELECT count(*) FROM pgqueuer "
        "WHERE entrypoint = :name AND status = 'queued' AND execute_after <= now()"
    )

    async with engine.connect() as connection:
        assert await connection.scalar(due, {"name": REMINDER}) == 0

    await asyncio.sleep(1.1)

    async with engine.connect() as connection:
        assert await connection.scalar(due, {"name": REMINDER}) == 1
