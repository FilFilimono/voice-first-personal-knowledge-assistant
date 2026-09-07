"""Пороги всплывания и разговора по теме разведены.

**Решение владельца 7 сентября 2026.** Порог «старше суток» верен для
всплывания и **неверен ровно наоборот** для разговора по теме: на вопрос
«что там по учёбе» сегодняшняя запись — самая нужная, а не самая лишняя.
Довод владельца: всплывание достаёт старое без повода, и сегодняшнее он помнит
без бота; разговор по теме он завёл сам, и вопрос в нём — «а что сейчас».

Порог был один на оба механизма (`by_topic.MIN_AGE`), унаследованный
от всплывания. Здесь проверяется, что он разведён — и что всплывание при этом
не поехало: правка, чинящая одно и ломающая другое молча, в этом проекте уже
была.

Требует поднятого Postgres: `just up`.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text as sql

from brain.core.digest import resurface
from brain.core.enrich import repository as enrichment
from brain.core.enrich.units import MergedUnit
from brain.core.present import archive_topics
from brain.storage.db import session_scope
from brain.storage.models.annotation import AnnotationState, AnnotationType

pytestmark = pytest.mark.integration

TIMEZONE = "Europe/Rome"
TOPIC = "учеба"

# Часы настоящие: продакшн считает возраст записи от `datetime.now(UTC)`,
# и прибитая дата в тесте живёт ровно до того дня, когда часы её обгонят.
# Три случая этого класса за неделю — правило `CLAUDE.md`.
NOW = datetime.now(UTC)

# Длиннее ста символов: короче этого не цитируется ни там, ни там
# (`resurface.MIN_LENGTH`), и тест мерил бы длину вместо возраста.
TODAY_SAID = (
    "Сегодня наконец сел за курсовую и понял, что первая глава почти вся "
    "переписывается заново, потому что структура не та."
)
LONG_AGO_SAID = (
    "Надо всё-таки взяться за курсовую, иначе в сентябре будет ровно то же самое, "
    "что и в прошлом году с последней неделей."
)


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


async def _entry_on_topic(said: str, *, at: datetime, message_id: int) -> int:
    """Запись с нужной датой и темой.

    Сырым SQL, как в `test_prompting_path`, и по той же причине: `created_at`
    ставит база, а поправить его нечем — `entries` только на вставку, и UPDATE
    отозван триггером (инвариант 3). А весь смысл этого файла — в возрасте
    записи.

    Тема ставится руками: разметка сюда не поднимается, она про модель,
    а тест про порог.
    """
    async with session_scope() as db:
        entry_id = await db.scalar(
            sql(
                "INSERT INTO entries "
                "(created_at, source, kind, raw_text, is_private, timezone, dedup_key, meta) "
                "VALUES (:created_at, 'telegram', 'text', :body, false, :tz, :key, "
                " '{}'::jsonb) RETURNING id"
            ),
            {"created_at": at, "body": said, "tz": TIMEZONE, "key": f"threshold:{message_id}"},
        )
    assert entry_id is not None

    # Разметка кладётся **рабочим путём**: тема живёт в `entry_topics`
    # и привязана к разметке, а не к записи, — своя вставка в таблицу обошла бы
    # ограничение схемы и проверяла бы наши представления о ней.
    await enrichment.save_annotation(
        entry_id=int(entry_id),
        type_=AnnotationType.THOUGHT,
        alternative_type=None,
        state=AnnotationState.SURE,
        entities=[],
        provider="test",
        model="test",
        existing_topic_ids=[],
        new_topics=[
            enrichment.NewTopic(
                name=TOPIC, reason="тема теста", embedding=None, embedding_model=None
            )
        ],
        units=[
            MergedUnit(
                start=0,
                end=len(said),
                type=AnnotationType.THOUGHT,
                alternative_type=None,
                state=AnnotationState.SURE,
            )
        ],
        transcript_id=None,
        text=said,
    )

    return int(entry_id)


async def test_the_talk_shows_todays_entry(archive: object) -> None:
    """Главное в правке: «а что сейчас» — это и есть смысл разговора по теме."""
    today = await _entry_on_topic(TODAY_SAID, at=NOW - timedelta(hours=2), message_id=10)

    talk = await archive_topics.collect(TOPIC, NOW)

    assert talk is not None
    assert today in [moment.entry_id for moment in talk.moments], (
        "сегодняшняя запись не показана — порог всплывания остался у разговора"
    )


async def test_the_talk_still_shows_the_old_ones_too(archive: object) -> None:
    """Снятый порог не должен вытеснить старое: движение видно по обоим концам."""
    old = await _entry_on_topic(LONG_AGO_SAID, at=NOW - timedelta(days=30), message_id=20)
    today = await _entry_on_topic(TODAY_SAID, at=NOW - timedelta(hours=2), message_id=30)

    talk = await archive_topics.collect(TOPIC, NOW)

    assert talk is not None
    shown = [moment.entry_id for moment in talk.moments]
    assert old in shown
    assert today in shown


async def test_the_talk_still_skips_the_too_short(archive: object) -> None:
    """Порог длины остаётся: «напомни в 14:50» вспоминать нечего.

    Разводили возраст, а не длину. Проверяется отдельно, потому что снять
    заодно и её — самая лёгкая ошибка в этой правке.
    """
    short = await _entry_on_topic("надо бы курсовую", at=NOW - timedelta(hours=2), message_id=40)

    talk = await archive_topics.collect(TOPIC, NOW)

    assert talk is not None
    assert short not in [moment.entry_id for moment in talk.moments]


async def test_resurfacing_keeps_its_own_threshold(archive: object) -> None:
    """Всплывание не поехало: сегодняшнее оно по-прежнему не достаёт.

    У него порог свой и куда старше — `max(14 дней, старейшая четверть)`,
    — и правка разговора не имеет права его тронуть.
    """
    today = await _entry_on_topic(TODAY_SAID, at=NOW - timedelta(hours=2), message_id=50)

    candidates = await resurface.eligible(NOW)

    assert today not in [old.entry_id for old in candidates]


def test_the_two_thresholds_are_no_longer_the_same_number() -> None:
    """Разведены по-настоящему, а не совпали случайно.

    Сверяется с **всплыванием** — тем механизмом, который владелец и назвал.
    С `by_topic.MIN_AGE`, откуда порог сюда приехал, сверяться уже нельзя:
    `by_topic.pick` и `eligible` не вызываются из `src/` ни разу с 06.09.2026,
    когда `/recall <тема>` перешёл на `archive_topics`. Проверка, опёртая
    на мёртвый код, зелена независимо от того, работает ли живой.
    """
    assert archive_topics.MIN_AGE != resurface.MIN_AGE
    assert timedelta(0) == archive_topics.MIN_AGE
