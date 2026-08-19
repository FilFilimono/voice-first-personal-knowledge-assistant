"""Что должно работать:

1. текстовая запись ставит задание на индексацию **в транзакции приёма**
2. приватная запись тоже ставит его: она обязана находиться словами
3. голосовая ставит его вместе с сохранением расшифровки — раньше нечем
4. расшифровка и задание на индексацию пишутся одной транзакцией
5. голосовая запись без расшифровки индексируется в ноль чанков, а не падает
6. повторная индексация не удваивает чанки

Пункты 1, 3 и 4 — про инвариант 7 в его втором применении. Первое (запись
и задание на расшифровку) стоило нам дыры 16 августа: задание исчезало,
запись оставалась, и узнать об этом было неоткуда. Здесь то же самое, только
тише: расшифровка без задания на индексацию выглядит совершенно здоровой,
а найти её нельзя никогда.
"""

from datetime import UTC, datetime
from pathlib import Path

from brain.core import ingest
from brain.core.scheduler.queue import INDEX
from brain.storage.models.entry import EntryKind
from tests.fakes import FakeUnitOfWork

NOW = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)


def _text(message_id: int = 1, body: str = "обычная мысль") -> ingest.IncomingText:
    return ingest.IncomingText(chat_id=42, message_id=message_id, ack_message_id=7, text=body)


async def test_text_entry_enqueues_indexing() -> None:
    uow = FakeUnitOfWork()

    await ingest.capture_text(uow, _text(), is_private=False, timezone="Europe/Moscow", now=NOW)

    assert len(uow.jobs_named(INDEX)) == 1


async def test_indexing_job_carries_only_the_entry_id() -> None:
    """В задании только идентификаторы: очередь не второе место для текста."""
    uow = FakeUnitOfWork()

    await ingest.capture_text(uow, _text(), is_private=False, timezone="Europe/Moscow", now=NOW)

    _, payload, _ = uow.jobs_named(INDEX)[0]
    assert payload == {"entry_id": 1}


async def test_private_entry_is_indexed_too() -> None:
    """Приватная запись ищется словами — значит, чанки ей нужны (ADR-0002)."""
    uow = FakeUnitOfWork()

    await ingest.capture_text(uow, _text(), is_private=True, timezone="Europe/Moscow", now=NOW)

    assert len(uow.jobs_named(INDEX)) == 1


async def test_duplicate_does_not_enqueue_indexing() -> None:
    """Повторная доставка того же сообщения не создаёт второй записи и работы."""
    uow = FakeUnitOfWork()
    message = _text()

    await ingest.capture_text(uow, message, is_private=False, timezone="Europe/Moscow", now=NOW)
    await ingest.capture_text(uow, message, is_private=False, timezone="Europe/Moscow", now=NOW)

    assert len(uow.jobs_named(INDEX)) == 1


async def test_voice_entry_waits_for_the_transcript() -> None:
    """У голосовой при приёме текста ещё нет — индексировать нечего."""
    uow = FakeUnitOfWork()

    await ingest.capture_voice(
        uow,
        ingest.IncomingVoice(
            chat_id=42, message_id=2, ack_message_id=7, file_id="f", duration_sec=5
        ),
        is_private=False,
        timezone="Europe/Moscow",
        audio_dir=Path("var/audio"),
    )

    assert uow.jobs_named(INDEX) == []
    assert uow.entries[0].kind is EntryKind.VOICE
