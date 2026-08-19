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

from sqlalchemy import func, select

from brain.config import get_runtime_settings
from brain.core.digest import commitments
from brain.core.enrich import repository as enrich_repository
from brain.core.memory.repository import latest_transcript
from brain.core.memory.unit_of_work import capture_transaction
from brain.core.retrieval import repository as retrieval_repository
from brain.core.scheduler.queue import ENRICH, INDEX, TRANSCRIBE
from brain.storage.db import dispose_engine, session_scope
from brain.storage.models.annotation import Annotation, AnnotationType
from brain.storage.models.entry import Entry, EntryKind
from brain.storage.models.transcript import Transcript
from brain.storage.models.unit import EntryUnit

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


async def backfill_commitments(*, dry_run: bool) -> int:
    """Завести дела по единицам, которые уже размечены. **Без вызова модели.**

    Нужна ровно один раз, при переходе на Фазу 4: дело заводится в транзакции
    разметки (ADR-0013), а разметка, сделанная до Фазы 4, про дела ничего
    не знала. Перепрогонять её ради этого незачем — единицы уже есть, дело
    выводится из них кодом, и модель здесь не нужна вовсе.

    Идемпотентна: повторный прогон дублей не заведёт, это держит уникальность
    по отрезку в базе.

    Записи, у которых **нет единиц вовсе** (разметка до ADR-0012), эта команда
    не чинит: вывести из них дела нечем. Их список печатается отдельно —
    им нужна переразметка, то есть `just reenrich --stale` и живая модель.
    """
    async with session_scope() as session:
        latest = (
            await session.execute(
                select(Annotation.source_entry_id, func.max(Annotation.id))
                .group_by(Annotation.source_entry_id)
                .order_by(Annotation.source_entry_id)
            )
        ).all()

    created = 0
    without_units: list[int] = []
    for entry_id, annotation_id in latest:
        async with session_scope() as session:
            has_units = await session.scalar(
                select(EntryUnit.id).where(EntryUnit.annotation_id == annotation_id).limit(1)
            )
            if has_units is None:
                without_units.append(int(entry_id))
                continue

            body = await session.scalar(
                select(func.coalesce(latest_transcript(), Entry.raw_text)).where(
                    Entry.id == entry_id
                )
            )
            if dry_run:
                pending = await session.scalars(
                    select(EntryUnit.id).where(
                        EntryUnit.annotation_id == annotation_id,
                        EntryUnit.type == AnnotationType.TASK,
                    )
                )
                created += len(list(pending))
                continue

            created += await commitments.create_in(
                session,
                entry_id=int(entry_id),
                annotation_id=int(annotation_id),
                text=str(body or ""),
            )

    print(f"Дел {'нашлось бы' if dry_run else 'заведено'}: {created}")
    if without_units:
        print(
            f"\nБез смысловых единиц (разметка до ADR-0012): {len(without_units)} записей.\n"
            "  " + ", ".join(str(entry_id) for entry_id in without_units) + "\n"
            "  Им нужна переразметка: just reenrich --stale (нужна живая модель)."
        )
    if dry_run:
        print("\nЭто прогон вхолостую: ничего не записано. Уберите --dry-run.")
    return created


async def reenrich_stale(*, dry_run: bool) -> int:
    """Поставить переразметку записям, чья разметка сделана до ADR-0012.

    Такая разметка не имеет смысловых единиц, а из них выводятся дела Фазы 4
    и предложение завести петлю. Отдельно от `reenrich` намеренно: та ставит
    задания на **неразмеченное**, а это перепрогон уже размеченного — операция
    другой цены, и путать их нельзя.

    Перепрогон добавляет строку разметки, а не правит старую (ADR-0011), так
    что откатывать нечего: не понравился результат — предыдущая разметка цела.
    """
    async with session_scope() as session:
        latest = (
            select(Annotation.source_entry_id, func.max(Annotation.id).label("id"))
            .group_by(Annotation.source_entry_id)
            .subquery()
        )
        entry_ids = list(
            await session.scalars(
                select(latest.c.source_entry_id)
                .where(~select(EntryUnit.id).where(EntryUnit.annotation_id == latest.c.id).exists())
                .order_by(latest.c.source_entry_id)
            )
        )

    if not entry_ids:
        print("Разметки без смысловых единиц нет.")
        return 0

    print(f"Записей с разметкой до ADR-0012: {len(entry_ids)}")
    print("  " + ", ".join(str(entry_id) for entry_id in entry_ids))

    if dry_run:
        print("\nЭто прогон вхолостую: ничего не поставлено. Уберите --dry-run.")
        return 0

    for entry_id in entry_ids:
        async with capture_transaction() as uow:
            await uow.enqueue(ENRICH, {"entry_id": int(entry_id)})

    print(f"\nПоставлено заданий: {len(entry_ids)}. Запустите воркер: just worker")
    return len(entry_ids)


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
    annotate.add_argument(
        "--stale",
        action="store_true",
        help="перепрогнать разметку, сделанную до ADR-0012 (без смысловых единиц)",
    )

    backfill = commands.add_parser(
        "backfill-commitments",
        help="завести дела по уже размеченным единицам, без вызова модели",
    )
    backfill.add_argument(
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
            if arguments.stale:
                await reenrich_stale(dry_run=arguments.dry_run)
            else:
                await reenrich(dry_run=arguments.dry_run)
        elif arguments.command == "backfill-commitments":
            await backfill_commitments(dry_run=arguments.dry_run)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
