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

**Состояний четыре, и переход между ними один — `set_state`.** Второй двери
здесь быть не должно: правило «отложенное дело обязано иметь срок» держится
не схемой, а тем, что состояние и срок пишутся одним UPDATE. Разведи их
по двум функциям — и однажды одна из них поставит `snoozed` без срока, а дело
после этого не покажется никогда и ни в чём: ни в сводке, ни в `/status`.
"""

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, and_, func, or_, select, update
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


def shown_now(now: datetime) -> ColumnElement[bool]:
    """Условие «дело сегодня видно». Одно место, где это решается.

    Открытое и взятое в работу видно всегда: `in_progress` не уходит из сводки,
    иначе начатое и брошенное перестало бы напоминать о себе. Отложенное
    возвращается своим сроком — и возвращается **само**, отдельного задания,
    которое «разбудит» его, нет: задание, не сработавшее в срок, спрятало бы
    дело навсегда и молча, а условие в запросе так сломаться не может.
    """
    return or_(
        Commitment.state.in_((CommitmentState.OPEN, CommitmentState.IN_PROGRESS)),
        and_(Commitment.state == CommitmentState.SNOOZED, Commitment.snoozed_until <= now),
    )


async def open_ones(now: datetime, limit: int | None = None) -> list[Commitment]:
    """Дела, которые сегодня висят, от старых к новым.

    Порядок — возраст: `vision.md` §7 требует эскалации вверх по важности,
    а не по частоте, и «висит дольше всех» — единственная мера важности,
    которая у системы есть без спроса у владельца. Взятое в работу поэтому
    остаётся на своём месте, а не поднимается наверх: подъём был бы наградой
    за начало, то есть счётчиком усердия (`vision.md` §7.0, принцип 5).
    """
    statement = (
        select(Commitment).where(shown_now(now)).order_by(Commitment.created_at, Commitment.id)
    )
    if limit is not None:
        statement = statement.limit(limit)

    async with session_scope() as session:
        return list((await session.scalars(statement)).all())


async def open_count(now: datetime) -> int:
    """Сколько дел висит. Нужно `/status` и порогу недельного ревью.

    Считает ровно то же, что показывает сводка: разъехавшись, число в `/status`
    противоречило бы списку в сообщении, и владелец не смог бы понять, кто из
    двоих врёт.
    """
    async with session_scope() as session:
        count = await session.scalar(
            select(func.count()).select_from(Commitment).where(shown_now(now))
        )
    return int(count or 0)


# Сроки откладывания. Дни, а не «часы до», потому что считаются они по календарю
# владельца — см. `snooze_term`.
TERMS = {"day": 1, "week": 7, "month": 30}


def snooze_term(now: datetime, *, term: str, timezone: str) -> datetime:
    """До какого момента прятать дело. Полночь по календарю владельца.

    «До завтра», отсчитанное сутками от нажатия, вернуло бы дело послезавтра:
    отложили в одиннадцать утра — сутки кончаются в одиннадцать следующего дня,
    а сводка приходит в девять. Поэтому срок — начало нужного дня, а не
    «плюс столько-то часов».
    """
    zone = ZoneInfo(timezone)
    day = (now.astimezone(zone) + timedelta(days=TERMS[term])).date()
    return datetime.combine(day, time.min, tzinfo=zone)


def return_target(previous: CommitmentState) -> CommitmentState:
    """Куда возвращает «Вернуть». Обычно — в прежнее состояние.

    Единственное исключение — прежний `snoozed`: срок к моменту возврата уже
    стёрт переходом, и вернуть дело в «отложено» значило бы отложить его
    без срока, то есть спрятать навсегда. Возврат идёт в `open`, и владелец
    видит дело в ближайшей сводке — то есть ошибается система в сторону
    «показать лишнее», а не «потерять молча».
    """
    return CommitmentState.OPEN if previous is CommitmentState.SNOOZED else previous


async def set_state(
    commitment_id: int,
    state: CommitmentState,
    *,
    now: datetime,
    until: datetime | None = None,
) -> CommitmentState | None:
    """Перевести дело в состояние. Возвращает **прежнее** — или `None`, если дела нет.

    Прежнее состояние возвращается не для отчётности: на нём стоит отмена
    последнего действия. Кнопка «Вернуть» везёт его в `callback_data`, потому
    что по данным его уже не восстановить — дело могло быть «в работе» до того,
    как его завершили, и возврат всегда в `open` молча стёр бы то, что владелец
    про это дело уже сказал.

    **Срок и состояние пишутся одной транзакцией**, и иначе быть не может:
    это один UPDATE. Дело в `snoozed` без срока не показалось бы больше
    никогда, а заметить это было бы неоткуда — оно просто перестало бы
    приходить. Отсюда же отказ на `snoozed` без `until`: не бывает.

    Повтор ничего не двигает: кнопка живёт в переписке вечно, и второе нажатие
    «Завершить» не должно переносить дело в другую неделю ревью.
    """
    if state is CommitmentState.SNOOZED and until is None:
        raise ValueError("отложить дело без срока нельзя: оно исчезло бы навсегда")
    if state is not CommitmentState.SNOOZED and until is not None:
        raise ValueError(f"срок бывает только у отложенного дела, а не у {state}")

    async with session_scope() as session:
        previous = await session.scalar(
            select(Commitment.state).where(Commitment.id == commitment_id).with_for_update()
        )
        if previous is None:
            return None
        if previous is state and state is not CommitmentState.SNOOZED:
            return previous

        await session.execute(
            update(Commitment)
            .where(Commitment.id == commitment_id)
            # Срок гасится при любом уходе из `snoozed`: пережив переход,
            # он однажды спрятал бы дело, которое никто не откладывал.
            .values(state=state, state_changed_at=now, snoozed_until=until)
        )
    return previous


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
