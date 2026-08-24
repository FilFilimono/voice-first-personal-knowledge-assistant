"""Вечерний разбор и долг, который не копится.

Два правила из `vision.md` §7.0 проверяются именно здесь, потому что именно
здесь их легче всего нарушить:

- **вечер приходит только с отдачей.** Нет спорных записей — нет и разбора;
- **долг фиксируется молча.** Сообщение «зафиксировал за тебя N записей» —
  это ретроспективный упрёк, запрещённый принципом 2.

Требует поднятого Postgres: `just up`.
"""

from datetime import UTC, datetime, timedelta

import pytest

from brain.core import ingest
from brain.core.digest import evening
from brain.core.enrich import repository as enrichment
from brain.core.enrich.units import MergedUnit
from brain.core.memory import overrides
from brain.core.memory.unit_of_work import capture_transaction
from brain.storage.models.annotation import AnnotationState, AnnotationType
from brain.storage.models.override import OverrideAction

pytestmark = pytest.mark.integration

TIMEZONE = "Europe/Rome"
CHAT_ID = 111
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


def _days_after_capture(days: int) -> datetime:
    """Момент через N дней после того, как запись реально создана.

    **Отсчёт от настоящих часов, а не от `NOW`, и это не мелочь.** Отсечка
    долга сравнивается с `entries.created_at`, а он приходит
    из `server_default=func.now()` — то есть с часов сервера. Переданный
    приёму `now=NOW` на него не влияет никак: `entries` не правится
    (инвариант 3), значение ставит база в момент вставки.

    Так эти три теста и сломались. С `NOW = 20 августа` отсечка
    `settle_debt(NOW + 8 дней) - 7 дней` приходилась на 21 августа, и пока
    настоящая дата была раньше, запись оказывалась старше отсечки — случайно.
    21 августа 2026 тесты покраснели молча и оставались красными, пока их
    не разобрали руками 24-го. Подгонять константу под сегодняшний день
    нельзя: это вернёт ту же поломку через неделю.
    """
    return datetime.now(UTC) + timedelta(days=days)


async def _annotated(body: str, state: AnnotationState, message_id: int) -> int:
    async with capture_transaction() as uow:
        result = await ingest.capture_text(
            uow,
            ingest.IncomingText(
                chat_id=CHAT_ID,
                message_id=message_id,
                ack_message_id=message_id + 1,
                text=body,
            ),
            is_private=False,
            timezone=TIMEZONE,
            now=NOW,
        )
    entry_id = result.entry_id
    assert entry_id is not None

    await enrichment.save_annotation(
        entry_id=entry_id,
        type_=AnnotationType.THOUGHT,
        alternative_type=AnnotationType.TASK if state is AnnotationState.TWO_HYPOTHESES else None,
        state=state,
        entities=[],
        provider="test",
        model="test",
        existing_topic_ids=[],
        new_topics=[],
        units=[
            MergedUnit(
                start=0,
                end=len(body),
                type=AnnotationType.THOUGHT,
                alternative_type=None,
                state=state,
            )
        ],
        transcript_id=None,
        text=body,
    )
    return entry_id


async def test_nothing_disputed_means_nothing_to_show() -> None:
    """Уверенная разметка вечер не поднимает.

    Это и есть «вечер приходит только с отдачей»: на живом архиве спорных вышло
    четыре за четыре дня, значит тишина здесь — обычный режим, а не сбой.
    """
    await _annotated("обычная мысль", AnnotationState.SURE, message_id=10)

    assert await evening.disputed() == []


async def test_two_hypotheses_and_unclear_are_one_queue() -> None:
    """Для владельца это одна очередь, а не две: обе означают «посмотри»."""
    first = await _annotated("спорная", AnnotationState.TWO_HYPOTHESES, message_id=10)
    second = await _annotated("непонятая", AnnotationState.UNCLEAR, message_id=20)

    assert {item.entry_id for item in await evening.disputed()} == {first, second}


async def test_settled_entries_leave_the_queue() -> None:
    """Разобранное не возвращается — иначе вечер спрашивал бы одно и то же."""
    entry_id = await _annotated("спорная", AnnotationState.TWO_HYPOTHESES, message_id=10)

    await evening.settle(entry_id, action=OverrideAction.CONFIRM)

    assert await evening.disputed() == []


async def test_correcting_the_type_also_settles_it() -> None:
    """«Не туда» — это тоже ответ на вопрос вечера, и более полный, чем «согласен»."""
    entry_id = await _annotated("спорная", AnnotationState.TWO_HYPOTHESES, message_id=10)

    await overrides.reclassify(entry_id, to=AnnotationType.TASK)

    assert await evening.disputed() == []


async def test_debt_settles_itself_after_seven_days() -> None:
    """Долг не копится (`vision.md` §7)."""
    entry_id = await _annotated("спорная", AnnotationState.TWO_HYPOTHESES, message_id=10)

    assert await evening.settle_debt(_days_after_capture(6)) == 0
    assert await evening.settle_debt(_days_after_capture(8)) == 1

    assert await evening.disputed() == []
    assert await evening.auto_resolved_count() == 1
    assert entry_id not in {item.entry_id for item in await evening.disputed()}


async def test_debt_is_settled_once_not_every_tick() -> None:
    """Тик просыпается каждые пять минут, а долг фиксируется один раз.

    Иначе на каждую спорную запись копилось бы по строке оверрайда в час,
    и `entry_overrides` перестал бы читаться глазами — а он ровно для этого.
    """
    await _annotated("спорная", AnnotationState.TWO_HYPOTHESES, message_id=10)

    assert await evening.settle_debt(_days_after_capture(8)) == 1
    assert await evening.settle_debt(_days_after_capture(8)) == 0


async def test_confirmed_and_auto_resolved_are_told_apart() -> None:
    """«Я согласился» и «за меня решили» — разные данные для eval'а.

    Слитые в одно значение, они завысили бы измеренную точность разметки:
    зафиксированное по таймауту не является подтверждением от владельца.
    """
    mine = await _annotated("спорная", AnnotationState.TWO_HYPOTHESES, message_id=10)
    await _annotated("другая спорная", AnnotationState.TWO_HYPOTHESES, message_id=20)

    await evening.settle(mine, action=OverrideAction.CONFIRM)
    await evening.settle_debt(_days_after_capture(8))

    assert await evening.auto_resolved_count() == 1
