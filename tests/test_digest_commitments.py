"""Дела: заведение из единиц разметки и закрытие.

Самый важный тест здесь — `test_closed_commitment_survives_reenrichment`. Ради
него дело и сделано отдельной таблицей, а не колонкой у `entry_units`: единицы
пересоздаются при каждом перепрогоне, и состояние, привязанное к ним, воскрешало
бы закрытые дела молча.

Требует поднятого Postgres: `just up`.
"""

from datetime import UTC, datetime, timedelta

import pytest

from brain.core import ingest
from brain.core.digest import commitments
from brain.core.enrich import repository as enrichment
from brain.core.enrich.units import MergedUnit
from brain.core.memory.unit_of_work import capture_transaction
from brain.storage.models.annotation import AnnotationState, AnnotationType
from brain.storage.models.commitment import CommitmentState

pytestmark = pytest.mark.integration

TIMEZONE = "Europe/Rome"
CHAT_ID = 111
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

# Три дела и мысль — то, ради чего ADR-0012 запретил схлопывать соседние
# единицы одного типа: Фаза 4 считает дела поштучно.
TEXT = "купить хлеб. позвонить маме. записаться к врачу. и вообще жизнь странная"


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


async def _entry(text: str, message_id: int = 10) -> int:
    async with capture_transaction() as uow:
        result = await ingest.capture_text(
            uow,
            ingest.IncomingText(
                chat_id=CHAT_ID,
                message_id=message_id,
                ack_message_id=message_id + 1,
                text=text,
            ),
            is_private=False,
            timezone=TIMEZONE,
            now=NOW,
        )
    assert result.entry_id is not None
    return result.entry_id


def _units(text: str) -> list[MergedUnit]:
    """Нарезка «три задачи и мысль» — руками, как её вернула бы модель."""
    bounds = [
        (0, text.index("позвонить"), AnnotationType.TASK),
        (text.index("позвонить"), text.index("записаться"), AnnotationType.TASK),
        (text.index("записаться"), text.index("и вообще"), AnnotationType.TASK),
        (text.index("и вообще"), len(text), AnnotationType.THOUGHT),
    ]
    return [
        MergedUnit(
            start=start, end=end, type=type_, alternative_type=None, state=AnnotationState.SURE
        )
        for start, end, type_ in bounds
    ]


async def _annotate(entry_id: int, text: str) -> None:
    await enrichment.save_annotation(
        entry_id=entry_id,
        type_=AnnotationType.TASK,
        alternative_type=None,
        state=AnnotationState.SURE,
        entities=[],
        provider="test",
        model="test",
        existing_topic_ids=[],
        new_topics=[],
        units=_units(text),
        transcript_id=None,
        text=text,
    )


async def test_tasks_become_commitments_and_thoughts_do_not() -> None:
    entry_id = await _entry(TEXT)
    await _annotate(entry_id, TEXT)

    open_now = await commitments.open_ones(NOW)

    assert len(open_now) == 3
    assert [item.text for item in open_now] == [
        "купить хлеб.",
        "позвонить маме.",
        "записаться к врачу.",
    ]


async def test_reenrichment_does_not_duplicate_commitments() -> None:
    """Перепрогон с той же нарезкой обязан быть идемпотентным.

    Держит это уникальность по отрезку в базе, а не проверка в коде: `just
    reenrich` по всему архиву — обычное дело, и удвоение списка дел заметили бы
    только по тому, что сводка стала вдвое длиннее.
    """
    entry_id = await _entry(TEXT)
    await _annotate(entry_id, TEXT)
    await _annotate(entry_id, TEXT)

    assert len(await commitments.open_ones(NOW)) == 3


async def test_closed_commitment_survives_reenrichment() -> None:
    """Ради этого дело и сделано отдельной таблицей.

    Перепрогон разметки создаёт новую строку `annotations` и новые единицы.
    Состояние, привязанное к `entry_units.id`, обнулилось бы — и закрытое дело
    вернулось бы в утреннюю сводку молча.
    """
    entry_id = await _entry(TEXT)
    await _annotate(entry_id, TEXT)

    first = (await commitments.open_ones(NOW))[0]
    await commitments.set_state(first.id, CommitmentState.DONE, now=NOW)

    await _annotate(entry_id, TEXT)

    still_open = await commitments.open_ones(NOW)
    assert len(still_open) == 2
    assert first.text not in [item.text for item in still_open]


async def test_closing_stamps_the_time_and_the_kind() -> None:
    """«Сделал» и «не актуально» — разные ответы.

    Недельное ревью считает закрытое, и записать отказ в достижения значило бы
    соврать в отчёте самому себе.
    """
    entry_id = await _entry(TEXT)
    await _annotate(entry_id, TEXT)
    items = await commitments.open_ones(NOW)

    await commitments.set_state(items[0].id, CommitmentState.DONE, now=NOW)
    await commitments.set_state(items[1].id, CommitmentState.DROPPED, now=NOW)

    done = await commitments.closed_between(NOW, NOW, state=CommitmentState.DONE)
    dropped = await commitments.closed_between(NOW, NOW, state=CommitmentState.DROPPED)

    assert [item.text for item in done] == ["купить хлеб."]
    assert [item.text for item in dropped] == ["позвонить маме."]


async def test_closing_twice_moves_nothing() -> None:
    """Кнопка живёт в переписке вечно, нажать её можно сколько угодно раз.

    Повтор не должен переносить дело в другую неделю ревью: «что закрыл
    за неделю» считается по `state_changed_at`, и второе нажатие в среду
    забрало бы у прошлой недели то, что было закрыто в ней.

    Отказывать в **другом** состоянии при этом больше нельзя: состояний
    четыре, и переход между ними — обычный ход, а не промах. От промаха
    защищает отмена под самим нажатием, а не запрет в базе.
    """
    entry_id = await _entry(TEXT)
    await _annotate(entry_id, TEXT)
    item = (await commitments.open_ones(NOW))[0]

    assert await commitments.set_state(item.id, CommitmentState.DONE, now=NOW)
    later = NOW + timedelta(days=3)
    assert await commitments.set_state(item.id, CommitmentState.DONE, now=later)

    still = await commitments.closed_between(NOW, NOW, state=CommitmentState.DONE)
    assert [found.id for found in still] == [item.id]
    assert len(await commitments.open_ones(NOW)) == 2


async def test_commitment_keeps_a_snapshot_of_its_text() -> None:
    """Снимок, а не только отрезок: сводка обязана показать дело даже после
    перепрогона расшифровки, сдвинувшего границы."""
    entry_id = await _entry(TEXT)
    await _annotate(entry_id, TEXT)

    item = (await commitments.open_ones(NOW))[0]

    assert item.text
    assert item.source_entry_id == entry_id
    assert item.source_unit_id is not None
