"""Открытые петли: «думал об этом и не додумал».

`vision.md` §5.2 называет петли ядром системы — именно они превращают свалку
в мышление. Механика оттуда же:

    заводится только с подтверждением владельца
    показывается старейшая среди не спрошенных за 14 дней
    «ещё думаю» сдвигает на 14 дней
    после третьего «ещё думаю» вопрос меняется

**Про подтверждение.** Тип `loop` у единицы — это предложение модели, а не
факт. Непрошеная петля превращается в вопрос, которого владелец не задавал,
то есть в требование, запрещённое принципом 1 (`vision.md` §7.0). Поэтому
разметка петель не заводит вовсе: заводит нажатие кнопки.

**Про «минимум 3 дня между вопросами», которое есть в vision, а здесь нет.**
Оно строго слабее правила четырнадцати дней: петля, спрошенная вчера,
не попадёт в выборку ещё две недели, и трёхдневный пол не может сработать
ни при каком порядке событий. Написать его отдельным условием значило бы
завести ветку, в которую невозможно попасть, — а такая ветка через полгода
читается как работающая защита и вводит в заблуждение.

**Про «переформулировать».** Vision предлагает после третьего откладывания
спросить «закрыть или переформулировать?». Переформулирование здесь закрывает
петлю и просит записать мысль заново — а не открывает поле ввода. Причина
не в экономии: в этой системе способ что-то сказать — записать это. Новая
формулировка, пришедшая обычной записью, получит разметку, связи и своё
подтверждение петли; формулировка, введённая в ответ на кнопку, не получила бы
ничего из этого и осталась бы строкой в чужой таблице.
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from brain.core.memory.repository import latest_transcript
from brain.storage.db import session_scope
from brain.storage.models.annotation import Annotation, AnnotationType
from brain.storage.models.entry import Entry
from brain.storage.models.loop import Loop, LoopStatus
from brain.storage.models.unit import EntryUnit

# Промежуток между вопросами по одной петле (`vision.md` §5.2). Он же — сдвиг
# на «ещё думаю»: спросили и получили «пока нет» — оба раза ждём одинаково.
INTERVAL = timedelta(days=14)

# Сколько раз можно отложить, прежде чем вопрос сменится на «закрыть или
# переформулировать?». Три — из vision. Смысл числа не в счёте, а в том, что
# четвёртый одинаковый вопрос перестаёт быть возвратом и становится упрёком.
SNOOZES_BEFORE_DECISION = 3


def is_stuck(times_asked: int) -> bool:
    """Пора ли менять вопрос. Чистая функция — её проверяет быстрый тест."""
    return times_asked >= SNOOZES_BEFORE_DECISION


async def create(*, entry_id: int, unit_id: int | None, question: str) -> int:
    """Завести петлю по подтверждению владельца. Повторно — не заводит.

    Кнопка живёт в переписке вечно, нажать её можно сколько угодно раз. Два
    одинаковых вопроса раздражают сильнее, чем отсутствие одного, — та же
    причина, что у повторного «да» под вопросом про напоминание.
    """
    async with session_scope() as session:
        existing = await session.scalar(
            select(Loop.id).where(Loop.source_entry_id == entry_id).order_by(Loop.id).limit(1)
        )
        if existing is not None:
            return int(existing)

        created = await session.scalar(
            pg_insert(Loop)
            .values(
                source_entry_id=entry_id,
                source_unit_id=unit_id,
                question=question.strip(),
                status=LoopStatus.OPEN,
            )
            .returning(Loop.id)
        )
    if created is None:  # pragma: no cover — INSERT ... RETURNING без строки
        raise RuntimeError("петля не завелась")
    return int(created)


async def create_from_entry(entry_id: int) -> int | None:
    """Завести петлю по записи, в которой разметка нашла открытый вопрос.

    Текст берётся из **единицы**, а не из записи целиком: запись «подумал про
    переезд, а ещё надо купить хлеб» содержит петлю только в первой половине,
    и вопрос «Ты хотел разобраться: купить хлеб» был бы бессмысленным.

    `None` — единицы типа `loop` в текущей разметке нет. Такое бывает, если
    запись переразметили между показом кнопки и нажатием: кнопка живёт
    в переписке вечно, а разметка — нет.
    """
    async with session_scope() as session:
        latest = await session.scalar(
            select(func.max(Annotation.id)).where(Annotation.source_entry_id == entry_id)
        )
        if latest is None:
            return None

        found = (
            await session.execute(
                select(EntryUnit.id, EntryUnit.span_start, EntryUnit.span_end)
                .where(EntryUnit.annotation_id == latest, EntryUnit.type == AnnotationType.LOOP)
                .order_by(EntryUnit.ordinal)
                .limit(1)
            )
        ).first()
        if found is None:
            return None

        unit_id, start, end = found
        body = await session.scalar(
            select(func.coalesce(latest_transcript(), Entry.raw_text)).where(Entry.id == entry_id)
        )

    question = str(body or "")[start:end].strip()
    if not question:
        return None
    return await create(entry_id=entry_id, unit_id=int(unit_id), question=question)


async def pick(now: datetime) -> Loop | None:
    """Какую петлю показать сегодня. `None` — ни одной подходящей.

    Старейшая среди открытых, не спрошенных за две недели и не отложенных.
    Ровно одна: `vision.md` §5.1 говорит «**одна** открытая петля», и это
    не про экономию места — список висящих вопросов это уже упрёк, а не возврат.
    """
    since = now - INTERVAL
    async with session_scope() as session:
        found: Loop | None = await session.scalar(
            select(Loop)
            .where(
                Loop.status == LoopStatus.OPEN,
                (Loop.last_asked_at.is_(None)) | (Loop.last_asked_at <= since),
                (Loop.snooze_until.is_(None)) | (Loop.snooze_until <= now),
            )
            .order_by(Loop.created_at, Loop.id)
            .limit(1)
        )
    return found


async def mark_asked(loop_id: int, *, now: datetime) -> None:
    """Отметить, что петлю сегодня спросили.

    Отдельно от `still_thinking`: спросить и получить ответ — разные события,
    и промежуток в две недели должен отсчитываться от вопроса, а не от ответа,
    которого может не быть вовсе.
    """
    async with session_scope() as session:
        await session.execute(update(Loop).where(Loop.id == loop_id).values(last_asked_at=now))


async def still_thinking(loop_id: int, *, now: datetime) -> None:
    """«Ещё думаю»: сдвинуть на две недели и запомнить, что откладывали.

    Сдвиг считается от **ответа**, а не от вопроса, и только поэтому колонка
    `snooze_until` вообще нужна: вопрос приходит в сводке, а отвечают на него
    когда откроют телефон — иногда через несколько дней. Считай мы от вопроса,
    «ещё думаю» не давало бы ничего сверх обычного промежутка, потому что оба
    равны четырнадцати дням.
    """
    async with session_scope() as session:
        await session.execute(
            update(Loop)
            .where(Loop.id == loop_id)
            .values(snooze_until=now + INTERVAL, times_asked=Loop.times_asked + 1)
        )


async def settle(loop_id: int, status: LoopStatus) -> bool:
    """Закрыть петлю — решённой или неактуальной. `False` — уже закрыта."""
    async with session_scope() as session:
        changed = await session.scalar(
            update(Loop)
            .where(Loop.id == loop_id, Loop.status == LoopStatus.OPEN)
            .values(status=status)
            .returning(Loop.id)
        )
    return changed is not None


async def by_id(loop_id: int) -> Loop | None:
    async with session_scope() as session:
        found: Loop | None = await session.get(Loop, loop_id)
    return found


async def open_count() -> int:
    """Сколько петель висит. Нужно `/status` и порогу недельного ревью."""
    async with session_scope() as session:
        count = await session.scalar(
            select(func.count()).select_from(Loop).where(Loop.status == LoopStatus.OPEN)
        )
    return int(count or 0)


async def oldest_open_age(now: datetime) -> timedelta | None:
    """Возраст старейшей открытой петли — метрика из `vision.md` §6.

    Больше тридцати дней там читается как перегрузка, а не как лень, — и это
    единственная причина, по которой число вообще считается.
    """
    async with session_scope() as session:
        oldest = await session.scalar(
            select(func.min(Loop.created_at)).where(Loop.status == LoopStatus.OPEN)
        )
    return None if oldest is None else now - oldest
