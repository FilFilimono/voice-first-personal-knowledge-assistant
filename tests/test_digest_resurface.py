"""Всплывание старого — `vision.md` §5.3.

Здесь проверяются все четыре правила оттуда: адаптивный порог возраста, отсечка
по длине, участие приватных записей и запрет повторять одну и ту же чаще раза
в 90 дней.

Записи вставляются сырым SQL с заданным `created_at`: приём таких дат не знает,
а весь механизм — про возраст. `entries` при этом только на вставку, инвариант 3
цел — тест ничего не правит и не удаляет.

Требует поднятого Postgres: `just up`.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from brain.core.digest import resurface, touches
from brain.storage.db import session_scope
from brain.storage.models.touch import TouchSlot

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
LONG = "мысль про то, что делать дальше, " * 5  # заведомо длиннее ста символов


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


async def _entry(body: str, *, age: timedelta, private: bool = False) -> int:
    """Запись с заданным возрастом. Сырым SQL — приём таких дат не принимает."""
    async with session_scope() as session:
        entry_id = await session.scalar(
            text(
                "INSERT INTO entries "
                "(created_at, source, kind, raw_text, is_private, timezone, dedup_key, meta) "
                "VALUES (:created_at, 'telegram', 'text', :body, :private, 'Europe/Rome', "
                ":key, '{}'::jsonb) RETURNING id"
            ),
            {
                "created_at": NOW - age,
                "body": body,
                "private": private,
                "key": f"{body[:20]}:{age.days}:{private}",
            },
        )
    return int(entry_id or 0)


async def test_young_archive_surfaces_nothing() -> None:
    """Порог не ниже четырнадцати дней, даже если архив весь «старый» по-своему.

    Это и есть причина, по которой механизм молчит первые две недели: отдельного
    календарного запрета в проекте нет, молчание получается из порога по данным
    (`vision.md` §7, правило про первые две недели).
    """
    await _entry(LONG, age=timedelta(days=3))
    await _entry(LONG, age=timedelta(days=10))

    assert await resurface.pick(NOW) is None


async def test_short_entries_never_surface() -> None:
    """Короче ста символов — нечего доставать: «ок», «в 14:50», проверка бота."""
    await _entry("ок", age=timedelta(days=200))

    assert await resurface.pick(NOW) is None


async def test_private_entries_do_surface() -> None:
    """Приватные участвуют: наружу ничего не уходит, это свой архив и свой бот.

    Прямая цитата из `vision.md` §5.3. Здесь это единственный механизм Фазы 4,
    который трогает приватные записи, — и трогает законно, потому что не выходит
    за пределы переписки владельца с собственным ботом.
    """
    await _entry(LONG, age=timedelta(days=200), private=True)

    picked = await resurface.pick(NOW)

    assert picked is not None
    assert picked.is_private


async def test_the_same_entry_does_not_surface_twice_in_ninety_days() -> None:
    entry_id = await _entry(LONG, age=timedelta(days=200))
    await touches.record_sent(TouchSlot.DAY, date(2026, 7, 1), payload={"entry_id": entry_id})

    assert await resurface.pick(NOW) is None


async def test_it_surfaces_again_after_ninety_days() -> None:
    entry_id = await _entry(LONG, age=timedelta(days=400))
    long_ago = (NOW - timedelta(days=120)).date()
    await touches.record_sent(TouchSlot.DAY, long_ago, payload={"entry_id": entry_id})

    picked = await resurface.pick(NOW)

    assert picked is not None
    assert picked.entry_id == entry_id


async def test_threshold_follows_the_archive_when_it_grows_old() -> None:
    """Порог адаптивный: на зрелом архиве всплывает старейшая четверть.

    Без этого через год «старше четырнадцати дней» означало бы «почти всё»,
    и всплывание перестало бы доставать именно старое.
    """
    for days in (400, 380, 360, 340, 60, 50, 40, 30):
        await _entry(LONG + str(days), age=timedelta(days=days))

    eligible = {item.entry_id for item in await resurface.eligible(NOW)}
    ages = {item.entry_id: (NOW - item.created_at).days for item in await resurface.eligible(NOW)}

    # Восемь записей: старейшая четверть — это две самые старые.
    assert len(eligible) == 2
    assert sorted(ages.values()) == [380, 400]
