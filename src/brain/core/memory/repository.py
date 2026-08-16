"""Чтение и запись всего, что не входит в транзакцию приёма.

Отдельно от `unit_of_work.py` намеренно: там одна транзакция, в которой нельзя
ошибиться, здесь — обычные чтения и вставки производных записей. Смешивать
их значит потерять из виду то место, где живёт инвариант 7.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, select, update

from brain.core.ports import Transcription
from brain.storage.db import session_scope
from brain.storage.models.entry import Entry, EntryKind
from brain.storage.models.private_mode import PrivateModeChange
from brain.storage.models.reminder import Reminder
from brain.storage.models.transcript import Transcript


async def is_private_mode() -> bool:
    """Включён ли приватный режим сейчас.

    Текущее состояние — последняя строка журнала. Никогда не переключали —
    значит выключено: приватность включается осознанно, а не по умолчанию.
    """
    async with session_scope() as session:
        latest = await session.scalar(
            select(PrivateModeChange.enabled).order_by(PrivateModeChange.id.desc()).limit(1)
        )
    return bool(latest)


async def switch_private_mode(*, enabled: bool) -> None:
    """Записать переключение режима."""
    async with session_scope() as session:
        await session.execute(insert(PrivateModeChange).values(enabled=enabled))


async def get_entry(entry_id: int) -> Entry | None:
    """Запись по идентификатору. Воркер получает в задании только его."""
    async with session_scope() as session:
        return await session.get(Entry, entry_id)


async def save_transcript(entry_id: int, transcription: Transcription) -> None:
    """Сохранить расшифровку как производную запись (инвариант 4)."""
    async with session_scope() as session:
        await session.execute(
            insert(Transcript).values(
                source_entry_id=entry_id,
                text=transcription.text,
                provider=transcription.provider,
                model=transcription.model,
                latency_ms=transcription.latency_ms,
            )
        )


async def get_transcript(entry_id: int) -> str | None:
    """Текущая расшифровка записи — последняя по времени.

    Перепрогон другой моделью добавляет строку, а не правит старую, поэтому
    «текущая» это всегда «последняя», а не «единственная».
    """
    async with session_scope() as session:
        text: str | None = await session.scalar(
            select(Transcript.text)
            .where(Transcript.source_entry_id == entry_id)
            .order_by(Transcript.id.desc())
            .limit(1)
        )
    return text


async def get_reminder(reminder_id: int) -> Reminder | None:
    async with session_scope() as session:
        return await session.get(Reminder, reminder_id)


async def mark_reminder_fired(reminder_id: int) -> bool:
    """Отметить напоминание отправленным.

    Возвращает `False`, если оно уже было отмечено: задание может выполниться
    дважды (ретрай после падения на полпути), и второе напоминание об одном
    и том же раздражает сильнее, чем отсутствие первого.
    """
    async with session_scope() as session:
        updated: int | None = await session.scalar(
            update(Reminder)
            .where(Reminder.id == reminder_id, Reminder.fired_at.is_(None))
            .values(fired_at=datetime.now(UTC))
            .returning(Reminder.id)
        )
    return updated is not None


async def audio_paths_past_retention(days: int) -> list[str]:
    """Пути к аудио, которому пора исчезнуть.

    Строку записи это не трогает: `entries` не правится (инвариант 3).
    Удаляется файл, `audio_path` остаётся указывать в пустоту — отсутствующий
    файл читается как «удалено по ретеншену». Цена такого решения записана
    в ADR-0003: зачистка колонки потребовала бы привилегированной роли.
    """
    threshold = datetime.now(UTC) - timedelta(days=days)
    async with session_scope() as session:
        result = await session.scalars(
            select(Entry.audio_path).where(
                Entry.kind == EntryKind.VOICE,
                Entry.audio_path.is_not(None),
                Entry.created_at < threshold,
            )
        )
    return [path for path in result if path]
