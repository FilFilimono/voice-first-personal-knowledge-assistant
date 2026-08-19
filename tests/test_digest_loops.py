"""Открытые петли — ядро системы по `vision.md` §5.2.

Здесь проверяется вся механика оттуда: заведение только с подтверждением,
выбор старейшей, промежуток в 14 дней, сдвиг на «ещё думаю» и смена вопроса
после третьего откладывания.

Требует поднятого Postgres: `just up`.
"""

from datetime import UTC, datetime, timedelta

import pytest

from brain.core import ingest
from brain.core.digest import loops
from brain.core.enrich import repository as enrichment
from brain.core.enrich.units import MergedUnit
from brain.core.memory.unit_of_work import capture_transaction
from brain.storage.models.annotation import AnnotationState, AnnotationType
from brain.storage.models.loop import LoopStatus

pytestmark = pytest.mark.integration

TIMEZONE = "Europe/Rome"
CHAT_ID = 111
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


async def _entry(text: str, message_id: int) -> int:
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


async def test_annotation_alone_does_not_create_a_loop() -> None:
    """Петлю предлагает модель, но заводит владелец (`vision.md` §5.2).

    Это разница между «система заметила» и «система решила за меня», и она
    важнее удобства: непрошеная петля превращается в вопрос, которого владелец
    не задавал, — то есть в требование, запрещённое принципом 1 §7.0.
    """
    text = "не понимаю, стоит ли переезжать в другой город"
    entry_id = await _entry(text, message_id=10)

    await enrichment.save_annotation(
        entry_id=entry_id,
        type_=AnnotationType.LOOP,
        alternative_type=None,
        state=AnnotationState.SURE,
        entities=[],
        provider="test",
        model="test",
        existing_topic_ids=[],
        new_topics=[],
        units=[
            MergedUnit(
                start=0,
                end=len(text),
                type=AnnotationType.LOOP,
                alternative_type=None,
                state=AnnotationState.SURE,
            )
        ],
        transcript_id=None,
        text=text,
    )

    assert await loops.pick(NOW) is None


async def test_confirmed_loop_is_offered() -> None:
    text = "не понимаю, стоит ли переезжать"
    entry_id = await _entry(text, message_id=10)

    await loops.create(entry_id=entry_id, unit_id=None, question=text)
    picked = await loops.pick(NOW)

    assert picked is not None
    assert picked.question == text


async def test_the_oldest_loop_goes_first() -> None:
    """Эскалация вверх по важности, а не по частоте (`vision.md` §7).

    «Висит дольше всех» — единственная мера важности, которая есть у системы
    без вопроса владельцу.
    """
    old = await _entry("старый вопрос", message_id=10)
    new = await _entry("новый вопрос", message_id=20)

    await loops.create(entry_id=old, unit_id=None, question="старый вопрос")
    await loops.create(entry_id=new, unit_id=None, question="новый вопрос")

    picked = await loops.pick(NOW)

    assert picked is not None
    assert picked.question == "старый вопрос"


async def test_a_loop_is_not_asked_again_for_two_weeks() -> None:
    entry_id = await _entry("вопрос", message_id=10)
    loop_id = await loops.create(entry_id=entry_id, unit_id=None, question="вопрос")

    await loops.mark_asked(loop_id, now=NOW)

    assert await loops.pick(NOW + timedelta(days=13)) is None
    assert await loops.pick(NOW + timedelta(days=15)) is not None


async def test_still_thinking_counts_from_the_answer_not_from_the_question() -> None:
    """Сдвиг отсчитывается от ответа, и только поэтому колонка `snooze_until` нужна.

    Вопрос приходит в сводке, а ответ владелец даёт когда откроет телефон —
    иногда через несколько дней. Считай мы сдвиг от вопроса, «ещё думаю»
    не давало бы ничего сверх обычного промежутка: оба по четырнадцать дней.
    """
    entry_id = await _entry("вопрос", message_id=10)
    loop_id = await loops.create(entry_id=entry_id, unit_id=None, question="вопрос")
    await loops.mark_asked(loop_id, now=NOW)

    answered = NOW + timedelta(days=2)
    await loops.still_thinking(loop_id, now=answered)

    # Пятнадцать дней с вопроса: по промежутку уже можно, по сдвигу — ещё нет.
    assert await loops.pick(NOW + timedelta(days=15)) is None
    assert await loops.pick(answered + timedelta(days=15)) is not None


async def test_after_the_third_snooze_the_question_changes() -> None:
    """Петля перестаёт быть раздражителем (`vision.md` §5.2).

    Третье «ещё думаю» означает, что вопрос в нынешнем виде не двигается,
    и спрашивать его четвёртый раз — это уже упрёк, а не возврат.
    """
    entry_id = await _entry("вопрос", message_id=10)
    loop_id = await loops.create(entry_id=entry_id, unit_id=None, question="вопрос")

    assert not loops.is_stuck(0)
    for _ in range(loops.SNOOZES_BEFORE_DECISION):
        await loops.still_thinking(loop_id, now=NOW)

    stuck = await loops.by_id(loop_id)
    assert stuck is not None
    assert stuck.times_asked == loops.SNOOZES_BEFORE_DECISION
    assert loops.is_stuck(stuck.times_asked)


async def test_closed_loops_are_never_offered_again() -> None:
    entry_id = await _entry("вопрос", message_id=10)
    loop_id = await loops.create(entry_id=entry_id, unit_id=None, question="вопрос")

    await loops.settle(loop_id, LoopStatus.CLOSED)

    assert await loops.pick(NOW) is None


async def test_one_loop_per_entry() -> None:
    """Кнопка живёт в переписке вечно — нажать её можно сколько угодно раз.

    Та же причина, что у повторного «да» под вопросом про напоминание:
    два одинаковых вопроса раздражают сильнее, чем отсутствие одного.
    """
    entry_id = await _entry("вопрос", message_id=10)

    first = await loops.create(entry_id=entry_id, unit_id=None, question="вопрос")
    second = await loops.create(entry_id=entry_id, unit_id=None, question="вопрос")

    assert first == second
    assert await loops.open_count() == 1


async def test_loop_is_created_from_the_unit_not_from_the_whole_entry() -> None:
    """Текст петли берётся из единицы, а не из записи целиком.

    Запись «подумал про переезд, а ещё надо купить хлеб» содержит открытый
    вопрос только в первой половине. Вопрос «Ты хотел разобраться: купить
    хлеб» был бы бессмысленным — и владелец решил бы, что механизм сломан.
    """
    text = "не понимаю, стоит ли переезжать. и ещё надо купить хлеб"
    entry_id = await _entry(text, message_id=10)
    split = text.index("и ещё")

    await enrichment.save_annotation(
        entry_id=entry_id,
        type_=AnnotationType.LOOP,
        alternative_type=None,
        state=AnnotationState.SURE,
        entities=[],
        provider="test",
        model="test",
        existing_topic_ids=[],
        new_topics=[],
        units=[
            MergedUnit(
                start=0,
                end=split,
                type=AnnotationType.LOOP,
                alternative_type=None,
                state=AnnotationState.SURE,
            ),
            MergedUnit(
                start=split,
                end=len(text),
                type=AnnotationType.TASK,
                alternative_type=None,
                state=AnnotationState.SURE,
            ),
        ],
        transcript_id=None,
        text=text,
    )

    loop_id = await loops.create_from_entry(entry_id)

    assert loop_id is not None
    created = await loops.by_id(loop_id)
    assert created is not None
    assert created.question == "не понимаю, стоит ли переезжать."
    assert "хлеб" not in created.question


async def test_no_loop_unit_means_no_loop() -> None:
    """Кнопка живёт в переписке вечно, а разметка — нет.

    Запись могли переразметить между показом кнопки и нажатием. Молча завести
    петлю по всей записи было бы хуже, чем честно сказать «не нашёл».
    """
    text = "надо купить хлеб"
    entry_id = await _entry(text, message_id=10)

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
        units=[
            MergedUnit(
                start=0,
                end=len(text),
                type=AnnotationType.TASK,
                alternative_type=None,
                state=AnnotationState.SURE,
            )
        ],
        transcript_id=None,
        text=text,
    )

    assert await loops.create_from_entry(entry_id) is None
