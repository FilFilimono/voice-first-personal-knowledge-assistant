"""Журнал касаний и бюджет — на живой базе.

Проверяется главным образом то, ради чего журнал и заведён: **молчание должно
быть записано.** Всё остальное здесь — защита от двойной отправки и арифметика
бюджета.

Требует поднятого Postgres: `just up`.
"""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from brain.core.digest import touches
from brain.storage.models.touch import SilenceReason, TouchCategory, TouchSlot

pytestmark = pytest.mark.integration

DAY = date(2026, 8, 20)


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


async def test_silence_is_a_row_not_an_absence() -> None:
    """Ради этого журнал и существует.

    Молчание системы и её смерть снаружи выглядят одинаково — так пряталась
    каждая из восьми дыр Фаз 1–2. Строка с причиной делает их различимыми.
    """
    written = await touches.record_silence(
        TouchSlot.MORNING, DAY, reason=SilenceReason.NOTHING_TO_SAY
    )

    assert written
    recorded = await touches.of_day(DAY)
    assert TouchSlot.MORNING in recorded
    assert recorded[TouchSlot.MORNING].silence_reason is SilenceReason.NOTHING_TO_SAY
    assert recorded[TouchSlot.MORNING].sent_at is None


async def test_slot_is_written_once_a_day_even_if_the_tick_repeats() -> None:
    """Тик просыпается каждые пять минут, а сводка бывает одна.

    Проверка «а не отправляли ли уже» в коде это не закрыла бы: два процесса
    успели бы прочитать пусто. Держит база, как и `entries.dedup_key`.
    """
    assert await touches.record_sent(TouchSlot.MORNING, DAY, payload={"n": 1})
    assert not await touches.record_sent(TouchSlot.MORNING, DAY, payload={"n": 2})
    assert not await touches.record_silence(
        TouchSlot.MORNING, DAY, reason=SilenceReason.NOTHING_TO_SAY
    )

    recorded = await touches.of_day(DAY)
    assert recorded[TouchSlot.MORNING].payload == {"n": 1}


async def test_budget_counts_initiative_only() -> None:
    """Утренняя сводка вне бюджета — она про сегодняшний день, а не про архив."""
    await touches.record_sent(TouchSlot.MORNING, DAY)
    await touches.record_sent(TouchSlot.DAY, DAY)

    budget = await touches.budget(DAY)

    assert budget.spent == 1
    assert budget.left == touches.INITIATIVE_BUDGET - 1
    assert not budget.exhausted


async def test_silence_does_not_spend_the_budget() -> None:
    """Промолчали — значит не потратили. Иначе тишина съедала бы право сказать."""
    await touches.record_silence(TouchSlot.DAY, DAY, reason=SilenceReason.NOTHING_TO_SAY)

    assert (await touches.budget(DAY)).spent == 0


async def test_budget_is_exhausted_by_three_initiative_touches() -> None:
    await touches.record_sent(TouchSlot.DAY, DAY)
    await touches.record_sent(TouchSlot.EVENING, DAY)
    await touches.record_sent(TouchSlot.WEEKLY, DAY)

    budget = await touches.budget(DAY)

    assert budget.spent == touches.INITIATIVE_BUDGET
    assert budget.exhausted


async def test_categories_follow_vision() -> None:
    """Категория выводится из слота, а не задаётся вызывающим.

    Иначе то же касание однажды окажется записанным в две разные категории,
    и бюджет начнёт считать не то.
    """
    assert touches.CATEGORIES[TouchSlot.MORNING] is TouchCategory.MORNING
    assert touches.CATEGORIES[TouchSlot.DAY] is TouchCategory.INITIATIVE
    assert touches.CATEGORIES[TouchSlot.EVENING] is TouchCategory.INITIATIVE
    assert touches.CATEGORIES[TouchSlot.WEEKLY] is TouchCategory.INITIATIVE


async def test_row_cannot_be_both_sent_and_silent(database: str) -> None:
    """Строка, у которой нет ни отправки, ни причины молчания, — это «неизвестно».

    Ровно то состояние, ради устранения которого таблица заведена, поэтому
    держит его база, а не договорённость.
    """
    engine = create_async_engine(database)
    insert = text(
        "INSERT INTO touches (slot, category, local_date) VALUES ('morning', 'morning', :day)"
    )
    try:
        async with engine.begin() as connection:
            with pytest.raises(Exception, match="ck_touches"):
                await connection.execute(insert, {"day": DAY})
    finally:
        await engine.dispose()


async def test_resurfaced_entries_are_remembered_by_the_journal() -> None:
    """Одна и та же запись не всплывает чаще раза в 90 дней (`vision.md` §5.3).

    Отдельной таблицы под это нет намеренно: журнал касаний уже хранит,
    что именно ушло, и вторая копия однажды разойдётся с первой.
    """
    await touches.record_sent(TouchSlot.DAY, DAY, payload={"entry_id": 17})
    await touches.record_sent(TouchSlot.DAY, date(2026, 8, 21), payload={"entry_id": 42})

    assert await touches.resurfaced_since(date(2026, 8, 1)) == {17, 42}
    assert await touches.resurfaced_since(date(2026, 8, 21)) == {42}


async def test_last_touch_of_a_slot_is_the_latest_one() -> None:
    """`/status` спрашивает «а что было со сводкой сегодня» — это последняя строка."""
    await touches.record_silence(TouchSlot.MORNING, date(2026, 8, 19), reason=SilenceReason.QUIET)
    await touches.record_sent(TouchSlot.MORNING, DAY)

    last = await touches.last(TouchSlot.MORNING)

    assert last is not None
    assert last.local_date == DAY
    assert last.sent_at is not None


async def test_sent_row_stamps_the_time_it_went_out() -> None:
    before = datetime.now(UTC)
    await touches.record_sent(TouchSlot.EVENING, DAY)

    recorded = await touches.of_day(DAY)
    sent_at = recorded[TouchSlot.EVENING].sent_at

    assert sent_at is not None
    assert sent_at >= before
