"""Событие в календаре из записи владельца. Только по нажатию.

Критерий готовности Фазы 5 звучит дословно: **«ни одно событие не создано без
моего нажатия»** (`tech_plan.md` §8). Здесь это не пожелание, а устройство:
единственный вход — `create_for_entry`, и зовёт его один обработчик нажатия
кнопки. Ни приём записи, ни разметка, ни воркер сюда не заходят.

**Проверка приватности живёт здесь, а не у вызывающего** — инвариант 5, тот же
порядок, что у `core/llm` и `core/stt`. И она стоит **в момент создания**,
а не в момент показа кнопки, потому что приватность ретроактивна (ADR-0002):
владелец мог нажать «🔒 Приватная» уже после того, как увидел предложение.
Кнопки у приватной записи при этом нет вовсе — но одно другого не заменяет:
отсутствие кнопки про удобство, проверка здесь про инвариант.

**Отказ Google не роняет ничего.** Календарь лежит — запись цела, напоминание
внутри бота работает как раньше. То же правило, по которому косметическая
модель не имеет права валить старт воркера (ADR-0015, п. 6): без календаря
система работает, без захвата — нет.

**Время события берётся из напоминания, а не передаётся из адаптера.** Оно уже
разобрано, уже лежит в базе и уже показано владельцу той самой строкой, на
которую он ответил «да». Второй разбор того же текста рядом однажды разошёлся
бы с первым, и владелец получил бы событие не на тот час, чем подтвердил.
"""

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from brain.config import get_settings
from brain.core.memory.repository import is_private_now
from brain.integrations.gcal import errors
from brain.integrations.gcal.client import build_calendar
from brain.storage.db import session_scope
from brain.storage.models.entry import Entry
from brain.storage.models.entry_calendar_event import EntryCalendarEvent
from brain.storage.models.reminder import Reminder

# Сколько длится событие, собранное из напоминания.
#
# **Число выбрано, а не измерено** — как часы слотов в `digest/slots.WINDOWS`,
# и помечено так же, чтобы через полгода его не читали как вывод из данных.
# Полчаса потому, что напоминание — это момент, а событие обязано занимать
# отрезок: нулевая длительность в Google рисуется засечкой, которую не видно
# на недельном виде, то есть ровно там, куда владелец и смотрит. Уточнять
# по данным нечего: длительности у напоминания нет и не будет.
DURATION = timedelta(minutes=30)


class Outcome(enum.StrEnum):
    """Чем кончилась попытка. Пять исходов, и четыре из них — не ошибки."""

    CREATED = "created"
    # Событие по этой записи уже создавали. Кнопка живёт в переписке вечно,
    # и нажать её можно сколько угодно раз — та же причина, по которой
    # `has_reminder` защищает от второго напоминания.
    ALREADY = "already"
    # Инвариант 5. Наружу не уходит ни при каких условиях.
    PRIVATE = "private"
    # Календарь не настроен: владелец не дошёл до конца инструкции.
    OFF = "off"
    # Напоминания у записи нет — значит, нечего класть в календарь.
    NO_REMINDER = "no_reminder"
    # Google отказал. Запись при этом цела.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Result:
    """Что произошло и что из этого показать владельцу.

    `calendar` — **имя**, а не идентификатор: `brain@group.calendar.google.com`
    владельцу ничего не говорит, а требование «я вижу, что именно ушло
    и в какой календарь» — часть приёмки фазы.
    """

    outcome: Outcome
    title: str = ""
    starts_at: datetime | None = None
    calendar: str = ""
    detail: str = ""


async def offered(entry_id: int) -> bool:
    """Показывать ли кнопку «в календарь» под этой записью.

    Четыре условия, и все четыре — про то, что нажатие даст результат. Кнопка,
    которая ничего не делает, хуже отсутствующей: владелец нажимает, ничего
    не происходит, и починить это он не может, потому что не знает, почему.

    **Наличие напоминания — условие, а не забота вызывающего.** Путей, на
    которых напоминание заводится, два: явное «напомни в четверг в 19:00»
    заводит его сразу при приёме, а повелительный глагол без «напомни» —
    только после нажатия «Да». Спрашивать про напоминание в адаптере значило бы
    написать это условие дважды и однажды разойтись: кнопка появилась бы там,
    где класть в календарь нечего.

    Приватность здесь — **удобство, а не защита**. Защита стоит
    в `create_for_entry` и работает даже тогда, когда запись сделали приватной
    уже после показа кнопки.
    """
    if not get_settings().calendar_configured:
        return False
    if await _reminder(entry_id) is None:
        return False
    if await _is_private(entry_id):
        return False
    return not await _already(entry_id)


async def create_for_entry(entry_id: int, *, now: datetime) -> Result:
    """Создать событие по записи. Единственный вход, зовётся из нажатия.

    Порядок проверок — от дешёвых к дорогим и от наших к чужим: настройка,
    приватность, наличие напоминания, повтор, и только потом сеть. Сходить
    в Google и там выяснить, что запись приватная, было бы поздно.
    """
    settings = get_settings()
    if not settings.calendar_configured:
        return Result(outcome=Outcome.OFF, detail=errors.SETUP_DOC)

    # Инвариант 5, и он проверяется **здесь**, а не у вызывающего.
    if await _is_private(entry_id):
        return Result(outcome=Outcome.PRIVATE)

    planned = await _reminder(entry_id)
    if planned is None:
        return Result(outcome=Outcome.NO_REMINDER)

    title, starts_at = planned
    if await _already(entry_id):
        return Result(outcome=Outcome.ALREADY, title=title, starts_at=starts_at)

    calendar = build_calendar(settings)
    try:
        created = await calendar.create_event(
            calendar_id=settings.google_calendar_target_id,
            title=title,
            starts_at=starts_at,
            ends_at=starts_at + DURATION,
            # Провенанс виден и на той стороне: открыв событие в телефоне,
            # владелец должен понимать, откуда оно взялось.
            description=f"Из записи №{entry_id} в Brain.",
        )
    except errors.CalendarError as refusal:
        # Отказ стоит события, а не записи. Наверх он не поднимается: приём
        # записи и напоминание внутри бота от календаря не зависят.
        return Result(
            outcome=Outcome.FAILED,
            title=title,
            starts_at=starts_at,
            detail=errors.advice(refusal),
        )

    await _remember(
        entry_id=entry_id,
        calendar_id=settings.google_calendar_target_id,
        google_event_id=created.id,
        title=title,
        starts_at=starts_at,
        ends_at=starts_at + DURATION,
        now=now,
    )

    return Result(
        outcome=Outcome.CREATED,
        title=title,
        starts_at=starts_at,
        calendar=settings.google_calendar_name,
    )


async def _is_private(entry_id: int) -> bool:
    """Приватна ли запись **сейчас**.

    Через `is_private_now()` — единственное место, где этот вопрос решается
    (инвариант 3). Колонка `entries.is_private` ответила бы «нет» на запись,
    которую сделали приватной кнопкой, и это ровно та дыра, ради которой
    выражение и заведено.
    """
    async with session_scope() as session:
        found = await session.scalar(select(is_private_now()).where(Entry.id == entry_id))
    return bool(found)


async def _reminder(entry_id: int) -> tuple[str, datetime] | None:
    """Текст и время напоминания записи. `None` — напоминания нет.

    Берётся последнее: напоминание на запись заводится один раз
    (`repository.has_reminder` стоит на страже), но порядок задаётся явно —
    неявный порядок строк однажды меняется вместе с планом запроса.
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Reminder.text, Reminder.fire_at)
                .where(Reminder.source_entry_id == entry_id)
                .order_by(Reminder.id.desc())
                .limit(1)
            )
        ).first()

    if row is None:
        return None
    return str(row[0]), row[1]


async def _already(entry_id: int) -> bool:
    """Создавали ли уже событие по этой записи."""
    async with session_scope() as session:
        found = await session.scalar(
            select(EntryCalendarEvent.id).where(EntryCalendarEvent.source_entry_id == entry_id)
        )
    return found is not None


async def _remember(
    *,
    entry_id: int,
    calendar_id: str,
    google_event_id: str,
    title: str,
    starts_at: datetime,
    ends_at: datetime,
    now: datetime,
) -> None:
    """Записать провенанс: инвариант 4 и доказательство критерия фазы разом.

    `ON CONFLICT DO NOTHING` — потому что уникальность держит база, а не
    проверка в коде: два нажатия, пришедшие разом, проверку «сначала
    посмотрим, потом вставим» проходят оба.
    """
    async with session_scope() as session:
        await session.execute(
            pg_insert(EntryCalendarEvent)
            .values(
                source_entry_id=entry_id,
                calendar_id=calendar_id,
                google_event_id=google_event_id,
                title=title,
                starts_at=starts_at,
                ends_at=ends_at,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=[EntryCalendarEvent.google_event_id])
        )


__all__ = ["DURATION", "Outcome", "Result", "create_for_entry", "offered"]
