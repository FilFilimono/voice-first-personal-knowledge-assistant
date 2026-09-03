"""Раскачка: вопрос, который вытаскивает мысль.

`vision.md` §5.9 и блок A плана Фазы 4. Диагноз владельца, ради которого
модуль существует, звучит так: **бот не даёт повода ему что-то сказать.**
Захват работает, возврата нет. Раскачка — механизм возврата в чистом виде.

**Вопросы не сочиняются из головы.** Это прямое требование §5.9, и оно
выполнено буквально: каждый вопрос набора несёт поле `method` — приём
из `docs/prompting_sources.md`, §5, — и поле `source` с разделом разбора,
где этот приём обоснован. Вопрос без опоры в набор не идёт; проверяется
тестом, а не намерением.

**Тон — решение владельца от 27.08.2026, одной строкой:**

    БОТ НЕ ОЦЕНИВАЕТ ЧЕЛОВЕКА, НО НАЗЫВАЕТ ФАКТЫ И СПРАШИВАЕТ ПРИЧИНУ.

«Ты бездельник» и «опять не сделал» отклонены владельцем прямым текстом.
Разница с разрешённым — не в резкости, а в том, требует сообщение отчёта
или предлагает разговор. Разбор источников даёт под это механизм, а не вкус
(§4.1): владелец, получивший упрёк, перестаёт отвечать честно, и система,
вся ценность которой в архиве, разрушает собственное сырьё.

**Отбор — код, речь — модель, и сегодня даже речь код** (принцип 12). Какой
вопрос, к какой теме, к какому делу и в какой день — считается запросами
к архиву, ноль вызовов модели. Формулировку в полном режиме пишет модель
(`core/present/question`), и ветка под это лежит за `WRITTEN`, выключенным
по умолчанию.

Четыре правила отбора, каждое со своей опорой:

- **один вопрос в сообщении** (§1.3): раскачка молчит в тот день, когда
  сообщение слота уже о чём-то спрашивает — например, когда в сводке есть
  петля с её «Сдвинулось?»;
- **последовательность не фиксирована** (§2.2): рамка, пройденная как
  чек-лист, даёт поверхностную рефлексию. Поэтому выбирается не «первый
  подходящий», а **тот, которого дольше всех не было**;
- **цитируем только неприватное** (инвариант 5, решение владельца № 4):
  дословная цитата — самый дешёвый способ говорить его словами (§4.4),
  но цитата уходит в модель;
- **молчание не поминается вовсе** (решение владельца № 7): вопрос не
  повторяется, про молчание не говорится ни слова, а после трёх молчаний
  подряд частота снижается сама. Считается **по журналу касаний**, а не
  счётчиком в колонке: колонка, посчитавшая молчания, однажды прочиталась бы
  вслух — других судеб у числа в базе не бывает (`vision.md` §7.0, п.5).

**Чего в наборе нет намеренно.** Шкал и «change ruler» (§1.5, п.8) — это
счётчик, запрещённый §7.0 п.5, и владелец подтвердил отказ. Вопросов про то,
что зависит от других людей (§2.3) — такие подсказки не отзываются вовсе;
вместо сужения тем вопрос переформулируется **на часть владельца**: не «почему
созвон не состоялся», а «что ты успел со своей стороны». Вопроса «как
продвижение?» — это ровно тот общий вопрос, ради замены которого §5.9 и
написан.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, literal_column, select

from brain.core.digest import slots
from brain.core.memory.repository import display_text, is_private_now
from brain.core.ports import LanguageModel
from brain.core.present import guard
from brain.core.present import question as writer
from brain.storage.db import session_scope
from brain.storage.models.commitment import Commitment, CommitmentState
from brain.storage.models.entry import Entry
from brain.storage.models.topic import EntryTopic, Topic
from brain.storage.models.touch import Touch, TouchSlot

logger = logging.getLogger(__name__)

# --- Настройки. Пока константы, и это надо знать ---
#
# Обе обязаны переехать в `brain.config` — «переключение стоит смены настроек,
# а не переписывания кода» (требование владельца, план Фазы 4). Здесь они
# лежат временно: файл настроек в момент написания блока A правился соседней
# работой, и две руки в одном файле стоят дороже, чем перенос двух строк.
# Точный текст полей — в отчёте блока A.

# В каких слотах спрашивать. **По умолчанию только утро** — решение владельца
# № 2 от 27.08.2026: каждый слот это отдельный запрос в сутки, и упирается это
# в квоту, а не во вкус. Полный режим добавляет сюда `DAY` и `EVENING`.
SLOTS: tuple[TouchSlot, ...] = (TouchSlot.MORNING,)

# Вопрос пишет модель, а не шаблон кода. Выключено — это минимальный режим
# Фазы 4. Ветка за флагом написана вместе с ним и проверяется тестом от входа
# системы с флагом, поднятым в самом тесте (`tests/test_prompting_path.py`).
WRITTEN = False

# --- Пороги отбора ---

# Сколько дней тишины по теме делают её «затихшей». Три — из разрешённой
# владельцем формулировки «за три дня ни одной записи про учёбу».
QUIET_AFTER = timedelta(days=3)

# А после скольких спрашивать уже поздно. Месяц молчания по теме — это не
# «застряло», это отпало само, и вопрос про неё читался бы как напоминание
# о брошенном, то есть как упрёк.
FORGOTTEN_AFTER = timedelta(days=30)

# Сколько записей делают тему темой. Одна-две — это ещё не тема, а совпадение
# слов, и «за три дня ни одной записи про X» про такую звучит выдумкой.
MIN_TOPIC_ENTRIES = 3

# Сколько дней подряд считается возвращением к теме. Три — то же число, что
# в утверждении из §1.6 разбора: «ты три вечера подряд возвращался к главе
# про индексы». Это утверждение по фактам, а не похвала и не серия: названо
# конкретное действие, без оценки и без счёта достижений.
RETURN_DAYS = 3

# Окно «оглянуться назад» (§1.5, п.5). Ближе двух недель — это ещё не «назад»,
# владелец такое помнит и сам; дальше двух месяцев — уже археология.
LOOK_BACK_MIN = timedelta(days=14)
LOOK_BACK_MAX = timedelta(days=60)

# Короче этого не цитируется: «ок», «в 14:50», проверка бота — там нечего
# вспоминать, а вытащенный обрывок читается как сбой. То же число, что
# у всплывания старого.
MIN_LENGTH = 100

# Сколько символов цитаты показывать. Больше, чем в сводке (70), потому что
# смысл именно в том, чтобы узнать свою мысль; меньше, чем во всплывании (400),
# потому что цитата здесь — подводка к вопросу, а не сама запись.
QUOTE = 180

# Сколько молчаний подряд снижают частоту и насколько. Оба числа выбраны,
# а не измерены, и это надо знать: про частоту раскачки источников нет вовсе
# (`docs/prompting_sources.md`, §7, п.4). Первый способ их проверить — неделя
# использования и доля слотов, на которые пришёл ответ.
SILENCE_STREAK = 3
QUIET_GAP_DAYS = 3


@dataclass(frozen=True, slots=True)
class Question:
    """Готовый вопрос вместе с тем, из чего он собран.

    `entry_ids` — записи архива, на которых вопрос стоит. Они уезжают в журнал
    касаний и возвращаются оттуда в ответ владельца: без них раскачка даёт
    повод сказать и теряет, **на что** было сказано.
    """

    kind: str
    method: str
    text: str
    entry_ids: tuple[int, ...] = ()
    # То, из чего собран вопрос, — вход модельной ветки. Приватного здесь
    # не бывает: отбор берёт цитаты запросом с `~is_private_now()`.
    facts: tuple[str, ...] = ()


Builder = Callable[[datetime, str], Awaitable[Question | None]]


@dataclass(frozen=True, slots=True)
class Item:
    """Один вопрос набора: шаблон, приём, опора и данные, на которых он стоит.

    `method` и `source` — не документация, а часть набора: `vision.md` §5.9
    требует, чтобы вопросы составлялись по исследованным практикам, и проверить
    это можно только тем, что опора лежит рядом с вопросом и читается вместе
    с ним. Пустое поле роняет тест.
    """

    method: str
    source: str
    template: str
    build: Builder
    # Чем подставляются поля шаблона, когда вопрос показывают вне архива:
    # в тестах и в отчёте владельцу. Выдуманы намеренно — живая речь в git
    # не уходит (ADR-0002).
    sample: Mapping[str, str] = field(default_factory=dict)


# --- Помощники отбора ---


def quote(text: str) -> str:
    """Цитата одной строкой. Дословно — но в пределах строки сообщения."""
    flat = " ".join(text.split())
    if len(flat) <= QUOTE:
        return flat
    return flat[: QUOTE - 1].rstrip(" ,.;:—-") + "…"


def acceptable(text: str) -> bool:
    """Годится ли готовый вопрос к показу.

    Гвард гоняется и по тексту, который собрал код, а не только по тексту
    модели, и это не перестраховка: в вопрос приезжает **дословная цитата**
    владельца, а в его собственной речи «забыл» и «так и не» — обычные слова.
    В его записи это речь, в сообщении бота — уже упрёк.
    """
    found = guard.reproach(text)
    if found is not None:
        logger.info("вопрос раскачки отброшен (упрёк «%s»): %r", found, text[:120])
    return found is None


def fits(text: str) -> bool:
    """Есть ли в сообщении место для вопроса.

    Один вопрос в сообщении — источники единогласны (`docs/prompting_sources.md`,
    §1.3, ловушка допроса). Считается знаками вопроса в уже собранном тексте
    слота, а не перебором случаев: случаев со временем станет больше, а правило
    останется тем же.
    """
    return "?" not in text


def attach(text: str, asked: str) -> str:
    """Приписать вопрос к сообщению слота — отбитым и со своим значком."""
    return f"{text}\n\n{alone(asked)}"


def alone(asked: str) -> str:
    """Вопрос сам по себе — когда слоту больше нечего сказать."""
    return f"💬 {asked}"


# --- Данные, на которых стоят вопросы ---


@dataclass(frozen=True, slots=True)
class _Recorded:
    """Запись архива в том виде, в каком её видит отбор."""

    entry_id: int
    text: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _Gap:
    """Затихшая тема и последняя запись по ней."""

    topic: str
    days: int
    last: _Recorded


async def _silent_topic(now: datetime) -> _Gap | None:
    """Тема, по которой давно нет записей. `None` — таких нет.

    Порог с двух сторон: молчание короче трёх дней ещё ничего не значит,
    длиннее месяца уже не вопрос, а напоминание о брошенном.
    """
    last_seen = func.max(Entry.created_at)

    async with session_scope() as session:
        row = (
            await session.execute(
                select(Topic.name, last_seen)
                .select_from(EntryTopic)
                .join(Topic, Topic.id == EntryTopic.topic_id)
                .join(Entry, Entry.id == EntryTopic.source_entry_id)
                .group_by(Topic.name)
                .having(func.count() >= MIN_TOPIC_ENTRIES)
                .having(last_seen <= now - QUIET_AFTER)
                .having(last_seen >= now - FORGOTTEN_AFTER)
                .order_by(last_seen.desc())
                .limit(1)
            )
        ).first()

    if row is None:
        return None

    last = await _last_of_topic(str(row[0]))
    if last is None:
        # Записи темы есть, но все приватные: цитировать нечего, и факт про
        # тему тоже не называем — тема приватной записи всё ещё её содержание.
        return None

    return _Gap(topic=str(row[0]), days=max(1, (now - row[1]).days), last=last)


async def _last_of_topic(name: str) -> _Recorded | None:
    """Последняя **неприватная** запись темы. Она же — цитата и связь."""
    body = display_text()

    async with session_scope() as session:
        row = (
            await session.execute(
                select(Entry.id, body, Entry.created_at)
                .select_from(EntryTopic)
                .join(Topic, Topic.id == EntryTopic.topic_id)
                .join(Entry, Entry.id == EntryTopic.source_entry_id)
                .where(Topic.name == name, ~is_private_now(), body.is_not(None))
                .order_by(Entry.created_at.desc())
                .limit(1)
            )
        ).first()

    if row is None:
        return None
    return _Recorded(entry_id=int(row[0]), text=str(row[1]), created_at=row[2])


async def _returned_topic(now: datetime, timezone: str) -> tuple[str, _Recorded] | None:
    """Тема, к которой владелец возвращался несколько дней подряд.

    Дни считаются по календарю владельца, а не «минус сутки»: три захода
    за одни сутки — это один день, а не три, и утверждение «три дня подряд»
    в таком случае было бы неправдой.
    """
    since = now - timedelta(days=RETURN_DAYS + 1)

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Topic.name, Entry.created_at)
                .select_from(EntryTopic)
                .join(Topic, Topic.id == EntryTopic.topic_id)
                .join(Entry, Entry.id == EntryTopic.source_entry_id)
                .where(Entry.created_at >= since, Entry.created_at <= now)
            )
        ).all()

    seen: dict[str, set[date]] = {}
    for name, created in rows:
        seen.setdefault(str(name), set()).add(slots.local_date(created, timezone))

    today = slots.local_date(now, timezone)
    wanted = {today - timedelta(days=shift) for shift in range(1, RETURN_DAYS + 1)}

    for name in sorted(seen):
        if wanted <= seen[name]:
            last = await _last_of_topic(name)
            if last is not None:
                return name, last
    return None


async def _recorded(*, since: datetime, until: datetime, newest: bool = True) -> _Recorded | None:
    """Одна неприватная запись из окна, годная к цитированию.

    Детерминированно, а не случайно: у всплывания случайность — замысел
    (`vision.md` §5.3), а здесь вопрос строится **на конкретной записи**,
    и тест, проверяющий случайный выбор, проверяет только случайность.
    """
    body = display_text()
    order = Entry.created_at.desc() if newest else Entry.created_at.asc()

    async with session_scope() as session:
        row = (
            await session.execute(
                select(Entry.id, body, Entry.created_at)
                .where(
                    Entry.created_at >= since,
                    Entry.created_at <= until,
                    ~is_private_now(),
                    func.length(body) >= MIN_LENGTH,
                )
                .order_by(order)
                .limit(1)
            )
        ).first()

    if row is None:
        return None
    return _Recorded(entry_id=int(row[0]), text=str(row[1]), created_at=row[2])


@dataclass(frozen=True, slots=True)
class _Deed:
    """Дело в том виде, в каком его видит отбор."""

    entry_id: int
    text: str


async def _deed(state: CommitmentState, now: datetime) -> _Deed | None:
    """Старейшее дело в этом состоянии, заведённое из **неприватной** записи."""
    async with session_scope() as session:
        row = (
            await session.execute(
                select(
                    Commitment.source_entry_id,
                    func.coalesce(Commitment.title, Commitment.text),
                )
                .join(Entry, Entry.id == Commitment.source_entry_id)
                .where(
                    Commitment.state == state,
                    Commitment.created_at <= now,
                    ~is_private_now(),
                )
                .order_by(Commitment.created_at.asc())
                .limit(1)
            )
        ).first()

    if row is None:
        return None
    return _Deed(entry_id=int(row[0]), text=" ".join(str(row[1]).split()))


def _yesterday(now: datetime, timezone: str) -> tuple[datetime, datetime]:
    """Границы вчерашнего дня по календарю владельца, а не «минус сутки»."""
    zone = ZoneInfo(timezone)
    day = (now.astimezone(zone) - timedelta(days=1)).date()
    start = datetime.combine(day, time.min, tzinfo=zone)
    return start, start + timedelta(days=1)


def _days(count: int) -> str:
    """Число дней по-русски. «За 1 дня» выдаёт шаблон с головой."""
    tail = count % 10
    hundreds = count % 100
    if tail == 1 and hundreds != 11:
        return f"{count} день"
    if tail in (2, 3, 4) and hundreds not in (12, 13, 14):
        return f"{count} дня"
    return f"{count} дней"


def _when(moment: datetime, timezone: str) -> str:
    """Дата словами сообщения — в календаре владельца."""
    return f"{moment.astimezone(ZoneInfo(timezone)):%d.%m}"


# --- Сам набор ---
#
# Порядок здесь — приоритет при равенстве, а не расписание: выбирается
# не первый подходящий, а тот, которого дольше всех не было (см. `pick`).
# Первым стоит вопрос, разобранный владельцем как образец всего набора.

QUIET_TOPIC = "quiet_topic"
LAST_TIME = "last_time"
LOOK_BACK = "look_back"
YESTERDAY = "yesterday"
OWN_SIDE = "own_side"
RETURNING = "returning"
INTENTION = "intention"
BLOCKER = "blocker"


async def _build_quiet_topic(now: datetime, timezone: str) -> Question | None:
    found = await _silent_topic(now)
    if found is None:
        return None
    return _make(
        QUIET_TOPIC,
        entry_ids=(found.last.entry_id,),
        facts=(found.last.text,),
        days=_days(found.days),
        topic=found.topic,
    )


async def _build_last_time(now: datetime, timezone: str) -> Question | None:
    found = await _silent_topic(now)
    if found is None:
        return None
    return _make(
        LAST_TIME,
        entry_ids=(found.last.entry_id,),
        facts=(found.last.text,),
        when=_when(found.last.created_at, timezone),
        topic=found.topic,
    )


async def _build_look_back(now: datetime, timezone: str) -> Question | None:
    found = await _recorded(since=now - LOOK_BACK_MAX, until=now - LOOK_BACK_MIN)
    if found is None:
        return None
    return _make(
        LOOK_BACK,
        entry_ids=(found.entry_id,),
        facts=(found.text,),
        when=_when(found.created_at, timezone),
        quote=quote(found.text),
    )


async def _build_yesterday(now: datetime, timezone: str) -> Question | None:
    start, end = _yesterday(now, timezone)
    found = await _recorded(since=start, until=end)
    if found is None:
        return None
    return _make(
        YESTERDAY,
        entry_ids=(found.entry_id,),
        facts=(found.text,),
        quote=quote(found.text),
    )


async def _build_own_side(now: datetime, timezone: str) -> Question | None:
    found = await _deed(CommitmentState.IN_PROGRESS, now)
    if found is None:
        return None
    return _make(
        OWN_SIDE,
        entry_ids=(found.entry_id,),
        facts=(found.text,),
        deed=quote(found.text),
    )


async def _build_returning(now: datetime, timezone: str) -> Question | None:
    found = await _returned_topic(now, timezone)
    if found is None:
        return None
    name, last = found
    return _make(
        RETURNING,
        entry_ids=(last.entry_id,),
        facts=(last.text,),
        days=_days(RETURN_DAYS),
        topic=name,
    )


async def _build_intention(now: datetime, timezone: str) -> Question | None:
    found = await _deed(CommitmentState.OPEN, now)
    if found is None:
        return None
    return _make(
        INTENTION,
        entry_ids=(found.entry_id,),
        facts=(found.text,),
        deed=quote(found.text),
    )


async def _build_blocker(now: datetime, timezone: str) -> Question | None:
    """Заготовка владельца, урезанная до одного вопроса (решение № 2).

    Данные ей всё-таки нужны, хотя сам вопрос их не называет: «что вчера
    помешало?» на пустом вчера — это вопрос ни о чём, а на вчера без единого
    висящего дела он предполагает провал, которого не было.
    """
    start, end = _yesterday(now, timezone)
    if await _recorded(since=start, until=end) is None:
        return None
    found = await _deed(CommitmentState.IN_PROGRESS, now)
    if found is None:
        return None
    return _make(BLOCKER, entry_ids=(found.entry_id,), facts=(found.text,))


SET: dict[str, Item] = {
    QUIET_TOPIC: Item(
        method="1 · назвать факт, не человека; 5 · дать выбор; 11 · факт, потом вопрос",
        source="§4.2 наблюдение против оценки · §1.2 праведный рефлекс · §2.3 MindScape",
        template="За {days} ни одной записи по теме «{topic}» — застряло или отпало?",
        build=_build_quiet_topic,
        sample={"days": "3 дня", "topic": "учёба"},
    ),
    LAST_TIME: Item(
        method="3 · попросить пример; 4 · оглянуться назад по архиву",
        source="§1.5, пп. 4 и 5 — Ask for Examples, Look Back",
        template="{when} ты последний раз писал по теме «{topic}». Что тогда получилось?",
        build=_build_last_time,
        sample={"when": "14.08", "topic": "учёба"},
    ),
    LOOK_BACK: Item(
        method="4 · оглянуться назад по архиву; 7 · говорить его словами",
        source="§1.5, п. 5 — Look Back · §4.4 чистый язык",
        template="{when} ты писал: «{quote}». Что с тех пор изменилось?",
        build=_build_look_back,
        sample={"when": "06.08", "quote": "надо разобраться с индексами"},
    ),
    YESTERDAY: Item(
        method="2 · привязать к моменту, а не к периоду; 7 · говорить его словами",
        source="§3.2 конкретный эпизод против периода · §4.4 чистый язык",
        template="Вчера ты записал: «{quote}». Что с этим сегодня?",
        build=_build_yesterday,
        sample={"quote": "надо разобраться с индексами"},
    ),
    OWN_SIDE: Item(
        method="8 · спрашивать про то, что в его власти; 2 · привязать к моменту",
        source="§2.3 подсказки не работают на чужое действие · §4.3 SBI",
        template="Ты отметил в работе: «{deed}». Что успел со своей стороны?",
        build=_build_own_side,
        sample={"deed": "разобраться с индексами"},
    ),
    RETURNING: Item(
        method="6 · утверждение по фактам вместо похвалы, «ты», а не «я»",
        source="§1.6 affirmation — конкретное действие без оценки",
        template="Ты возвращался к теме «{topic}» {days} подряд. Что там сдвинулось?",
        build=_build_returning,
        sample={"topic": "индексы", "days": "3 дня"},
    ),
    INTENTION: Item(
        method="10 · конкретизировать намерение; 7 · говорить его словами",
        source="§3.2 Gollwitzer & Sheeran, implementation intentions · §4.4",
        template="Ты писал: «{deed}». Когда сегодня к этому вернёшься?",
        build=_build_intention,
        sample={"deed": "разобраться с индексами"},
    ),
    BLOCKER: Item(
        method="9 · один вопрос в сообщении (заготовка владельца, урезанная)",
        source="§1.3 ловушка допроса · решение владельца № 2 от 27.08.2026",
        template="Что вчера помешало?",
        build=_build_blocker,
        sample={},
    ),
}


def _make(
    kind: str, *, entry_ids: Sequence[int] = (), facts: Sequence[str] = (), **fields: str
) -> Question:
    """Собрать вопрос по шаблону набора."""
    item = SET[kind]
    return Question(
        kind=kind,
        method=item.method,
        text=item.template.format(**fields),
        entry_ids=tuple(entry_ids),
        facts=tuple(facts),
    )


def sample(kind: str, **overrides: str) -> str:
    """Вопрос, собранный на образцах. Нужен тестам и отчёту владельцу.

    Лишние поля шаблону не мешают: `format` берёт только те, что в нём есть, —
    поэтому один и тот же набор подстановок годится всему набору сразу.
    """
    item = SET[kind]
    return item.template.format(**{**item.sample, **overrides})


# --- Журнал: что спрашивали и что на это ответили ---


async def _asked_days() -> dict[str, date]:
    """Когда каждый вопрос набора спрашивали последний раз.

    По журналу касаний, а не по своей таблице: журнал уже хранит, что именно
    ушло, и вторая копия однажды разошлась бы с первой молча — ровно как
    разошлась бы таблица перевода типов, от которой отказался ADR-0011.
    """
    # Группировка по псевдониму, а не по повтору выражения: одно и то же
    # обращение к JSONB SQLAlchemy рендерит с разными плейсхолдерами, и Postgres
    # считает их **разными** выражениями — «column touches.payload must appear
    # in the GROUP BY clause». Псевдоним однозначен и читается лучше.
    kind = Touch.payload["prompting"]["kind"].astext.label("kind")

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(kind, func.max(Touch.local_date))
                .where(Touch.sent_at.is_not(None), Touch.payload.has_key("prompting"))
                .group_by(literal_column("kind"))
            )
        ).all()
    return {str(row[0]): row[1] for row in rows if row[0] is not None}


async def _recent(limit: int) -> list[tuple[int, date]]:
    """Последние заданные вопросы: строка журнала и её сутки. Свежие первыми."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Touch.id, Touch.local_date)
                .where(Touch.sent_at.is_not(None), Touch.payload.has_key("prompting"))
                .order_by(Touch.local_date.desc())
                .limit(limit)
            )
        ).all()
    return [(int(row[0]), row[1]) for row in rows]


async def _answered(touch_ids: Sequence[int]) -> bool:
    """Ответил ли владелец хоть на один из этих вопросов.

    Ответ — это запись архива со ссылкой на строку журнала (`meta.answers`).
    Отдельной таблицы под связку нет: ответ и есть запись, а связь живёт
    рядом с ней, там же, где `ack_message_id`.
    """
    if not touch_ids:
        return False
    async with session_scope() as session:
        found = await session.scalar(
            select(Entry.id)
            .where(Entry.meta["answers"]["touch_id"].astext.in_([str(item) for item in touch_ids]))
            .limit(1)
        )
    return found is not None


async def _due(day: date) -> bool:
    """Спрашивать ли сегодня вообще.

    Три молчания подряд снижают частоту — **и ни одного слова об этом**
    (решение владельца № 7). Снижение считается по журналу и снимается само,
    как только владелец ответит: отдельного состояния «мы обиделись» в системе
    нет и быть не должно.
    """
    recent = await _recent(SILENCE_STREAK)
    if len(recent) < SILENCE_STREAK:
        return True
    if await _answered([touch_id for touch_id, _ in recent]):
        return True

    since = (day - recent[0][1]).days
    if since < QUIET_GAP_DAYS:
        logger.info("раскачка придержана: %s молчаний подряд, прошло %s дн.", len(recent), since)
        return False
    return True


# --- Что спросить сегодня ---


async def pick(
    *,
    now: datetime,
    timezone: str,
    model: LanguageModel | None = None,
) -> Question | None:
    """Вопрос на сегодня. `None` — спрашивать нечего или незачем.

    **Выбирается тот, которого дольше всех не было**, а не первый подходящий.
    Причина в разборе (§2.2): фиксированная последовательность вопросов
    превращается в чек-лист, а рефлексия по чек-листу выходит поверхностной.
    Заодно это и есть правило «вопрос не повторять» из решения владельца № 7:
    вчерашний вопрос сегодня оказывается последним в очереди сам, без отдельной
    проверки на повтор.

    Порядок набора участвует только при равенстве — когда ни один из вопросов
    ещё не задавали.
    """
    if not await _due(slots.local_date(now, timezone)):
        return None

    asked = await _asked_days()

    candidates: list[tuple[date, int, Question]] = []
    for order, (kind, item) in enumerate(SET.items()):
        found = await item.build(now, timezone)
        if found is None or not acceptable(found.text):
            continue
        candidates.append((asked.get(kind, date.min), order, found))

    if not candidates:
        logger.info("раскачке нечего спросить: подходящих данных в архиве нет")
        return None

    chosen = min(candidates, key=lambda row: (row[0], row[1]))[2]
    return await _written(chosen, model=model, now=now)


async def _written(found: Question, *, model: LanguageModel | None, now: datetime) -> Question:
    """Полный режим: тот же вопрос, написанный моделью.

    Ветка выключена по умолчанию (`WRITTEN`), но написана и вызывается отсюда,
    с пути тика: код за флагом, который никто не зовёт, — это `loops.is_stuck`,
    и стоила она проекту месяца неработавшей смены вопроса у петли.

    Отказ модели, придержанная квота и отвергнутый гвардом ответ дают один
    и тот же исход — прежний шаблон кода. Он поэтому не удаляется никогда.
    """
    if not WRITTEN:
        return found

    phrase = await writer.written(
        model,
        method=found.method,
        fallback=found.text,
        facts=found.facts,
        now=now,
    )
    if phrase is None or not acceptable(phrase):
        return found
    return Question(
        kind=found.kind,
        method=found.method,
        text=phrase,
        entry_ids=found.entry_ids,
        facts=found.facts,
    )


def payload(found: Question) -> dict[str, Any]:
    """Что кладётся в журнал касаний вместе с вопросом.

    Записи здесь — не украшение: по ним ответ владельца связывается с тем,
    на что он отвечал. `message_id` дописывается после отправки (см. `service`).
    """
    return {"kind": found.kind, "entry_ids": list(found.entry_ids)}


# --- Ответ владельца ---


@dataclass(frozen=True, slots=True)
class Answer:
    """То, на что владелец сейчас отвечает."""

    touch_id: int
    kind: str
    entry_ids: tuple[int, ...]

    def as_meta(self) -> dict[str, Any]:
        """Связь в том виде, в каком она ложится в `entries.meta`."""
        return {
            "touch_id": self.touch_id,
            "kind": self.kind,
            "entry_ids": list(self.entry_ids),
        }


async def answered(message_id: int) -> Answer | None:
    """Вопрос, на сообщение которого отвечают. `None` — это не ответ раскачке.

    Ищется по идентификатору сообщения в журнале — тем же приёмом, каким ответ
    на «когда напомнить?» ищется по `ack_message_id` в `entries.meta`. Своей
    таблицы ожидающих вопросов в проекте нет и здесь не заводится: состояние
    вопроса живёт в самой переписке.
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Touch.id, Touch.payload["prompting"]).where(
                    Touch.payload["prompting"]["message_id"].astext == str(message_id)
                )
            )
        ).first()

    if row is None:
        return None

    stored = row[1] or {}
    return Answer(
        touch_id=int(row[0]),
        kind=str(stored.get("kind", "")),
        entry_ids=tuple(int(item) for item in stored.get("entry_ids", [])),
    )


__all__ = [
    "BLOCKER",
    "INTENTION",
    "LAST_TIME",
    "LOOK_BACK",
    "MIN_LENGTH",
    "OWN_SIDE",
    "QUIET_TOPIC",
    "RETURNING",
    "SET",
    "SILENCE_STREAK",
    "SLOTS",
    "WRITTEN",
    "YESTERDAY",
    "Answer",
    "Item",
    "Question",
    "acceptable",
    "alone",
    "answered",
    "attach",
    "fits",
    "payload",
    "pick",
    "quote",
    "sample",
]
