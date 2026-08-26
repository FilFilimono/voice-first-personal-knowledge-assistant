"""Три дыры, которые нашёл повтор касаний по живому архиву 19 августа 2026.

Ни одну из трёх не поймал бы модульный тест: все три видны только тогда,
когда механизм прогоняется по нескольким дням подряд на настоящих данных.
Это ровно тот случай, ради которого в проекте записано правило «замер обязан
воспроизводить рабочий режим».

    1. вечерний разбор повторял одну и ту же пачку каждый вечер
    2. утренняя сводка уходила, состоя целиком из «ждут разбора: 2»
    3. недельное ревью срабатывало на архиве в один день

Требует поднятого Postgres: `just up`.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from brain.core.digest import evening, loops, morning, touches, weekly
from brain.storage.db import session_scope
from brain.storage.models.annotation import AnnotationState, AnnotationType
from brain.storage.models.touch import TouchSlot

pytestmark = pytest.mark.integration

# Таймзона записей задаётся прямо во вставке: сборке сводки она больше
# не нужна — вчерашнее неразобранное из сводки убрано, а с ним и единственное
# место, где день владельца влиял на её содержание.
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


async def _disputed_entry(body: str, *, age: timedelta, key: str) -> int:
    """Спорная запись с заданным возрастом — сырым SQL, ради дат."""
    async with session_scope() as session:
        entry_id = await session.scalar(
            text(
                "INSERT INTO entries "
                "(created_at, source, kind, raw_text, is_private, timezone, dedup_key, meta) "
                "VALUES (:created_at, 'telegram', 'text', :body, false, 'Europe/Rome', "
                ":key, '{}'::jsonb) RETURNING id"
            ),
            {"created_at": NOW - age, "body": body, "key": key},
        )
        await session.execute(
            text(
                "INSERT INTO annotations "
                "(source_entry_id, type, alternative_type, state, entities, provider, model) "
                "VALUES (:entry_id, :type, :alt, :state, '[]'::jsonb, 'test', 'test')"
            ),
            {
                "entry_id": entry_id,
                "type": AnnotationType.THOUGHT.value,
                "alt": AnnotationType.TASK.value,
                "state": AnnotationState.TWO_HYPOTHESES.value,
            },
        )
    return int(entry_id or 0)


async def _topic(entry_id: int, name: str) -> None:
    """Тема и её привязка к записи — сырым SQL, минуя разметку.

    Разметку здесь звать нечем: модель в быстрых и интеграционных тестах
    не поднимается, а ревью нужна только сама привязка.
    """
    async with session_scope() as session:
        topic_id = await session.scalar(
            text(
                "INSERT INTO topics (source_entry_id, name, slug, reason) "
                "VALUES (:entry_id, :name, :name, 'тест') RETURNING id"
            ),
            {"entry_id": entry_id, "name": name},
        )
        annotation_id = await session.scalar(
            text("SELECT max(id) FROM annotations WHERE source_entry_id = :entry_id"),
            {"entry_id": entry_id},
        )
        await session.execute(
            text(
                "INSERT INTO entry_topics (source_entry_id, topic_id, annotation_id) "
                "VALUES (:entry_id, :topic_id, :annotation_id)"
            ),
            {"entry_id": entry_id, "topic_id": topic_id, "annotation_id": annotation_id},
        )


async def test_evening_does_not_repeat_the_same_batch_every_night() -> None:
    """Дыра 1: без исключения показанного одна пачка уходила бы семь вечеров.

    Повторение одного и того же сообщения — та самая усталость от уведомлений,
    из-за которой системы бросают (`vision.md` §7.0). Не ответили — запись
    зафиксируется сама через семь дней и молча, а не будет спрашиваться снова.
    """
    first = await _disputed_entry("первая спорная", age=timedelta(days=1), key="a")
    await _disputed_entry("вторая спорная", age=timedelta(hours=12), key="b")

    tonight = await evening.disputed(limit=1)
    assert [item.entry_id for item in tonight] == [first]

    await touches.record_sent(TouchSlot.EVENING, NOW.date(), payload={"entry_ids": [first]})

    tomorrow = await evening.disputed(limit=1, exclude=await touches.shown_in_evening())
    assert tomorrow
    assert tomorrow[0].entry_id != first


async def test_unsorted_alone_is_not_a_reason_to_send_the_summary() -> None:
    """Дыра 2: сводка 17 августа состояла целиком из «Со вчера ждут разбора: 2».

    Сообщение, у которого всё содержание — указание на несделанное, нарушает
    принцип 1 §7.0 ровно тем же, чем нарушал бы вечерний упрёк. Тогда строка
    про неразобранное осталась припиской, а не поводом отправить; позже она
    ушла из сводки совсем (`digest/morning.py`), и утверждение от этого только
    усилилось: спорная запись в базе есть, а сводка молчит.
    """
    await _disputed_entry("вчерашняя спорная", age=timedelta(days=1), key="c")

    content = await morning.collect(NOW)

    assert await evening.disputed()
    assert not content.commitments
    assert content.loop is None
    assert content.empty


async def test_weekly_review_waits_for_a_week_of_archive() -> None:
    """Дыра 3: ревью срабатывало в первое же воскресенье, на архиве в один день.

    «Неделя» из одного дня — это не неделя, и `/status` при этом обещает
    владельцу ровно обратное: «пересказ недели — нужна неделя записей».
    Обещание и код разъезжались молча.
    """
    entry_ids = [
        await _disputed_entry(f"запись {number}", age=timedelta(hours=1), key=f"young-{number}")
        for number in range(12)
    ]
    # Материала хватает по всем остальным условиям: тема и открытый вопрос —
    # это два непустых раздела из трёх, то есть порог `MIN_SECTIONS` пройден.
    # Имя не из базовых тем: `разработка`, `cv чек` и `учеба` заводит миграция,
    # и вставка такой же упёрлась бы в уникальность слага.
    await _topic(entry_ids[0], "переезд")
    await loops.create(entry_id=entry_ids[1], unit_id=None, question="куда дальше")

    # Не хватает только возраста архива.
    assert await weekly.collect(NOW) is None

    # Тот же архив, но старше недели — ревью появляется.
    await _disputed_entry("старая запись", age=timedelta(days=9), key="old")
    assert await weekly.collect(NOW) is not None
