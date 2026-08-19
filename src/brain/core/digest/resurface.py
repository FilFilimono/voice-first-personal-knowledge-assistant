"""Всплывание старого: одна запись из архива, без повода.

`vision.md` §5.3: «Одна из десяти попадёт в текущий контекст и сработает».
Механизм намеренно тупой — никакого подбора «по релевантности», иначе он начнёт
показывать то, о чём владелец и так думает.

Четыре правила, все оттуда:

    порог возраста     max(14 дней, возраст старейших 25% архива)
    длина              короче 100 символов не всплывает
    приватность        приватные **участвуют**
    повторы            одна и та же не чаще раза в 90 дней

**Про адаптивный порог.** Нижняя граница в 14 дней — то, из-за чего механизм
молчит первые две недели. Отдельного календарного запрета «первые две недели
молчим» в проекте нет: молчание получается из порога по данным, и снимается
оно тогда, когда доставать действительно есть что (ADR-0013).

Верхняя часть формулы нужна на зрелом архиве: через год «старше четырнадцати
дней» означало бы «почти всё», и всплывание перестало бы доставать именно
старое.

**Про приватные.** Это единственный механизм Фазы 4, который их трогает,
и трогает законно: наружу ничего не уходит, границы переписки владельца
с собственным ботом он не покидает. Инвариант 5 про внешние сервисы,
а не про то, что бот показывает хозяину.
"""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select

from brain.core.digest import touches
from brain.storage.db import session_scope
from brain.storage.models.entry import Entry
from brain.storage.models.transcript import Transcript

# Нижняя граница возраста. Она же — причина, по которой механизм молчит
# на молодом архиве.
MIN_AGE = timedelta(days=14)

# Какая доля архива считается «старейшей». 0.75 по возрасту — это те 25%,
# что старше всех.
OLDEST_QUARTILE = 0.75

# Короче этого не всплывает: «ок», «в 14:50», проверка бота — там нечего
# доставать, а вытащенный обрывок читается как сбой.
MIN_LENGTH = 100

# Одна и та же запись не чаще раза в 90 дней.
COOLDOWN = timedelta(days=90)


@dataclass(frozen=True, slots=True)
class Old:
    """Запись, годная к всплыванию."""

    entry_id: int
    text: str
    created_at: datetime
    is_private: bool


async def eligible(now: datetime) -> list[Old]:
    """Все записи, которые сегодня годятся. Пусто — доставать нечего.

    Отдельно от `pick`, чтобы правила отбора можно было проверить целиком:
    случайный выбор из непроверяемого множества проверяет только случайность.
    """
    cutoff = await _cutoff(now)
    if cutoff is None:
        return []

    seen = await touches.resurfaced_since((now - COOLDOWN).date())

    # Текст берётся так же, как везде: расшифровка, если она есть, иначе
    # сырой текст. У приватной записи расшифровки не бывает — в приватном
    # режиме голосовые не принимаются (ADR-0002).
    latest = (
        select(Transcript.text)
        .where(Transcript.source_entry_id == Entry.id)
        .order_by(Transcript.id.desc())
        .limit(1)
        .correlate(Entry)
        .scalar_subquery()
    )
    body = func.coalesce(latest, Entry.raw_text)

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Entry.id, body, Entry.created_at, Entry.is_private).where(
                    Entry.created_at <= cutoff,
                    func.length(body) >= MIN_LENGTH,
                )
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
        if int(row[0]) not in seen
    ]


async def pick(now: datetime) -> Old | None:
    """Одна старая запись — случайная. `None`, если доставать нечего.

    Случайная, а не «самая подходящая»: подбор по близости к сегодняшним
    записям показывал бы то, о чём владелец и так думает, а смысл механизма
    в обратном (`vision.md` §5.3).
    """
    candidates = await eligible(now)
    if not candidates:
        return None
    return random.choice(candidates)


async def _cutoff(now: datetime) -> datetime | None:
    """Позже какого момента запись уже не «старая». `None` — архив пуст.

    Порог `max(14 дней, возраст старейших 25%)` из `vision.md` §5.3, но в виде
    даты, а не длительности: перцентиль считается прямо по `created_at`, без
    перевода в секунды. Так короче и, главное, читаемо — «граница старейшей
    четверти» это и есть дата, а не число секунд.

    `percentile_disc` вместо `percentile_cont` не случайно: непрерывный вариант
    интерполирует между значениями и над timestamptz не определён вовсе,
    а дискретный возвращает настоящую дату из архива.
    """
    async with session_scope() as session:
        boundary = await session.scalar(
            select(
                func.percentile_disc(1 - OLDEST_QUARTILE).within_group(Entry.created_at.asc())
            ).select_from(Entry)
        )

    if boundary is None:
        return None
    # Оба условия сразу: и в старейшей четверти, и не моложе четырнадцати дней.
    oldest_quarter: datetime = boundary
    return min(oldest_quarter, now - MIN_AGE)
