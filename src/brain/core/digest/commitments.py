"""Дела: то, что владелец пообещал себе, и что можно закрыть.

Дело рождается из единицы разметки типа «задача» (ADR-0012) и живёт дальше
своей жизнью. Утренняя сводка перечисляет незакрытые, недельное ревью считает
закрытые за неделю.

**Заведение живёт в транзакции разметки**, а не рядом с ней: `create_in`
принимает открытую сессию и вызывается из `core/enrich/repository`. Раздельными
транзакциями это писать нельзя — упав между ними, мы получили бы разметку
без дел и никакого следа о том, что так вышло. Та же дыра, что с расшифровкой
без задания на индексацию, только тише.

**Почему `core/enrich` зовёт `core/digest`, а не наоборот.** Дело принадлежит
касаниям: заводит его разметка, но читает и закрывает только сводка. Обратное
направление означало бы, что разметка знает, зачем нужны её задачи, — а она
не знает и знать не должна.

**Три подряд идущие задачи — три дела.** Схлопывать соседние единицы одного
типа нельзя: это необратимо стирает число дел, а сводка считает их поштучно
(ADR-0012, блок «Обновление»).
"""

import logging
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from brain.storage.db import session_scope
from brain.storage.models.annotation import AnnotationType
from brain.storage.models.commitment import Commitment, CommitmentState
from brain.storage.models.unit import EntryUnit

logger = logging.getLogger(__name__)

# Дело выводится только из этого типа. `loop` тоже «висит», но петля — вопрос,
# а не обещание, и закрывается она другими кнопками (`vision.md` §5.2).
FROM_TYPE = AnnotationType.TASK


async def create_in(session: AsyncSession, *, entry_id: int, annotation_id: int, text: str) -> int:
    """Завести дела по единицам только что сохранённой разметки.

    Единицы читаются из той же транзакции, в которой их вставили: до коммита
    их не видит никто снаружи, и это правильно — дело без своей единицы
    существовать не должно даже мгновение.

    Идемпотентность держит уникальность по отрезку, а не проверка в коде:
    `just reenrich` по всему архиву — обычное дело, и удвоение списка заметили
    бы только по тому, что сводка стала вдвое длиннее.
    """
    rows = (
        await session.execute(
            select(EntryUnit.id, EntryUnit.span_start, EntryUnit.span_end)
            .where(EntryUnit.annotation_id == annotation_id, EntryUnit.type == FROM_TYPE)
            .order_by(EntryUnit.ordinal)
        )
    ).all()
    if not rows:
        return 0

    created = 0
    for unit_id, start, end in rows:
        # Снимок текста, а не только отрезок: сводка обязана показать дело
        # даже после перепрогона расшифровки, сдвинувшего границы.
        snippet = text[start:end].strip()
        if not snippet:
            continue

        written = await session.scalar(
            pg_insert(Commitment)
            .values(
                source_entry_id=entry_id,
                source_unit_id=unit_id,
                text=snippet,
                span_start=start,
                span_end=end,
                state=CommitmentState.OPEN,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    Commitment.source_entry_id,
                    Commitment.span_start,
                    Commitment.span_end,
                ]
            )
            .returning(Commitment.id)
        )
        created += written is not None

    if created:
        logger.info("запись %s: заведено дел %s", entry_id, created)
    return created


async def open_ones(limit: int | None = None) -> list[Commitment]:
    """Незакрытые дела, от старых к новым.

    Порядок — возраст: `vision.md` §7 требует эскалации вверх по важности,
    а не по частоте, и «висит дольше всех» — единственная мера важности,
    которая у системы есть без спроса у владельца.
    """
    statement = (
        select(Commitment)
        .where(Commitment.state == CommitmentState.OPEN)
        .order_by(Commitment.created_at, Commitment.id)
    )
    if limit is not None:
        statement = statement.limit(limit)

    async with session_scope() as session:
        return list((await session.scalars(statement)).all())


async def open_count() -> int:
    """Сколько дел висит. Нужно `/status` и порогу недельного ревью."""
    async with session_scope() as session:
        rows = await session.scalars(
            select(Commitment.id).where(Commitment.state == CommitmentState.OPEN)
        )
    return len(list(rows))


async def close(commitment_id: int, state: CommitmentState, *, now: datetime) -> bool:
    """Закрыть дело. `False` — оно уже закрыто.

    Нажать кнопку можно сколько угодно раз: она живёт в переписке вечно.
    Вторая попытка не должна переписывать «сделал» на «не актуально» —
    та же причина, что у повторного «да» под вопросом про напоминание.
    """
    async with session_scope() as session:
        changed = await session.scalar(
            update(Commitment)
            .where(Commitment.id == commitment_id, Commitment.state == CommitmentState.OPEN)
            .values(state=state, state_changed_at=now)
            .returning(Commitment.id)
        )
    return changed is not None


async def by_id(commitment_id: int) -> Commitment | None:
    """Одно дело. Нужно кнопке — чтобы показать, что именно закрыли."""
    async with session_scope() as session:
        found: Commitment | None = await session.get(Commitment, commitment_id)
    return found


async def closed_between(
    start: datetime, end: datetime, *, state: CommitmentState
) -> list[Commitment]:
    """Что закрыли за отрезок времени. Нужно недельному ревью.

    Границы включающие с обеих сторон: ревью спрашивает про календарную неделю,
    и терять последнюю секунду воскресенья из-за полуоткрытого интервала
    было бы неверно ровно там, где ревью и отправляется.
    """
    async with session_scope() as session:
        rows = await session.scalars(
            select(Commitment)
            .where(
                Commitment.state == state,
                Commitment.state_changed_at.is_not(None),
                Commitment.state_changed_at >= start,
                Commitment.state_changed_at <= end,
            )
            .order_by(Commitment.state_changed_at)
        )
    return list(rows.all())
