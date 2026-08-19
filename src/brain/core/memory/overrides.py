"""Поправки владельца: «не туда» и «сделать приватной».

Обе меняют состояние записи, и обе делают это строкой в `entry_overrides`,
не трогая `entries` (инвариант 3). Оригинал неприкосновенен физически — права
отозваны и стоит триггер, — поэтому вопрос «что с этой записью сейчас»
отвечается не колонкой, а последней поправкой.

**Ретроактивная приватность — операция с необратимой частью.** Вектора
и разметку мы удаляем, но текст уже уходил во внешнюю модель, и отозвать это
нельзя. ADR-0002 разрешает такую кнопку ровно с одним условием: бот обязан
сказать об этом прямо, а не сделать вид, что всё вернулось как было. Поэтому
функция возвращает не «готово», а перечень того, что удалось убрать, и признак
того, уходил ли текст наружу вообще.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import delete, insert, select, update

from brain.storage.db import session_scope
from brain.storage.models.annotation import Annotation, AnnotationType
from brain.storage.models.chunk import Chunk
from brain.storage.models.override import EntryOverride, OverrideAction
from brain.storage.models.topic import EntryTopic
from brain.storage.models.transcript import Transcript

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MadePrivate:
    """Что удалось убрать и что убрать было нельзя."""

    cleared_vectors: int
    removed_annotations: int
    # Уходил ли текст наружу до этого момента. У голосовой записи — всегда:
    # её расшифровал внешний сервис. У текстовой — если успели посчитаться
    # вектора или разметка.
    went_outside: bool


async def reclassify(entry_id: int, *, to: AnnotationType) -> AnnotationType | None:
    """Поправить тип записи. Вернуть тот, что был.

    Разметку не переписывает и не удаляет: она — то, что модель действительно
    сказала, и стирать это значит терять единственный материал для eval'а.
    Поправка ложится сверху, а «текущим типом» становится она (ADR-0003).
    """
    async with session_scope() as session:
        was = await session.scalar(
            select(Annotation.type)
            .where(Annotation.source_entry_id == entry_id)
            .order_by(Annotation.id.desc())
            .limit(1)
        )
        await session.execute(
            insert(EntryOverride).values(
                source_entry_id=entry_id,
                action=OverrideAction.RECLASSIFY,
                payload={"type": to.value, "was": was.value if was else None},
            )
        )
    return was


async def make_private(entry_id: int) -> MadePrivate:
    """Сделать запись приватной задним числом. Одной транзакцией.

    Порядок важен: сначала поправка, потом зачистка. Упав посередине,
    мы получили бы запись, которую система считает приватной, но у которой
    ещё лежит вектор, — и следующий проход `just reindex` увидел бы это
    и вычистил. Обратный порядок дал бы обратное: вектора нет, приватности
    тоже нет, и первый же пересчёт отправил бы текст наружу снова.
    """
    async with session_scope() as session:
        await session.execute(
            insert(EntryOverride).values(
                source_entry_id=entry_id,
                action=OverrideAction.MAKE_PRIVATE,
                payload={},
            )
        )

        cleared = (
            await session.scalars(
                update(Chunk)
                .where(Chunk.source_entry_id == entry_id, Chunk.embedding.is_not(None))
                .values(embedding=None, embedding_model=None)
                .returning(Chunk.id)
            )
        ).all()

        await session.execute(delete(EntryTopic).where(EntryTopic.source_entry_id == entry_id))
        removed = (
            await session.scalars(
                delete(Annotation)
                .where(Annotation.source_entry_id == entry_id)
                .returning(Annotation.id)
            )
        ).all()

        transcribed = await session.scalar(
            select(Transcript.id).where(Transcript.source_entry_id == entry_id).limit(1)
        )

    result = MadePrivate(
        cleared_vectors=len(cleared),
        removed_annotations=len(removed),
        went_outside=bool(transcribed) or bool(cleared) or bool(removed),
    )
    logger.info(
        "запись %s сделана приватной: векторов убрано %s, разметок %s, наружу уходило: %s",
        entry_id,
        result.cleared_vectors,
        result.removed_annotations,
        result.went_outside,
    )
    return result


async def current_type(entry_id: int) -> AnnotationType | None:
    """Чем запись считается сейчас — с учётом поправок владельца.

    Единственное место, где этот вопрос решается. Тип живёт в двух таблицах
    сразу: модель сказала своё в `annotations`, владелец мог поправить это
    в `entry_overrides`, и правым всегда остаётся владелец.
    """
    async with session_scope() as session:
        corrected = await session.scalar(
            select(EntryOverride.payload)
            .where(
                EntryOverride.source_entry_id == entry_id,
                EntryOverride.action == OverrideAction.RECLASSIFY,
            )
            .order_by(EntryOverride.id.desc())
            .limit(1)
        )
        if corrected is not None and corrected.get("type"):
            return AnnotationType(corrected["type"])

        annotated = await session.scalar(
            select(Annotation.type)
            .where(Annotation.source_entry_id == entry_id)
            .order_by(Annotation.id.desc())
            .limit(1)
        )
    return annotated
