"""Перепрогон разметки, сделанной до ADR-0012.

**Почему этот файл существует.** 19 августа была написана команда
`just reenrich --stale`, которая ставила по заданию на каждую из 37 записей
со старой разметкой. Задания встали в очередь и **ничего бы не сделали**:
обработчик `ENRICH` отбирает записи, у которых разметки нет **вовсе**,
а у этих она есть — просто старая, без смысловых единиц. Нашлось это только
разбором очереди назавтра: `attempts = 0` у всех тридцати семи.

Заодно выяснилась цена: 37 записей — это не 37 запросов к модели, а две пачки
по 25 в два прогона, то есть **четыре**. Разница ровно та, из-за которой
в квоту упирались вслепую.

Требует поднятого Postgres: `just up`.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, insert, select

from brain.core import enrich, ingest
from brain.core.enrich import repository as enrichment
from brain.core.memory.unit_of_work import capture_transaction
from brain.storage.db import session_scope
from brain.storage.models.annotation import Annotation, AnnotationState, AnnotationType
from brain.storage.models.override import EntryOverride, OverrideAction
from brain.storage.models.unit import EntryUnit
from tests.fakes import ScriptedModel, StubEmbedder, answer_units

pytestmark = pytest.mark.integration

TIMEZONE = "Europe/Rome"
CHAT_ID = 111
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

STALE = "надо купить хлеб и заодно подумать про переезд"


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


async def _entry_with_old_annotation(text: str = STALE, message_id: int = 10) -> int:
    """Запись, размеченная по-старому: разметка есть, смысловых единиц нет."""
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
    entry_id = result.entry_id
    assert entry_id is not None

    await enrichment.save_annotation(
        entry_id=entry_id,
        type_=AnnotationType.THOUGHT,
        alternative_type=None,
        state=AnnotationState.SURE,
        entities=[],
        provider="test",
        model="test",
        existing_topic_ids=[],
        new_topics=[],
        # Пусто — это и есть «разметка до ADR-0012».
        units=[],
        transcript_id=None,
        text=text,
    )
    return entry_id


async def _units_of(entry_id: int) -> list[EntryUnit]:
    async with session_scope() as session:
        latest = await session.scalar(
            select(func.max(Annotation.id)).where(Annotation.source_entry_id == entry_id)
        )
        return list(
            (
                await session.scalars(
                    select(EntryUnit)
                    .where(EntryUnit.annotation_id == latest)
                    .order_by(EntryUnit.ordinal)
                )
            ).all()
        )


async def test_pending_pass_ignores_already_annotated_entries() -> None:
    """Это и есть корень: обычный проход такую запись не видит.

    Тест закрепляет поведение, а не осуждает его: отбирать по «нет разметки» —
    правильно для обычной работы. Неправ был вызывающий, который ждал от него
    перепрогона.
    """
    await _entry_with_old_annotation()
    model = ScriptedModel([answer_units(1, ("начало", "task"))])

    outcome = await enrich.annotate_pending(model, StubEmbedder(), limit=25)

    assert outcome.annotated == 0
    assert model.calls == 0


async def test_explicit_entries_are_reannotated_even_when_annotated() -> None:
    """Перепрогон по явному списку — то, чего не хватало команде.

    Разметка при этом не правится, а **добавляется строкой** (ADR-0011):
    старая остаётся, текущей становится новая.
    """
    entry_id = await _entry_with_old_annotation()
    split = STALE.index("и заодно")
    model = ScriptedModel(
        [answer_units(entry_id, ("надо купить", "task"), ("и заодно", "thought"))]
    )

    outcome = await enrich.annotate_pending(model, StubEmbedder(), limit=25, entry_ids=[entry_id])

    assert outcome.annotated == 1
    units = await _units_of(entry_id)
    assert [unit.type for unit in units] == [AnnotationType.TASK, AnnotationType.THOUGHT]
    assert units[1].span_start == split

    async with session_scope() as session:
        # Старая разметка цела: перепрогон добавляет строку, а не стирает.
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Annotation)
                .where(Annotation.source_entry_id == entry_id)
            )
            == 2
        )


async def test_private_entries_never_get_reannotated_by_id() -> None:
    """Явный список не должен быть дырой в инварианте 5.

    Отбор по «нет разметки» приватные отсекает сам; список по идентификаторам
    мог бы протащить их мимо — если бы фильтр стоял только там.
    """
    entry_id = await _entry_with_old_annotation()
    async with session_scope() as session:
        await session.execute(
            insert(EntryOverride).values(
                source_entry_id=entry_id, action=OverrideAction.MAKE_PRIVATE, payload={}
            )
        )

    model = ScriptedModel([answer_units(entry_id, ("начало", "task"))])
    outcome = await enrich.annotate_pending(model, StubEmbedder(), limit=25, entry_ids=[entry_id])

    assert outcome.annotated == 0
    assert model.calls == 0


async def test_batch_size_decides_the_number_of_requests() -> None:
    """Тридцать семь записей — четыре запроса, а не тридцать семь.

    Это ответ на вопрос «сколько квоты потратит команда», и без него в лимит
    упираются вслепую. Два прогона на пачку — устройство разметки (ADR-0011).
    """
    assert enrich.requests_for(37, batch_limit=25) == 4
    assert enrich.requests_for(25, batch_limit=25) == 2
    assert enrich.requests_for(26, batch_limit=25) == 4
    assert enrich.requests_for(0, batch_limit=25) == 0
