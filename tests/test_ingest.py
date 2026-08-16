"""Что должно работать:

1. текстовое сообщение сохраняется в entries
2. голосовое сохраняется вместе с заданием на расшифровку — и в задании
   лежит идентификатор подтверждения, которое воркер потом отредактирует
3. в задании только идентификаторы: содержимое записи в очередь не уезжает
4. повторная доставка того же апдейта не создаёт вторую запись
5. «напомни завтра в 10» заводит напоминание и отложенное задание
6. обычная мысль со словом «завтра» напоминанием не становится
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from brain.core import ingest
from brain.core.scheduler.queue import REMINDER, TRANSCRIBE
from brain.storage.models.entry import EntryKind
from tests.fakes import FakeUnitOfWork

TIMEZONE = "Europe/Moscow"
AUDIO_DIR = Path("var/audio")
# Пятница, 15 августа 2026, 09:00 по Москве.
NOW = datetime(2026, 8, 15, 6, 0, tzinfo=UTC)


def _text(text: str, message_id: int = 10) -> ingest.IncomingText:
    return ingest.IncomingText(
        chat_id=1, message_id=message_id, ack_message_id=message_id + 1, text=text
    )


def _voice(message_id: int = 20) -> ingest.IncomingVoice:
    return ingest.IncomingVoice(
        chat_id=1,
        message_id=message_id,
        ack_message_id=message_id + 1,
        file_id="file-abc",
        duration_sec=12,
    )


async def _capture_text(uow: FakeUnitOfWork, message: ingest.IncomingText) -> ingest.CaptureResult:
    return await ingest.capture_text(uow, message, is_private=False, timezone=TIMEZONE, now=NOW)


async def test_text_message_is_stored() -> None:
    uow = FakeUnitOfWork()

    result = await _capture_text(uow, _text("надо переписать ретривал"))

    assert result.entry_id == 1
    assert not result.duplicate
    stored = uow.entries[0]
    assert stored.kind == EntryKind.TEXT
    assert stored.raw_text == "надо переписать ретривал"
    assert stored.dedup_key == "telegram:1:10"


async def test_voice_is_stored_with_a_transcription_job() -> None:
    uow = FakeUnitOfWork()

    result = await ingest.capture_voice(
        uow, _voice(), is_private=False, timezone=TIMEZONE, audio_dir=AUDIO_DIR
    )

    stored = uow.entries[0]
    assert stored.kind == EntryKind.VOICE
    assert stored.audio_path == str(AUDIO_DIR / "1_20.oga")
    assert stored.meta["file_id"] == "file-abc"

    entrypoint, payload, execute_after = uow.jobs_named(TRANSCRIBE)[0]
    assert entrypoint == TRANSCRIBE
    assert payload == {"entry_id": result.entry_id, "chat_id": 1, "ack_message_id": 21}
    assert execute_after is None


async def test_job_carries_identifiers_only() -> None:
    """ADR-0005: очередь не должна становиться вторым местом с содержимым записи."""
    uow = FakeUnitOfWork()

    await ingest.capture_voice(
        uow, _voice(), is_private=False, timezone=TIMEZONE, audio_dir=AUDIO_DIR
    )

    _, payload, _ = uow.jobs_named(TRANSCRIBE)[0]
    assert set(payload) == {"entry_id", "chat_id", "ack_message_id"}
    assert all(isinstance(value, int) for value in payload.values())


async def test_redelivered_update_does_not_create_a_second_entry() -> None:
    uow = FakeUnitOfWork()
    message = _text("одна мысль")

    first = await _capture_text(uow, message)
    second = await _capture_text(uow, message)

    assert first.entry_id == 1
    assert second.duplicate
    assert second.entry_id is None
    assert len(uow.entries) == 1


async def test_two_identical_thoughts_are_two_entries() -> None:
    """Дедупликация по идентификатору сообщения, а не по тексту."""
    uow = FakeUnitOfWork()

    await _capture_text(uow, _text("та же мысль", message_id=10))
    await _capture_text(uow, _text("та же мысль", message_id=11))

    assert len(uow.entries) == 2


async def test_reminder_is_created_and_scheduled() -> None:
    uow = FakeUnitOfWork()

    result = await _capture_text(uow, _text("напомни завтра в 10 позвонить в банк"))

    reminder = uow.reminders[0]
    assert reminder.source_entry_id == 1
    assert reminder.fire_at.hour == 10
    assert reminder.fire_at.day == 16
    assert "позвонить в банк" in reminder.text

    entrypoint, payload, execute_after = uow.jobs_named(REMINDER)[0]
    assert entrypoint == REMINDER
    assert payload == {"reminder_id": 1, "chat_id": 1}
    # Задание отложено ровно до момента напоминания: завтра в 10 по Москве,
    # то есть через 25 часов от девяти утра сегодняшнего дня.
    assert execute_after == timedelta(hours=25)
    assert "16.08 в 10:00" in result.reply


async def test_a_thought_about_tomorrow_is_not_a_reminder() -> None:
    uow = FakeUnitOfWork()

    result = await _capture_text(uow, _text("завтра будет тяжёлый день"))

    assert uow.reminders == []
    assert uow.jobs_named(REMINDER) == []
    assert result.reply == "Записал."
