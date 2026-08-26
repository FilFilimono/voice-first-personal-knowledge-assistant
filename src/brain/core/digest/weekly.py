"""Недельное ревью — то, что включается по данным, а не по календарю.

`vision.md` §6 обещает связный рассказ о неделе из девяти пунктов. Шесть из них
требуют журналов решений и ошибок, приоритетов недели и противоречий — это Фазы
6 и 7. Честно считаются три: о чём думал (темы), что висит (дела и петли),
одна подсказка.

**Отсюда решение включать по данным, а не по воскресенью.** `vision.md` §1.1
говорит прямо: система с криво сделанным ревью провалена, даже если всё
остальное безупречно. Ревью, собранное из недели в десять записей про отладку
бота, — это ревью про отладку бота, и оно хуже молчания. Порог снимает вопрос:
пока материала нет, воскресенье проходит тихо, а `/status` объясняет, чего
не хватает.

**Связный рассказ остаётся Фазой 6**, где записан дорогой тир. Здесь ревью —
это сведённые данные, и оно намеренно не притворяется рассказом.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select

from brain.core.digest import commitments as commitment_store
from brain.core.digest import loops
from brain.storage.db import session_scope
from brain.storage.models.commitment import Commitment, CommitmentState
from brain.storage.models.entry import Entry, EntryKind
from brain.storage.models.loop import Loop
from brain.storage.models.override import EntryOverride, OverrideAction
from brain.storage.models.private_mode import PrivateModeChange
from brain.storage.models.reminder import Reminder
from brain.storage.models.topic import EntryTopic, Topic

WEEK = timedelta(days=7)

# Порог, ниже которого ревью не собирается. Число **выбрано, а не измерено**,
# и это надо знать: данных о том, сколько записей делает неделю, пока нет —
# архиву четыре дня. Десять — это примерно полтора обычных дня; ниже него
# «неделя» состоит из одного вечера, и пересказывать нечего.
#
# Пересматривается после двух недель использования, вместе с часами слотов.
MIN_ENTRIES = 10

# Сколько разделов должны быть непустыми. Одного мало: ревью из единственной
# строки «записал 12 раз» — это счётчик, а счётчики запрещены (§7.0, принцип 5).
MIN_SECTIONS = 2


@dataclass(frozen=True, slots=True)
class Weekly:
    """Содержание недельного ревью."""

    entries: int
    voice: int
    topics: list[tuple[str, int]]
    done: list[Commitment]
    dropped: list[Commitment]
    open_commitments: int
    open_loops: int
    oldest_loop_days: int | None
    hint: str | None

    @property
    def sections(self) -> int:
        """Сколько разделов есть что сказать."""
        return sum(
            [
                bool(self.topics),
                bool(self.done or self.dropped),
                bool(self.open_commitments or self.open_loops),
            ]
        )


# Подсказки о неиспользуемых возможностях (`vision.md` §6.8, §14). Каждая
# проверяется **по данным**, а не по журналу действий: журнала действий
# в проекте нет, и заводить его ради подсказки было бы дороже подсказки.
#
# Про `/find` подсказки нет намеренно: поиск нигде не оставляет следа, и понять,
# пользовались им или нет, неоткуда. Соврать «вы ни разу не искали» хуже,
# чем промолчать.
HINTS: list[tuple[str, str]] = [
    ("reminders", "Скажите «напомни завтра в 10 …» — и я напомню. Разбирает код, не модель."),
    ("voice", "Голосовые можно просто наговаривать: расшифрую и разберу сам."),
    (
        "loops",
        "Под разбором есть кнопка «Открытый вопрос» — так заводится петля, "
        "к которой я буду возвращаться.",
    ),
    (
        "reclassify",
        "Если разметка мимо — кнопка «Не туда» под разбором. Поправка идёт в оценку качества.",
    ),
    ("private", "/private on — записи не уходят наружу вовсе."),
]


async def collect(now: datetime) -> Weekly | None:
    """Собрать ревью за неделю. `None` — материала меньше порога.

    `None` означает именно «рано», а не «сломалось»: причина уходит в журнал
    касаний, и `/status` показывает её словами.
    """
    since = now - WEEK

    async with session_scope() as session:
        # Архив должен быть старше недели. Иначе «неделя» состоит из одного дня,
        # и пересказывать в ней нечего — а `/status` при этом обещает владельцу
        # ровно это условие («пересказ недели — нужна неделя записей»). Найдено
        # повтором по живому архиву 19 августа: без проверки ревью уходило бы
        # в первое же воскресенье, на архиве в один день.
        oldest = await session.scalar(select(func.min(Entry.created_at)))
        if oldest is None or oldest > since:
            return None

        entries = (
            await session.scalar(
                select(func.count()).select_from(Entry).where(Entry.created_at >= since)
            )
            or 0
        )
        voice = (
            await session.scalar(
                select(func.count())
                .select_from(Entry)
                .where(Entry.created_at >= since, Entry.kind == EntryKind.VOICE)
            )
            or 0
        )
        topics = (
            await session.execute(
                select(Topic.name, func.count())
                .join(EntryTopic, EntryTopic.topic_id == Topic.id)
                .join(Entry, Entry.id == EntryTopic.source_entry_id)
                .where(Entry.created_at >= since)
                .group_by(Topic.name)
                .order_by(func.count().desc(), Topic.name)
            )
        ).all()

    review = Weekly(
        entries=int(entries),
        voice=int(voice),
        topics=[(str(name), int(count)) for name, count in topics],
        done=await commitment_store.closed_between(since, now, state=CommitmentState.DONE),
        dropped=await commitment_store.closed_between(since, now, state=CommitmentState.DROPPED),
        open_commitments=await commitment_store.open_count(now),
        open_loops=await loops.open_count(),
        oldest_loop_days=_days(await loops.oldest_open_age(now)),
        hint=await _hint(),
    )

    if review.entries < MIN_ENTRIES or review.sections < MIN_SECTIONS:
        return None
    return review


def _days(age: timedelta | None) -> int | None:
    return None if age is None else age.days


async def _hint() -> str | None:
    """Первая возможность, которой владелец ни разу не пользовался.

    Порядок в списке — порядок полезности: напоминания и голос дают отдачу
    в первый же день, приватность — по случаю.
    """
    async with session_scope() as session:
        used = {
            "reminders": await session.scalar(select(Reminder.id).limit(1)),
            "voice": await session.scalar(
                select(Entry.id).where(Entry.kind == EntryKind.VOICE).limit(1)
            ),
            "loops": await session.scalar(select(Loop.id).limit(1)),
            "reclassify": await session.scalar(
                select(EntryOverride.id)
                .where(EntryOverride.action == OverrideAction.RECLASSIFY)
                .limit(1)
            ),
            "private": await session.scalar(select(PrivateModeChange.id).limit(1)),
        }

    for key, text in HINTS:
        if not used.get(key):
            return text
    return None
