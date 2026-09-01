"""Срез по темам за период: о чём владелец думал и что можно вспомнить.

`vision.md` §5.4, вторая половина: «Ты думал о таких темах за неделю: книги,
работа, учёба. Хочешь, вспомним какую-то подробно?»

**Главное правило этого модуля — конкретную старую запись без спроса
не выдавать.** Срез называет темы; конкретная запись приходит только после
того, как владелец назвал тему сам. Причина прямая: выбор всегда к месту,
а случайная запись полугодовой давности к месту может не быть. Этим срез
отличается от всплывания (§5.3), которое достаёт запись именно без повода
и именно случайную.

**Считать нечего.** Темы уже есть: их размечает модель по каждой записи
и раз в N записей переобобщает пачкой (ADR-0011). Здесь только выборка
за период — ни одного вызова модели, как и у связей.

**Молчание — обычный исход.** Одна тема за неделю срезом не является: «ты думал
о теме „разработка“» — это не срез, а пересказ единственной строки. Порог
поэтому по данным, а не по календарю: сказать есть что тогда, когда тем
набралось хотя бы две.
"""

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select

from brain.core.digest.resurface import MIN_LENGTH, Old
from brain.core.memory.repository import display_text
from brain.core.ports import LanguageModel
from brain.core.present import recall
from brain.storage.db import session_scope
from brain.storage.models.entry import Entry
from brain.storage.models.topic import EntryTopic, Topic, normalize

logger = logging.getLogger(__name__)

# Меньше двух тем — не срез. См. модуль.
MIN_TOPICS = 2

# Сколько тем показывать. Шесть — это весь список тем живого архива на 27.08.2026
# (30 записей «разработка», 18 «CV чек», 12 «учёба», 4 «курсовая», 4 «фильмы»,
# 1 «здоровье и фитнес»), то есть сегодня потолок ни во что не упирается.
# Он существует ради архива через год: список из сорока тем — это не срез,
# а оглавление.
MAX_TOPICS = 6

# Насколько старой должна быть запись, чтобы её имело смысл «вспоминать».
# Сегодняшнее владелец помнит и без бота — та же причина, что у возрастного
# зазора у связей.
MIN_AGE = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class Counted:
    """Тема и сколько записей она собрала за период."""

    name: str
    entries: int


@dataclass(frozen=True, slots=True)
class Slice:
    """Срез по темам за период."""

    topics: list[Counted]
    since: datetime
    until: datetime


async def collect(now: datetime, *, days: int) -> Slice | None:
    """Темы за период. `None` — срезом это назвать нельзя.

    Считается по времени записи, а не по времени разметки: владельца
    интересует, о чём он думал на неделе, а не когда воркер разгрёб очередь.
    """
    since = now - timedelta(days=days)

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Topic.name, func.count().label("entries"))
                .select_from(EntryTopic)
                .join(Topic, Topic.id == EntryTopic.topic_id)
                .join(Entry, Entry.id == EntryTopic.source_entry_id)
                .where(Entry.created_at >= since, Entry.created_at <= now)
                .group_by(Topic.name)
                .order_by(func.count().desc(), Topic.name)
                .limit(MAX_TOPICS)
            )
        ).all()

    if len(rows) < MIN_TOPICS:
        logger.info("срез по темам не собирается: тем за %s дн. — %s", days, len(rows))
        return None

    return Slice(
        topics=[Counted(name=str(row[0]), entries=int(row[1])) for row in rows],
        since=since,
        until=now,
    )


async def text(model: LanguageModel | None, found: Slice, *, days: int, now: datetime) -> str:
    """Срез словами. Список тем и приглашение — код, первая строка — см. `recall`.

    Приглашение называет команду прямо и с примером: «спросите» без образца
    оставляет владельца гадать, как именно называется тема, — а темы
    нормализованы («учёба» и «учеба» — одна), и угадывать регистр не нужно.
    """
    listing = ", ".join(f"{topic.name} ({topic.entries})" for topic in found.topics)
    return await recall.topics_text(
        model,
        headline=f"За {_period(days)} вы возвращались к этим темам.",
        listing=listing,
        invitation=f"Вспомнить какую-то подробно: /recall {found.topics[0].name}",
        now=now,
    )


def _period(days: int) -> str:
    """Период по-русски. Неделя — это неделя, а не «7 дн.»."""
    return "неделю" if days == 7 else f"{days} дн."


async def pick(name: str, now: datetime) -> Old | None:
    """Одна запись названной темы. `None` — вспоминать нечего.

    Случайная — как у всплывания (§5.3), и по той же причине: «самая
    подходящая» показывала бы то, о чём владелец и так думает. Разница
    в поводе: тему назвал он сам, поэтому случайность внутри темы к месту.

    Приватные участвуют. Наружу ничего не уходит — это переписка владельца
    с собственным ботом, и §5.3 разрешает их прямым текстом; в связях
    (`retrieval/links`) правило обратное, потому что там запись показывается
    без спроса.
    """
    candidates = await eligible(name, now)
    if not candidates:
        return None
    return random.choice(candidates)


async def eligible(name: str, now: datetime) -> list[Old]:
    """Все записи темы, годные к «вспомним подробно».

    Отдельно от `pick` — как у всплывания: случайный выбор из непроверяемого
    множества проверяет только случайность.

    Тема ищется по `slug`, а не по имени: «учёба» и «учеба» — одна тема,
    и владелец, набирая команду с телефона, о нормализации знать не обязан.
    """
    body = display_text()
    slug = normalize(name)

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Entry.id, body, Entry.created_at, Entry.is_private)
                .select_from(EntryTopic)
                .join(Topic, Topic.id == EntryTopic.topic_id)
                .join(Entry, Entry.id == EntryTopic.source_entry_id)
                .where(
                    Topic.slug == slug,
                    Entry.created_at <= now - MIN_AGE,
                    func.length(body) >= MIN_LENGTH,
                )
                .order_by(Entry.id)
            )
        ).all()

    return [
        Old(
            entry_id=int(row[0]),
            text=str(row[1]),
            created_at=row[2],
            is_private=bool(row[3]),
        )
        for row in rows
    ]


async def names(now: datetime, *, days: int) -> list[str]:
    """Темы периода списком — чтобы сказать, что можно вспомнить.

    Нужна там, где владелец назвал тему, которой нет: список вместо «не нашёл»
    превращает тупик в подсказку.
    """
    found = await collect(now, days=days)
    return [] if found is None else [topic.name for topic in found.topics]
