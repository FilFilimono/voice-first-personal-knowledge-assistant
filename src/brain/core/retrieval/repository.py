"""Чтение и запись чанков.

Отдельно от `search.py` намеренно: здесь простые вставки и выборки, там —
три канала и слияние. Смешав их, мы получили бы файл, в котором самое
интересное (RRF) тонет среди SQL на вставку.
"""

from sqlalchemy import delete, func, insert, select

from brain.core.memory.repository import is_private_now, latest_transcript
from brain.storage.db import session_scope
from brain.storage.models.chunk import Chunk
from brain.storage.models.entry import Entry


async def replace_chunks(
    entry_id: int,
    chunks: list[str],
    vectors: list[list[float] | None],
    *,
    model: str | None,
) -> int:
    """Заменить чанки записи новыми. Одной транзакцией.

    Именно заменить, а не дописать: чанк — детерминированная функция от текста
    и правила резки, и хранить его историю значит копить нечитаемый мусор
    (см. `models/chunk.py`). Повторный проход задания даёт тот же результат,
    и это единственный способ сделать пересчёт безопасным.

    `vectors` может содержать `None` — у приватной записи (инвариант 5)
    вектора нет и не будет.
    """
    async with session_scope() as session:
        await session.execute(delete(Chunk).where(Chunk.source_entry_id == entry_id))

        if not chunks:
            return 0

        await session.execute(
            insert(Chunk),
            [
                {
                    "source_entry_id": entry_id,
                    "ordinal": ordinal,
                    "text": text,
                    "embedding": vector,
                    # Провенанс ставится только там, где вектор действительно
                    # есть: иначе у приватной записи в колонке оказалось бы имя
                    # модели, которая её в глаза не видела.
                    "embedding_model": model if vector is not None else None,
                }
                for ordinal, (text, vector) in enumerate(zip(chunks, vectors, strict=True))
            ],
        )
    return len(chunks)


async def chunk_count(entry_id: int) -> int:
    """Сколько чанков у записи сейчас."""
    async with session_scope() as session:
        count = await session.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.source_entry_id == entry_id)
        )
    return int(count or 0)


async def entries_needing_index() -> list[int]:
    """Записи, которым нужен индекс. Считается по данным, а не по журналу очереди.

    Так же устроен `just requeue-transcripts`, и по той же причине: задание
    может исчезнуть — из-за исчерпанных попыток, из-за падения между записью
    и постановкой, — а данные остаются. Журнал очереди тут не источник правды.

    Три случая, и все три означают «индекса нет или он неполный»:

    - у записи с текстом нет ни одного чанка;
    - у обычной (не приватной) записи есть чанк без вектора;
    - у приватной записи в чанке вектор есть — этого быть не должно вовсе,
      и пересчёт его уберёт.

    Приватность берётся `is_private_now()`, а не из колонки. Разница
    не косметическая: запись, сделанную приватной задним числом, кнопка чистит
    сразу, но по колонке она осталась обычной — и первый же пересчёт вернул бы
    ей вектор, то есть отправил бы текст наружу второй раз.
    """
    text_of = func.coalesce(latest_transcript(), Entry.raw_text)
    is_private = is_private_now()
    has_chunks = select(Chunk.id).where(Chunk.source_entry_id == Entry.id).exists()
    missing_vector = (
        select(Chunk.id)
        .where(Chunk.source_entry_id == Entry.id, Chunk.embedding.is_(None))
        .exists()
    )
    stray_vector = (
        select(Chunk.id)
        .where(Chunk.source_entry_id == Entry.id, Chunk.embedding.is_not(None))
        .exists()
    )

    async with session_scope() as session:
        result = await session.scalars(
            select(Entry.id)
            .where(
                text_of.is_not(None),
                ~has_chunks | (~is_private & missing_vector) | (is_private & stray_vector),
            )
            .order_by(Entry.id)
        )
        return list(result)
