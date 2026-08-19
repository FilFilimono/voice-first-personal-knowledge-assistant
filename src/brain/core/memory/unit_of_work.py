"""Транзакция приёма: запись, напоминание и задание пишутся вместе.

Это реализация `CaptureUnitOfWork` на SQLAlchemy. Всё, что здесь делается,
идёт по одному соединению и внутри одной транзакции — включая постановку
задания в очередь (инвариант 7). Открывает и закрывает транзакцию
`capture_transaction`, и это единственное место, где она открывается.
"""

import json
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from brain.core.ports import EntryDraft, ReminderDraft, Transcription
from brain.core.scheduler.queue import queue_on
from brain.storage.db import session_scope
from brain.storage.models.entry import Entry
from brain.storage.models.reminder import Reminder, ReminderTrigger
from brain.storage.models.transcript import Transcript


class SqlCaptureUnitOfWork:
    """Единица работы поверх открытой сессии."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_entry(self, draft: EntryDraft) -> int | None:
        """Вставить запись; `None`, если такая уже есть.

        Дедупликация делается базой, а не проверкой «сначала посмотрим, нет ли»:
        два экземпляра одного апдейта могут прийти одновременно, и проверка
        перед вставкой их пропустит обоих.
        """
        statement = (
            pg_insert(Entry)
            .values(
                source=draft.source,
                kind=draft.kind,
                raw_text=draft.raw_text,
                audio_path=draft.audio_path,
                duration_sec=draft.duration_sec,
                is_private=draft.is_private,
                timezone=draft.timezone,
                dedup_key=draft.dedup_key,
                meta=dict(draft.meta),
            )
            .on_conflict_do_nothing(index_elements=[Entry.dedup_key])
            .returning(Entry.id)
        )
        entry_id: int | None = await self._session.scalar(statement)
        return entry_id

    async def add_reminder(self, draft: ReminderDraft) -> int:
        statement = (
            insert(Reminder)
            .values(
                source_entry_id=draft.source_entry_id,
                trigger=ReminderTrigger.TIME,
                fire_at=draft.fire_at,
                text=draft.text,
            )
            .returning(Reminder.id)
        )
        reminder_id = await self._session.scalar(statement)
        if reminder_id is None:  # pragma: no cover — INSERT ... RETURNING без строки
            raise RuntimeError("напоминание не вставилось")
        return reminder_id

    async def add_transcript(self, entry_id: int, transcription: Transcription) -> None:
        """Вставить расшифровку — производную запись (инвариант 4)."""
        await self._session.execute(
            insert(Transcript).values(
                source_entry_id=entry_id,
                text=transcription.text,
                provider=transcription.provider,
                model=transcription.model,
                latency_ms=transcription.latency_ms,
            )
        )

    async def enqueue(
        self,
        entrypoint: str,
        payload: Mapping[str, Any],
        *,
        execute_after: timedelta | None = None,
    ) -> None:
        """Поставить задание. В задании только идентификаторы, не содержимое.

        Так решено в ADR-0005: очередь не должна становиться вторым местом,
        где лежит текст записи.
        """
        queries = await queue_on(self._session)
        await queries.enqueue(
            entrypoint,
            json.dumps(payload).encode(),
            execute_after=execute_after,
        )


@asynccontextmanager
async def capture_transaction() -> AsyncGenerator[SqlCaptureUnitOfWork]:
    """Транзакция приёма: коммит на выходе, откат на исключении."""
    async with session_scope() as session:
        yield SqlCaptureUnitOfWork(session)
