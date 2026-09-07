"""Падение одного механизма не роняет весь тик.

**Найдено 7 сентября 2026, и найдено дорого.** Привычки упали на непринятой
миграции (`relation "habits" does not exist`), и вместе с ними не ушли утренняя
сводка, раскачка, всплывание и вечерний разбор — потому что `habits.remind_due`
зовётся в `service.tick` **до** цикла по слотам. Хуже самого пропуска то,
что не записалось молчание: журнал касаний пишет `nothing_to_say` строкой ровно
затем, чтобы отличать тишину системы от её смерти, — а пишет её тот же тик,
который упал. Снаружи это выглядело как «сегодня нечего сказать», и бот молчал
незаметно.

Здесь проверяется решение владельца от 7 сентября 2026 — три вещи сразу:

    1. упавший механизм не мешает остальным;
    2. упавший **слот** оставляет в журнале строку `failed` — молчание
       остаётся отличимым от смерти;
    3. строка `failed` **не сжигает сутки**: следующий тик пробует снова
       и переписывает её отправкой. Один сбой провайдера не стоит дня.

Ломается тут по-настоящему — подменой самого механизма, а не выдуманным флагом:
проверяется, что тик переживает **любое** исключение, а не заранее известное.

Требует поднятого Postgres: `just up`.
"""

from datetime import UTC, datetime

import pytest

from brain.core.digest import failures, habits, service, slots, touches
from brain.storage.models.touch import SilenceReason, TouchSlot
from tests.fakes import FakeNotifier
from tests.telegram_harness import CHAT_ID

pytestmark = pytest.mark.integration

TIMEZONE = "Europe/Rome"

# 09:30 по Риму — внутри утреннего окна и вне всех остальных.
MORNING = datetime(2026, 8, 20, 7, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


class BoomError(RuntimeError):
    """То, чем 7 сентября был `UndefinedTableError`. Роду отказа тик безразличен."""


async def _tick(notifier: FakeNotifier, now: datetime = MORNING) -> list[TouchSlot]:
    return await service.tick(
        now=now,
        timezone=TIMEZONE,
        chat_id=CHAT_ID,
        notifier=notifier,
        model=None,
    )


async def test_falling_habits_do_not_take_the_whole_tick_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ровно 7 сентября: привычки упали, слоты обязаны отработать.

    Архив пуст, поэтому утро правильно молчит — и **строка молчания обязана
    появиться**. Именно её отсутствие делало тишину неотличимой от смерти.
    """

    async def explode(**_: object) -> None:
        raise BoomError('relation "habits" does not exist')

    monkeypatch.setattr(habits, "remind_due", explode)
    notifier = FakeNotifier()

    await _tick(notifier)

    written = await touches.of_day(slots.local_date(MORNING, TIMEZONE))
    assert TouchSlot.MORNING in written, "молчание утра не записано — это и есть дыра"
    assert written[TouchSlot.MORNING].silence_reason is SilenceReason.NOTHING_TO_SAY


async def test_a_failing_mechanism_is_counted_where_the_owner_can_see_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отказ не проглатывается: он считается и виден в `/status`.

    Без этого `try/except` был бы просто глушителем, и мы поменяли бы громкое
    падение на тихое — то есть ухудшили бы положение, а не починили.
    """

    async def explode(**_: object) -> None:
        raise BoomError("привычки не отработали")

    monkeypatch.setattr(habits, "remind_due", explode)

    await _tick(FakeNotifier())

    counted = await failures.of_day(MORNING)
    assert counted, "отказ нигде не посчитан"
    assert any("привыч" in name for name, _ in counted.items())


async def test_a_failing_slot_leaves_a_failed_line_in_the_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Упавший слот оставляет след. «Строки нет» означает мёртвый воркер."""

    async def explode(*_: object, **__: object) -> tuple[str, dict[str, object]] | None:
        raise BoomError("сборка утра упала")

    monkeypatch.setattr(service, "_prepare", explode)

    await _tick(FakeNotifier())

    written = await touches.of_day(slots.local_date(MORNING, TIMEZONE))
    assert written[TouchSlot.MORNING].silence_reason is SilenceReason.FAILED


async def test_a_failed_slot_is_retried_and_the_line_is_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сбой стоит пяти минут, а не суток.

    Это половина решения владельца: строка `failed` обязана быть — иначе
    молчание неотличимо от смерти, — но она не имеет права занять слот
    навсегда, иначе один отказ провайдера съедает целый день сводки.
    """
    calls = {"count": 0}
    original = service._prepare

    async def once(*args: object, **kwargs: object) -> object:
        calls["count"] += 1
        if calls["count"] == 1:
            raise BoomError("первый раз не вышло")
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_prepare", once)
    notifier = FakeNotifier()

    await _tick(notifier)
    day = slots.local_date(MORNING, TIMEZONE)
    assert (await touches.of_day(day))[TouchSlot.MORNING].silence_reason is SilenceReason.FAILED

    # Второй тик — тот же слот, те же сутки. На пустом архиве сказать нечего,
    # и строка обязана смениться на честное «нечего сказать».
    await _tick(notifier)
    again = (await touches.of_day(day))[TouchSlot.MORNING]
    assert again.silence_reason is SilenceReason.NOTHING_TO_SAY


async def test_a_settled_slot_is_not_touched_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Перезапись касается **только** строки `failed`.

    Иначе правка «пробуем ещё раз» отменила бы защиту от двойной отправки,
    ради которой слот и занимается до отправки.
    """
    await touches.record_silence(
        TouchSlot.MORNING,
        slots.local_date(MORNING, TIMEZONE),
        reason=SilenceReason.QUIET,
    )

    async def explode(*_: object, **__: object) -> None:
        raise BoomError("сюда доходить нельзя")

    monkeypatch.setattr(service, "_prepare", explode)

    assert await _tick(FakeNotifier()) == []
    day = slots.local_date(MORNING, TIMEZONE)
    assert (await touches.of_day(day))[TouchSlot.MORNING].silence_reason is SilenceReason.QUIET


async def test_falling_debt_settlement_does_not_take_the_tick_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Долг фиксируется первым в тике — значит он же первым мог всё уронить."""
    from brain.core.digest import evening

    async def explode(*_: object, **__: object) -> None:
        raise BoomError("долг не зафиксировался")

    monkeypatch.setattr(evening, "settle_debt", explode)

    await _tick(FakeNotifier())

    day = slots.local_date(MORNING, TIMEZONE)
    assert TouchSlot.MORNING in await touches.of_day(day)
