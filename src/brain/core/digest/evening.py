"""Вечерний разбор и долг, который не копится.

Вечер — единственное касание, которое просит владельца что-то сделать, и потому
единственное, к которому правило «сообщение даёт, а не требует» применяется
строже всего (`vision.md` §7.0). Из него следуют два устройства:

- **вечер приходит только со спорными записями.** Нет спорных — тишина,
  а не «что сдвинулось за день». На живом архиве спорных вышло четыре за четыре
  дня, значит тишина здесь — обычный режим, а не сбой (ADR-0013);
- **долг фиксируется молча.** Сообщение «зафиксировал за тебя N записей» — это
  ретроспективный упрёк, запрещённый принципом 2. Сколько зафиксировано само,
  видно в `/status`, и только там.

**Что считается разобранным.** Спорная запись уходит из очереди, когда поверх
неё легла поправка: «согласен» (`confirm`), «не туда» (`reclassify`) или
«прошло семь дней» (`auto_resolve`). Читается это последней строкой оверрайда,
как и всё остальное состояние записи (ADR-0003).

**Почему `confirm` и `auto_resolve` — разные значения.** Слитые в одно, они
завысили бы измеренную точность разметки: зафиксированное по таймауту
не является подтверждением от владельца, и eval классификации обязан уметь
их различать.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Exists, Subquery, func, insert, select

from brain.core.memory.repository import is_private_now, latest_transcript
from brain.storage.db import session_scope
from brain.storage.models.annotation import Annotation, AnnotationState, AnnotationType
from brain.storage.models.entry import Entry
from brain.storage.models.override import EntryOverride, OverrideAction

logger = logging.getLogger(__name__)

# Через сколько неразобранное фиксируется само (`vision.md` §7).
DEBT_AFTER = timedelta(days=7)

# Состояния, означающие «посмотри». Для владельца это одна очередь, а не две:
# «две гипотезы» и «не понял» одинаково требуют его глаз.
DISPUTED = (AnnotationState.TWO_HYPOTHESES, AnnotationState.UNCLEAR)

# Поправки, означающие «разобрано». Три, потому что разобрать можно тремя
# способами, и различать их нужно только eval'у, а не очереди.
SETTLING = (OverrideAction.CONFIRM, OverrideAction.RECLASSIFY, OverrideAction.AUTO_RESOLVE)


@dataclass(frozen=True, slots=True)
class Disputed:
    """Спорная запись — то, что вечер показывает владельцу."""

    entry_id: int
    text: str
    type: AnnotationType
    alternative_type: AnnotationType | None
    state: AnnotationState
    created_at: datetime


def _latest_annotations() -> Subquery:
    """Подзапрос «последняя разметка каждой записи». Используется дважды."""
    return (
        select(Annotation.source_entry_id, func.max(Annotation.id).label("id"))
        .group_by(Annotation.source_entry_id)
        .subquery()
    )


def _settled() -> Exists:
    """Есть ли поверх записи поправка, означающая «разобрано»."""
    return (
        select(EntryOverride.id)
        .where(
            EntryOverride.source_entry_id == Entry.id,
            EntryOverride.action.in_(SETTLING),
        )
        .exists()
    )


async def disputed(limit: int = 10, *, exclude: set[int] | None = None) -> list[Disputed]:
    """Записи, ждущие разбора. Пусто — вечер молчит.

    Потолок нужен не ради экономии, а ради двух минут: `vision.md` §7 обещает,
    что вечерний разбор занимает около двух минут, и пачка на сорок записей
    это обещание нарушает молча.

    `exclude` — то, что вечер уже показывал. Множество приходит снаружи, а
    не читается здесь из журнала: у повтора по прошедшим дням журнала нет
    вовсе, и он ведёт свой список в памяти. Спрятав чтение журнала внутрь,
    мы получили бы замер, который не воспроизводит рабочий режим, — то есть
    ровно ту ошибку, из-за которой два дня держался неверный диагноз про темы.

    Счётчик `/status` это множество не передаёт намеренно: показанное,
    но неотвеченное из очереди не уходит — оно перестаёт **спрашиваться**,
    а это разные вещи.
    """
    latest = _latest_annotations()
    body = func.coalesce(latest_transcript(), Entry.raw_text)
    seen = exclude or set()

    statement = (
        select(
            Entry.id,
            body,
            Annotation.type,
            Annotation.alternative_type,
            Annotation.state,
            Entry.created_at,
        )
        .join(latest, latest.c.source_entry_id == Entry.id)
        .join(Annotation, Annotation.id == latest.c.id)
        .where(
            Annotation.state.in_(DISPUTED),
            ~_settled(),
            ~is_private_now(),
        )
        .order_by(Entry.created_at, Entry.id)
    )
    if seen:
        statement = statement.where(Entry.id.not_in(seen))

    async with session_scope() as session:
        rows = (await session.execute(statement.limit(limit))).all()

    return [
        Disputed(
            entry_id=int(row[0]),
            text=str(row[1] or ""),
            type=row[2],
            alternative_type=row[3],
            state=row[4],
            created_at=row[5],
        )
        for row in rows
    ]


async def waiting_count() -> int:
    """Сколько записей ждут разбора. Нужно `/status`."""
    return len(await disputed(limit=1000))


async def settle(entry_id: int, *, action: OverrideAction) -> None:
    """Записать, что со спорной разметкой разобрались.

    Разметку не трогает и не переписывает: она — то, что модель действительно
    сказала, и стирать это значит терять единственный материал для eval'а.
    Ровно как у `reclassify`.
    """
    async with session_scope() as session:
        await session.execute(
            insert(EntryOverride).values(source_entry_id=entry_id, action=action, payload={})
        )


async def settle_debt(now: datetime) -> int:
    """Зафиксировать всё, что висит спорным дольше недели. **Молча.**

    Возвращает число зафиксированных — оно уходит в лог и в `/status`,
    но никогда в сообщение владельцу: «ты не разобрал N записей» это
    ретроспективный упрёк (`vision.md` §7.0, принцип 2).

    Фиксирует один раз, а не на каждом тике: после первой строки оверрайда
    запись уходит из очереди и больше сюда не попадает.
    """
    latest = _latest_annotations()

    async with session_scope() as session:
        stale = (
            await session.scalars(
                select(Entry.id)
                .join(latest, latest.c.source_entry_id == Entry.id)
                .join(Annotation, Annotation.id == latest.c.id)
                .where(
                    Annotation.state.in_(DISPUTED),
                    ~_settled(),
                    Entry.created_at <= now - DEBT_AFTER,
                )
            )
        ).all()

        for entry_id in stale:
            await session.execute(
                insert(EntryOverride).values(
                    source_entry_id=int(entry_id),
                    action=OverrideAction.AUTO_RESOLVE,
                    # Дата в нагрузке, а не только `created_at` строки: перепрогон
                    # архива однажды сдвинет `created_at` записей относительно
                    # оверрайдов, и «когда именно это зафиксировалось само»
                    # придётся отвечать глазами.
                    payload={"settled_at": now.isoformat()},
                )
            )

    if stale:
        logger.info("долг зафиксирован автоматически: записей %s", len(stale))
    return len(stale)


async def auto_resolved_count() -> int:
    """Сколько записей зафиксировалось само. Видно только в `/status`."""
    async with session_scope() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(EntryOverride)
            .where(EntryOverride.action == OverrideAction.AUTO_RESOLVE)
        )
    return int(count or 0)
