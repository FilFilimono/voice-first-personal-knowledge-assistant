"""Эксплуатационные команды: то, что делается руками и изредка.

    uv run python -m brain.maintenance requeue-transcripts [--dry-run]

Отдельно от воркера намеренно: воркер крутит цикл и ничего не чинит,
а это одноразовые операции, которые запускает владелец, прочитав, что они делают.

**Когда что применять.** Упавшее задание, которое ещё лежит в очереди
со статусом failed, возвращается штатно:

    uv run pgq failed
    uv run pgq requeue <id> [<id> ...]

Команда отсюда нужна в другом случае — когда задания в очереди уже **нет**:
оно исчерпало попытки при `on_failure="delete"` (так было до 16 августа 2026),
либо процесс упал между записью и постановкой. Запись при этом цела —
инвариант 7 гарантирует, что не бывает наоборот, — но расшифровки у неё нет
и взяться ей неоткуда. Здесь список считается по самим данным: голосовая
запись без расшифровки, а не по журналу очереди.
"""

import argparse
import asyncio
import logging

from sqlalchemy import select

from brain.config import get_runtime_settings
from brain.core.memory.unit_of_work import capture_transaction
from brain.core.scheduler.queue import TRANSCRIBE
from brain.storage.db import dispose_engine, session_scope
from brain.storage.models.entry import Entry, EntryKind
from brain.storage.models.transcript import Transcript

logger = logging.getLogger(__name__)


async def voice_entries_without_transcripts() -> list[Entry]:
    """Голосовые записи, у которых расшифровки нет.

    Это и есть определение «застрявшей» записи: очередь могла потерять задание,
    данные — нет.
    """
    async with session_scope() as session:
        result = await session.scalars(
            select(Entry)
            .where(
                Entry.kind == EntryKind.VOICE,
                ~select(Transcript.id).where(Transcript.source_entry_id == Entry.id).exists(),
            )
            .order_by(Entry.id)
        )
        return list(result)


async def requeue_transcripts(*, dry_run: bool) -> int:
    """Поставить заново задания на расшифровку. Вернуть число поставленных."""
    entries = await voice_entries_without_transcripts()

    if not entries:
        print("Записей без расшифровки нет.")
        return 0

    print(f"Голосовых без расшифровки: {len(entries)}")
    for entry in entries:
        print(f"  entry {entry.id}  {entry.created_at:%d.%m %H:%M}  {entry.audio_path}")

    if dry_run:
        print("\nЭто прогон вхолостую: ничего не поставлено. Уберите --dry-run.")
        return 0

    queued = 0
    for entry in entries:
        chat_id = entry.meta.get("chat_id")
        ack_message_id = entry.meta.get("ack_message_id")
        if not isinstance(chat_id, int) or not isinstance(ack_message_id, int):
            # Без них воркеру некуда положить результат. Пропускаем громко:
            # молчаливый пропуск здесь означал бы «починили», а это не так.
            print(f"  entry {entry.id}: пропущена, в meta нет chat_id/ack_message_id")
            continue

        async with capture_transaction() as uow:
            await uow.enqueue(
                TRANSCRIBE,
                {
                    "entry_id": entry.id,
                    "chat_id": chat_id,
                    "ack_message_id": ack_message_id,
                },
            )
        queued += 1

    print(f"\nПоставлено заданий: {queued}. Запустите воркер: just worker")
    return queued


async def main() -> None:
    parser = argparse.ArgumentParser(description="Эксплуатационные команды Brain")
    commands = parser.add_subparsers(dest="command", required=True)

    requeue = commands.add_parser(
        "requeue-transcripts",
        help="поставить заново расшифровку для голосовых, у которых её нет",
    )
    requeue.add_argument(
        "--dry-run",
        action="store_true",
        help="показать, что было бы сделано, и ничего не менять",
    )

    arguments = parser.parse_args()
    get_runtime_settings()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        if arguments.command == "requeue-transcripts":
            await requeue_transcripts(dry_run=arguments.dry_run)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
