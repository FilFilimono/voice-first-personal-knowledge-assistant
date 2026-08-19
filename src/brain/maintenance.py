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
from brain.core.enrich import repository as enrich_repository
from brain.core.memory.unit_of_work import capture_transaction
from brain.core.retrieval import repository as retrieval_repository
from brain.core.scheduler.queue import ENRICH, INDEX, TRANSCRIBE
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


async def reindex(*, dry_run: bool) -> int:
    """Поставить задания на индексацию всему, чему её не хватает.

    Список считается **по данным**, а не по журналу очереди — так же и по той
    же причине, что у `requeue-transcripts`. Три случая перечислены
    в `retrieval.repository.entries_needing_index`; общего у них одно:
    запись есть, а найти её нельзя.

    Ставится задание, а не считается вектор прямо здесь. Разница существенная:
    задание попадает под ретраи очереди и переживает падение провайдера,
    а цикл в этой команде на первой же сетевой ошибке остановился бы посередине
    архива — и понять, где именно, было бы нечем.
    """
    entry_ids = await retrieval_repository.entries_needing_index()

    if not entry_ids:
        print("Всё проиндексировано.")
        return 0

    print(f"Записей без индекса: {len(entry_ids)}")
    print("  " + ", ".join(str(entry_id) for entry_id in entry_ids))

    if dry_run:
        print("\nЭто прогон вхолостую: ничего не поставлено. Уберите --dry-run.")
        return 0

    for entry_id in entry_ids:
        async with capture_transaction() as uow:
            await uow.enqueue(INDEX, {"entry_id": entry_id})

    print(f"\nПоставлено заданий: {len(entry_ids)}. Запустите воркер: just worker")
    return len(entry_ids)


async def reenrich(*, dry_run: bool) -> int:
    """Поставить задания на разметку всему, что не размечено.

    Считается по данным — так же и по той же причине, что у двух команд выше.
    Приватные записи в список не попадают: разметки у них не бывает вовсе
    (инвариант 5), и это не «отставание», а нормальное состояние.

    Задание ставится одно на запись, но обработчик разберёт всё накопленное
    первой же пачкой (ADR-0011). Остальные задания найдут пустой список
    и честно ничего не сделают — так дешевле, чем выдумывать здесь пачки
    руками и разъезжаться с воркером в правилах отбора.
    """
    entries = await enrich_repository.entries_needing_annotation(limit=10_000)

    if not entries:
        print("Всё размечено.")
        return 0

    print(f"Записей без разметки: {len(entries)}")
    print("  " + ", ".join(str(entry.entry_id) for entry in entries))

    if dry_run:
        print("\nЭто прогон вхолостую: ничего не поставлено. Уберите --dry-run.")
        return 0

    for entry in entries:
        async with capture_transaction() as uow:
            await uow.enqueue(ENRICH, {"entry_id": entry.entry_id})

    print(f"\nПоставлено заданий: {len(entries)}. Запустите воркер: just worker")
    return len(entries)


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

    index = commands.add_parser(
        "reindex",
        help="поставить индексацию записям, которых нет в поиске",
    )
    index.add_argument(
        "--dry-run",
        action="store_true",
        help="показать, что было бы сделано, и ничего не менять",
    )

    annotate = commands.add_parser(
        "reenrich",
        help="поставить разметку записям, у которых её нет",
    )
    annotate.add_argument(
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
        elif arguments.command == "reindex":
            await reindex(dry_run=arguments.dry_run)
        elif arguments.command == "reenrich":
            await reenrich(dry_run=arguments.dry_run)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
