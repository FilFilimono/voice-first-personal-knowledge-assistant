"""Кнопка паузы и «сегодня выходной» — на живой базе.

Тишина глушит инициативные касания **и утреннюю сводку**, но никогда
не глушит напоминания: система не молчит о том, о чём её просили сами
(`vision.md` §7). Здесь проверяется первая половина; вторая — тем, что
напоминания в этот модуль не заглядывают вовсе.

Требует поднятого Postgres: `just up`.
"""

from datetime import UTC, datetime, timedelta

import pytest

from brain.core.digest import quiet

pytestmark = pytest.mark.integration

ROME = "Europe/Rome"
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


async def test_by_default_nobody_is_silent() -> None:
    """Пустой журнал — обычный режим, а не тишина."""
    assert not await quiet.is_quiet(NOW)
    assert await quiet.until(NOW) is None


async def test_pause_lasts_a_week() -> None:
    """«Замолчи на неделю» — ровно неделя, а не «пока не передумаю»."""
    ends = await quiet.pause(NOW)

    assert ends == NOW + quiet.PAUSE
    assert await quiet.is_quiet(NOW + timedelta(days=6))
    assert not await quiet.is_quiet(NOW + timedelta(days=8))


async def test_resume_lifts_the_pause_immediately() -> None:
    await quiet.pause(NOW)
    await quiet.resume(NOW + timedelta(days=1))

    assert not await quiet.is_quiet(NOW + timedelta(days=1, seconds=1))


async def test_day_off_ends_at_the_owners_midnight_not_the_servers() -> None:
    """«Сегодня выходной» кончается в полночь там, где живёт владелец.

    В 23:00 по Риму в UTC ещё 21:00 того же дня. Выходной, посчитанный по UTC,
    прихватил бы два часа следующего дня — и утренняя сводка следующего утра
    осталась бы цела, а вот вечер сегодняшнего дня уже нет.
    """
    # 22:00 по Риму 20 августа — это 20:00 UTC.
    evening = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)

    ends = await quiet.day_off(evening, ROME)

    # Полночь по Риму 21 августа — это 22:00 UTC двадцатого.
    assert ends == datetime(2026, 8, 20, 22, 0, tzinfo=UTC)
    assert await quiet.is_quiet(evening + timedelta(hours=1))
    assert not await quiet.is_quiet(evening + timedelta(hours=3))


async def test_the_latest_row_wins() -> None:
    """Состояние — последняя строка журнала, как у приватного режима.

    Проверяется именно порядок, а не «есть ли активная строка»: пауза после
    снятия обязана снова включить тишину, а не проиграть более ранней строке.
    """
    await quiet.pause(NOW)
    await quiet.resume(NOW + timedelta(hours=1))
    await quiet.pause(NOW + timedelta(hours=2))

    assert await quiet.is_quiet(NOW + timedelta(hours=3))


async def test_day_off_after_a_pause_shortens_the_silence() -> None:
    """Последняя строка выигрывает, даже если она короче предыдущей.

    Это не ошибка, а поведение журнала: владелец нажал «сегодня выходной»
    последним, значит он про сегодня и говорит.
    """
    await quiet.pause(NOW)
    await quiet.day_off(NOW, ROME)

    assert not await quiet.is_quiet(NOW + timedelta(days=2))
