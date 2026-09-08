"""Календарь внутрь системы: сегодняшние встречи и контекст перед событием.

Две вещи, и обе про направление «внутрь» (ADR-0016, объём фазы):

    agenda        события сегодняшнего дня владельца — первой строкой
                  утренней сводки (`vision.md` §5.1)
    deliver_due   контекст за 15–30 минут до встречи (`vision.md` §5.7)

**Отбор данных здесь целиком кодом.** Три слоя контекста — напоминания,
которые владелец поставил сам; его же прежние слова по теме и человеку;
незакрытые дела и вопросы — собираются запросами и векторной близостью.
Модель пишет только фразу перед ними, и то за выключенным по умолчанию флагом
(`core/present/event`).

**Заказанное касание: вне бюджета трёх инициативных и со звуком** (§7.1).
Вне бюджета — потому что владелец о нём просил сам, поставив встречу; со
звуком — потому что звук требует внимания сейчас, а встреча через двадцать
минут именно этого и требует. Беззвучное исключение из заказанных ровно одно,
и это привычка: она приходит каждый день, а ежедневный звук обесценивает и себя,
и привычку.

**Нечего показать — молчим, и молчание видно в логе.** Строки в журнал касаний
контекст не пишет вовсе: он не слот, места в бюджете не занимает, а
`event_contexts` устроена так, что строка без `delivered_at` означает «не
дошло» — то есть повод повторить. Записать ею молчание значило бы завести
прибор, который врёт (см. шапку `storage/models/event_context`). Поэтому
молчание — строка в логе воркера, и это единственное место, где оно
отличается от слотов.

**Клиент календаря собирается здесь**, а не приходит параметром, и это надо
знать при чтении. Путь до тика — воркер → `scheduler/jobs.touch` → `digest.tick`
— календаря не несёт, и вписать его туда значит завести пятый параметр
у задания, которое про расписание. Шов при этом остался: `client` и `embedder`
— обычные функции модуля, и тест подменяет их, не поднимая ни сети, ни Google.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from brain.config import get_settings
from brain.core.digest import commitments
from brain.core.memory.repository import is_private_now
from brain.core.ports import Calendar, CalendarEvent, Embedder, LanguageModel, Notifier
from brain.core.present import event as event_view
from brain.core.retrieval import embeddings, search
from brain.integrations.gcal.client import build_calendar
from brain.storage.db import session_scope
from brain.storage.models.commitment import LIVE_STATES, Commitment
from brain.storage.models.entry import Entry
from brain.storage.models.event_context import EventContext
from brain.storage.models.loop import Loop, LoopStatus
from brain.storage.models.reminder import Reminder

logger = logging.getLogger(__name__)

# Окно перед встречей из `vision.md` §5.7. Раньше `LEAD` говорить рано —
# сообщение потеряется среди дневных дел; позже `DEADLINE` поздно — владелец
# уже идёт на встречу.
#
# Тик приходит раз в пять минут, значит внутри окна его застанут три-четыре
# раза, и первого хватает. Повтор после неудачной отправки при этом ходит
# до самого начала встречи: пятиминутное опоздание лучше молчания.
LEAD = timedelta(minutes=30)
DEADLINE = timedelta(minutes=15)

# Сколько прежних записей показывать. Две — столько же, сколько у связей
# (`LINKS_MAX`), и по той же причине: третья цитата превращает напоминание
# в чтение, а до встречи пятнадцать минут.
ARCHIVE_LIMIT = 2

# Сколько кандидатов спрашивать у поиска до отбора. Больше, чем показываем:
# верхние по слиянию могут оказаться найденными без вектора, и тогда материал
# должен найтись у следующих, а не пропасть.
SEARCH_LIMIT = 8

# Сколько незакрытого показывать. Больше трёх — и сообщение перестаёт быть
# контекстом встречи, становясь списком дел, который владелец и так видит утром.
UNFINISHED_LIMIT = 3

# Чем считается совпадение по теме: слово не короче пяти букв, сравниваемое
# первыми четырьмя. Четыре — то же число и та же причина, что у
# `present.guard.NAME_STEM`: русский меняет у слов окончание, а не начало,
# и «планёрка» с «планёрке» сходятся ровно четырьмя буквами.
#
# Пять как нижняя граница длины — потому что на четырёх буквах совпадать
# начинает всё подряд: «боту» и «боте» это разные слова, а «дела» и «делу»
# — одно, и различить их нечем. Короткое слово в названии встречи темой
# всё равно не является.
#
# Оба числа **выбраны, а не измерены**, и это надо знать: замерить их можно
# будет только на живом календаре владельца.
MIN_WORD = 5
STEM = 4


@dataclass(frozen=True, slots=True)
class Agenda:
    """Сегодняшние встречи владельца вместе с поясом, в котором их читают.

    Пояс едет рядом с событиями, а не отдельным полем сводки, по той же
    причине, по которой приватность едет рядом с текстом в `ports.EntryText`:
    тот, кто показывает время встречи, обязан узнать пояс из того же места,
    откуда взял событие. Время в системе живёт в UTC, и «то же время,
    но в другом поясе» — самая дорогая ошибка этого слоя, потому что она даёт
    правдоподобный результат.
    """

    timezone: str
    events: tuple[CalendarEvent, ...]


def client() -> Calendar | None:
    """Клиент календаря. `None` — календарь не настроен.

    `None` — законное и ожидаемое состояние: без `refresh_token` в календарь
    физически не сходить, и отдельного флага-выключателя для этого не заведено
    (`config.calendar_configured`).
    """
    settings = get_settings()
    if not settings.calendar_configured:
        return None
    return build_calendar(settings)


def embedder() -> Embedder:
    """Модель эмбеддингов для второго слоя.

    Вектор запроса — **не вызов текстовой модели**: у эмбеддингов своя квота
    и своя дверь, и инвариант 8 («связи считаются близостью, без модели»)
    здесь не нарушается.
    """
    return embeddings.build_embedder(get_settings())


# --- Первая строка утренней сводки (`vision.md` §5.1) ---


async def agenda(now: datetime, timezone: str) -> Agenda | None:
    """Встречи сегодняшнего дня владельца. `None` — календаря сегодня нет.

    `None` означает одно из двух, и оба законные: календарь не настроен либо
    не ответил. **Отказ календаря сводку не роняет** — она уйдёт без первой
    строки, ровно как уходила до Фазы 5. Заглушки «календарь недоступен» здесь
    нет намеренно: это шум в единственном сообщении, которое приходит каждый
    день, а починить его владелец всё равно сможет только из `/status`.

    Сутки берутся **в поясе владельца**, а не в UTC: в 01:00 по Риму «сегодня»
    в UTC ещё вчера, и сводка показала бы чужой день.
    """
    calendar = client()
    if calendar is None:
        return None

    zone = ZoneInfo(timezone)
    start = datetime.combine(now.astimezone(zone).date(), time.min, tzinfo=zone)

    try:
        events = await calendar.events(since=start, until=start + timedelta(days=1))
    except Exception:
        logger.exception("календарь не ответил — сводка уйдёт без первой строки")
        return None

    return Agenda(
        timezone=timezone,
        # Порядок — по началу, потом по названию: две встречи в одно время
        # обязаны показываться в одном и том же порядке два утра подряд.
        events=tuple(sorted(events, key=lambda item: (item.starts_at, item.title))),
    )


# --- Контекст перед событием (`vision.md` §5.7) ---


async def deliver_due(
    *,
    now: datetime,
    timezone: str,
    chat_id: int,
    notifier: Notifier,
    model: LanguageModel | None,
) -> list[str]:
    """Отправить контекст ко всем встречам, чьё окно открыто. Вернуть их номера.

    Порядок внутри одной встречи: собрать материал → нечего показать, молчим →
    занять строку в `event_contexts` (`decided_at`) → отправить → отметить
    доставку (`delivered_at`, `message_id`).

    **Отметка доставки ставится после отправки, и это прямое следствие находки
    7 сентября 2026.** В тот день выяснилось, что `touches.sent_at` пишется
    до отправки: упавшая отправка выглядела в журнале точно так же, как
    состоявшаяся, и сутки сгорали молча — с подтверждением в базе.

    Отказ одной встречи не отменяет соседнюю: он пишется в лог, а после цикла
    поднимается наверх одним исключением — тик огородит его `_isolated`
    и запишет строку в `tick_failures`. Проглотить его молча было бы хуже,
    чем упасть: тихо не работающий механизм в этом проекте уже стоил недели.
    """
    calendar = client()
    if calendar is None:
        return []

    # Окно спрашивается у календаря целиком: события, начинающиеся в ближайшие
    # полчаса. Отбор по нижней границе делает `_due` — Google отдаёт ещё и то,
    # что уже идёт.
    events = await calendar.events(since=now, until=now + LEAD)
    known = {event.id for event in events}
    # Про что уже сказали и что ещё не дошло — два разных ответа, и оба нужны.
    #
    # **Отсечка доставленных — правка 8 сентября 2026, и без неё механизм давал
    # ровно то, ради предотвращения чего заведён.** Тик приходит каждые пять
    # минут, окно длится пятнадцать: встреча, про которую сказали в 12:35,
    # снова попадала в окно в 12:40 и в 12:45, и владелец получал одно и то же
    # сообщение трижды. Таблица `event_contexts` при этом уже писалась —
    # не хватало единственной строки, которая её **спрашивает**. Тот же почерк,
    # что у `loops.is_stuck`: механизм есть, вызова нет.
    told = await delivered_ids(sorted(known))
    undelivered = await _undelivered(known)

    sent: list[str] = []
    failed: list[str] = []

    for event in sorted(events, key=lambda item: item.starts_at):
        if event.id in told:
            continue
        if not _due(event, now=now, retry=event.id in undelivered):
            continue
        try:
            if await _deliver(
                event,
                now=now,
                timezone=timezone,
                chat_id=chat_id,
                notifier=notifier,
                model=model,
            ):
                sent.append(event.id)
        except Exception:
            logger.exception("контекст к «%s» не отправился", event.title)
            failed.append(event.title)

    if failed:
        raise RuntimeError(f"контекст перед событием не отправился: {', '.join(failed)}")
    return sent


def _due(event: CalendarEvent, *, now: datetime, retry: bool) -> bool:
    """Пора ли говорить про эту встречу.

    Решение принимается **по данным**, а не по расписанию: тик приходит каждые
    пять минут и каждый раз спрашивает заново, ровно как у привычек. Окно
    из `vision.md` §5.7 — от тридцати минут до пятнадцати.

    `retry` — про эту встречу мы уже решили говорить, но сообщение не дошло.
    Тогда нижняя граница снимается: контекст за пять минут до встречи хуже
    контекста за двадцать, но лучше молчания. Верхняя граница остаётся всегда
    — начавшаяся встреча закрывает окно.
    """
    left = event.starts_at - now
    if left <= timedelta(0):
        return False
    return left <= LEAD if retry else DEADLINE <= left <= LEAD


async def _deliver(
    event: CalendarEvent,
    *,
    now: datetime,
    timezone: str,
    chat_id: int,
    notifier: Notifier,
    model: LanguageModel | None,
) -> bool:
    """Одна встреча. `True` — сообщение ушло."""
    meeting = await collect(event, now=now, timezone=timezone)
    if meeting.empty:
        # Молчание — обычный исход, а не сбой: у встречи «созвон» в пустом
        # архиве не найдётся ни одного из трёх слоёв. Строка в лог пишется
        # затем, чтобы тишина системы и её смерть не выглядели одинаково
        # (`vision.md` §7.0, п. 3).
        logger.info("контекст к «%s»: показывать нечего — молчим", event.title)
        return False

    text = await event_view.text(model, meeting=meeting, now=now, timezone=timezone)

    await _decide(event, now=now)
    # Со звуком: заказанное касание (`vision.md` §7.1). `send_quietly` здесь
    # был бы ошибкой, которую не видно ни в одном тесте на текст.
    message_id = await notifier.send(chat_id, text)
    await _mark_delivered(event.id, message_id=message_id, now=now)

    logger.info("контекст к «%s» отправлен, сообщение %s", event.title, message_id)
    return True


async def collect(event: CalendarEvent, *, now: datetime, timezone: str) -> event_view.Meeting:
    """Три слоя `vision.md` §5.7. Отбор целиком кодом.

    Слои считаются порознь и падают порознь: без архива контекст беднее,
    но он всё ещё контекст, а исключение отсюда стоило бы всей встречи.
    """
    stems = _stems(event)
    return event_view.Meeting(
        title=event.title,
        starts_at=event.starts_at,
        asked=tuple(await _asked(event, now=now, timezone=timezone, stems=stems)),
        said=tuple(await _said(event)),
        unfinished=tuple(await _unfinished(stems, now=now)),
    )


def _stems(event: CalendarEvent) -> set[str]:
    """Признаки темы встречи: начала слов названия и имён участников.

    Начала, а не слова целиком: «планёрка» в названии и «планёрке» в записи
    — одно и то же, а точное совпадение отвергало бы каждое второе попадание.
    Из адреса участника берётся часть до `@`: домен у всех общий и темой
    не является.
    """
    words = _words(event.title)
    for address in event.attendees:
        words += _words(address.split("@")[0])
    return {word[:STEM] for word in words if len(word) >= MIN_WORD}


def _words(text: str) -> list[str]:
    return [word for word in "".join(_readable(text)).split() if word]


def _readable(text: str) -> list[str]:
    """Текст без знаков: разделители имён и адресов не должны склеивать слова."""
    return [symbol if symbol.isalnum() else " " for symbol in text.lower()]


def _matches(text: str, stems: set[str]) -> bool:
    """Совпадает ли строка с темой встречи хотя бы одним началом слова."""
    return any(word[:STEM] in stems for word in _words(text) if len(word) >= MIN_WORD)


async def _asked(
    event: CalendarEvent, *, now: datetime, timezone: str, stems: set[str]
) -> list[event_view.Item]:
    """Слой первый: то, о чём владелец просил напомнить сам.

    Два признака, и они соединены через «или», а не через «и»: напоминание
    может сработать во время встречи (тогда оно про неё по времени) или
    называть её словами (тогда по теме). Требовать обоих значило бы терять
    «напомни в четверг спросить про бюджет» — поставленное на утро того же дня,
    а не на час встречи.

    Берутся только **несработавшие** напоминания: сработавшее владелец уже
    прочитал, и повторять его перед встречей значит показывать одно и то же
    дважды.
    """
    zone = ZoneInfo(timezone)
    day_end = datetime.combine(event.starts_at.astimezone(zone).date(), time.max, tzinfo=zone)

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Reminder.text, Reminder.fire_at, is_private_now())
                .join(Entry, Entry.id == Reminder.source_entry_id)
                .where(
                    Reminder.fired_at.is_(None),
                    Reminder.fire_at >= now,
                    Reminder.fire_at <= day_end,
                )
                .order_by(Reminder.fire_at)
            )
        ).all()

    return [
        event_view.Item(text=str(text), is_private=bool(private))
        for text, fire_at, private in rows
        if fire_at <= event.ends_at or _matches(str(text), stems)
    ]


async def _said(event: CalendarEvent) -> list[event_view.Item]:
    """Слой второй: то, что владелец сам говорил по теме и человеку.

    Считается **векторной близостью, без вызова текстовой модели** (инвариант
    8): запросом идёт название встречи вместе с именами участников, а отвечает
    гибридный поиск (`core/retrieval`). Из выдачи берутся только записи,
    которые нашёл **вектор**: по словам названия совпадает и случайное, а §5.7
    обещает «известное по теме», а не «содержащее то же слово».

    **Порога близости здесь нет, и это названо вслух.** Слияние RRF отдаёт
    ранги, а не расстояния, и абсолютное число, с которым можно сравнить,
    в нём отсутствует. Цена — связь может оказаться натянутой; видно это
    глазами владельца, а не числом, и настоящее решение придёт вместе с порогом
    в самом поиске.

    Отказ поиска стоит слоя, а не встречи: без архива контекст беднее.
    """
    query = " ".join([event.title, *[address.split("@")[0] for address in event.attendees]])
    if not query.strip():
        return []

    try:
        found = await search.search(embedder(), query, limit=SEARCH_LIMIT)
    except Exception:
        logger.exception("архив по встрече «%s» не спросился", event.title)
        return []

    return [
        event_view.Item(
            text=item.text,
            is_private=item.is_private,
            entry_id=item.entry_id,
            created_at=item.created_at,
        )
        for item in found
        if search.VECTOR in item.channels
    ][:ARCHIVE_LIMIT]


async def _unfinished(stems: set[str], *, now: datetime) -> list[event_view.Item]:
    """Слой третий: незакрытые дела и вопросы по той же теме.

    Тема — единственный отбор: показывать перед встречей весь список висящего
    значило бы прислать утреннюю сводку второй раз, а сообщение перед встречей
    обязано быть про встречу.

    Свежие первыми: старое владелец уже видел утром, а «незакрытое по этой
    теме» чаще всего появилось на прошлой встрече той же серии.
    """
    async with session_scope() as session:
        deeds = (
            await session.execute(
                select(Commitment.title, Commitment.text, is_private_now())
                .join(Entry, Entry.id == Commitment.source_entry_id)
                .where(Commitment.state.in_(LIVE_STATES), commitments.shown_now(now))
                .order_by(Commitment.id.desc())
            )
        ).all()
        questions = (
            await session.execute(
                select(Loop.title, Loop.question, is_private_now())
                .join(Entry, Entry.id == Loop.source_entry_id)
                .where(Loop.status == LoopStatus.OPEN)
                .order_by(Loop.id.desc())
            )
        ).all()

    found = [
        event_view.Item(text=str(title or body), is_private=bool(private))
        for title, body, private in [*deeds, *questions]
        if _matches(str(title or body), stems)
    ]
    return found[:UNFINISHED_LIMIT]


# --- Отметки доставки ---


async def _undelivered(event_ids: set[str]) -> set[str]:
    """Встречи, про которые мы уже решили сказать, но сообщение не дошло.

    Строка с пустым `delivered_at` — честное «не дошло», и следующий тик обязан
    попробовать снова, пока окно не закрылось. Доставленные сюда не попадают:
    повтор им не нужен, и по ним же видно, что сказано было один раз.
    """
    if not event_ids:
        return set()

    async with session_scope() as session:
        rows = await session.scalars(
            select(EventContext.google_event_id).where(
                EventContext.google_event_id.in_(event_ids),
                EventContext.delivered_at.is_(None),
            )
        )
    return {str(found) for found in rows.all()}


async def _decide(event: CalendarEvent, *, now: datetime) -> None:
    """Занять строку под встречу. Пишется **до** отправки и значит намерение.

    Повторное решение переписывает `decided_at` и не заводит второй строки:
    уникальность держит база (`google_event_id`), а не проверка в коде —
    проверка «сначала посмотрим, потом вставим» пропустила бы два тика,
    пришедших разом.
    """
    async with session_scope() as session:
        await session.execute(
            pg_insert(EventContext)
            .values(
                google_event_id=event.id,
                calendar_id=event.calendar_id,
                event_starts_at=event.starts_at,
                event_title=event.title,
                decided_at=now,
            )
            .on_conflict_do_update(
                index_elements=[EventContext.google_event_id],
                set_={"decided_at": now},
            )
        )


async def _mark_delivered(event_id: str, *, message_id: int, now: datetime) -> None:
    """Отметить доставку. Пишется **после** успешной отправки, и только тогда.

    Номер сообщения — единственное доказательство доставки, которое остаётся
    в базе: по нему потом видно, что владелец получил именно этот контекст,
    а не что мы собирались его прислать.
    """
    async with session_scope() as session:
        await session.execute(
            update(EventContext)
            .where(EventContext.google_event_id == event_id)
            .values(delivered_at=now, message_id=message_id)
        )


async def delivered_ids(event_ids: Sequence[str]) -> set[str]:
    """Встречи, контекст к которым дошёл.

    **Зовётся из `deliver_due` — это и есть отсечка «не сказать дважды».**
    До 8 сентября 2026 функция существовала и не вызывалась из продакшн-пути
    ни разу: её докстринг обещал «нужно тестам пути и `/status`», и ровно так
    в проекте выглядит начало дыры — механизм написан, таблица пишется,
    а спрашивает её только тест.
    """
    if not event_ids:
        return set()

    async with session_scope() as session:
        rows = await session.scalars(
            select(EventContext.google_event_id).where(
                EventContext.google_event_id.in_(list(event_ids)),
                EventContext.delivered_at.is_not(None),
            )
        )
    return {str(found) for found in rows.all()}


__all__ = [
    "ARCHIVE_LIMIT",
    "DEADLINE",
    "LEAD",
    "Agenda",
    "agenda",
    "client",
    "collect",
    "deliver_due",
    "delivered_ids",
    "embedder",
]
