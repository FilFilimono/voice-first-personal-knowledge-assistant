"""Утренняя сводка не считает вслух неразобранное.

**Почему тест идёт от тика, а не от `render.morning`.** Правило §7.0 п.2
(«запрещён ретроспективный упрёк») было записано за три дня до того, как
строка «Со вчера ждут разбора: N. Покажу вечером.» прошла ревью и уехала в прод.
Разработчик правило помнил — и всё равно написал строку, потому что в замысле
она была заботой. Значит, проверять надо **текст, который получил владелец**,
а не намерение автора и не чистую функцию рендера: чистая функция и раньше
делала ровно то, что от неё хотели.

Отсюда устройство: `service.tick` → `FakeNotifier` → строка сообщения. Тот же
путь, что у `test_digest_tick`, и по той же причине — девять дыр проекта
одинаково прятались между проверенными кусками.

**Неразобранные записи в базе есть.** Иначе тест зелёный по случайности:
сводка без упрёка на архиве без спорных записей ничего не доказывает.
Тексты берутся из живой базы по номерам записей — выдуманная «похожая» строка
не ломает код так, как ломает живая речь (`tests/archive.py`).

**Почему здесь не зовётся `present.guard.reproach`.** Он ищет упрёк по закрытому
списку слов («накопилось», «так и не», «забыл»), а убранная строка не содержала
ни одного из них: упрёк в ней нёс **счётчик**, а не лексика. Прогнав сводку
через `reproach`, мы получили бы зелёный, который не зависит от правки, —
то есть ровно ту проверку, что пропустила строку в первый раз.

Требует поднятого Postgres: `just up`.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text as sql

from brain.core import ingest
from brain.core.digest import evening, loops, service, slots
from brain.core.enrich import repository as enrichment
from brain.core.enrich.units import MergedUnit
from brain.core.memory.unit_of_work import capture_transaction
from brain.storage.db import session_scope
from brain.storage.models.annotation import AnnotationState, AnnotationType
from tests import archive as archive_module
from tests.fakes import FakeNotifier

pytestmark = pytest.mark.integration

TIMEZONE = "Europe/Rome"
CHAT_ID = 111

# 09:30 по Риму 20 августа 2026 — внутри утреннего окна. Тот же момент,
# что в `test_digest_tick`: сводка обязана уйти.
MORNING = datetime(2026, 8, 20, 7, 30, tzinfo=UTC)
YESTERDAY = MORNING - timedelta(days=1)

# Живая запись, из которой выходят два дела и открытый вопрос. Выбрана за то,
# что граница между делами в ней настоящая: «Фильм, да, надо будет посмотреть»
# начинается на 55-м символе — так эта запись и разрезана в рабочей базе.
DEEDS_ENTRY = 53
SECOND_DEED = "Фильм, да,"
LOOP_SENTENCE = "Нужно ещё find починить"

# Записи, о которых модель не договорилась сама с собой. Ровно тот материал,
# который вечер показывает, а утро молчаливо пропускает.
DISPUTED_ENTRIES = (36, 43, 50)


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


async def _deeds_and_loop(entries: list[archive_module.Entry]) -> tuple[int, int]:
    """Живая запись с двумя делами и петлёй. Возвращает номера записи и петли.

    Приём — через настоящий `ingest`, а не вставкой строки: сводка обязана
    собираться на том, что кладёт в базу рабочий путь.
    """
    body = archive_module.entry(entries, DEEDS_ENTRY).text

    async with capture_transaction() as uow:
        result = await ingest.capture_text(
            uow,
            ingest.IncomingText(chat_id=CHAT_ID, message_id=10, ack_message_id=11, text=body),
            is_private=False,
            timezone=TIMEZONE,
            now=YESTERDAY,
        )
    entry_id = result.entry_id
    assert entry_id is not None

    split = body.index(SECOND_DEED)
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
                start=start,
                end=end,
                type=AnnotationType.TASK,
                alternative_type=None,
                state=AnnotationState.SURE,
            )
            for start, end in ((0, split), (split, len(body)))
        ],
        transcript_id=None,
        text=body,
    )

    # Вопрос петли — слова самого владельца из той же записи, а не наша
    # формулировка: петля возвращает его к своей мысли (`vision.md` §5.2).
    question = body[body.index(LOOP_SENTENCE) :].split(".")[0]
    loop_id = await loops.create(entry_id=entry_id, unit_id=None, question=question)
    return entry_id, loop_id


async def _disputed_yesterday(entries: list[archive_module.Entry]) -> list[int]:
    """Вчерашние спорные записи — сырым SQL, ради даты создания."""
    created: list[int] = []
    async with session_scope() as session:
        for number in DISPUTED_ENTRIES:
            body = archive_module.entry(entries, number).text
            entry_id = await session.scalar(
                sql(
                    "INSERT INTO entries "
                    "(created_at, source, kind, raw_text, is_private, timezone, "
                    " dedup_key, meta) "
                    "VALUES (:created_at, 'telegram', 'text', :body, false, :tz, "
                    " :key, '{}'::jsonb) RETURNING id"
                ),
                {
                    "created_at": YESTERDAY,
                    "body": body,
                    "tz": TIMEZONE,
                    "key": f"live-disputed:{number}",
                },
            )
            await session.execute(
                sql(
                    "INSERT INTO annotations "
                    "(source_entry_id, type, alternative_type, state, entities, "
                    " provider, model) "
                    "VALUES (:entry_id, :type, :alt, :state, '[]'::jsonb, 'test', 'test')"
                ),
                {
                    "entry_id": entry_id,
                    "type": AnnotationType.THOUGHT.value,
                    "alt": AnnotationType.TASK.value,
                    "state": AnnotationState.TWO_HYPOTHESES.value,
                },
            )
            created.append(int(entry_id or 0))
    return created


async def _waiting_since_yesterday() -> list[int]:
    """Что в базе действительно ждёт разбора со вчера."""
    return [
        item.entry_id
        for item in await evening.disputed(limit=100)
        if slots.local_date(item.created_at, TIMEZONE) == slots.local_date(YESTERDAY, TIMEZONE)
    ]


async def test_the_morning_summary_never_counts_the_unsorted(
    archive: list[archive_module.Entry],
) -> None:
    """Главное утверждение задачи: счёта неразобранного в тексте нет.

    Проверяется на архиве, где неразобранное **есть**: иначе утверждение
    выполнялось бы само собой и правку бы не удержало.
    """
    await _deeds_and_loop(archive)
    await _disputed_yesterday(archive)
    assert len(await _waiting_since_yesterday()) == len(DISPUTED_ENTRIES)

    notifier = FakeNotifier()
    await service.tick(
        now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None
    )

    assert len(notifier.mornings) == 1
    _, text, _, _ = notifier.mornings[0]
    assert "ждут разбора" not in text
    assert "Покажу вечером" not in text


async def test_the_morning_summary_still_gives_deeds_and_the_loop(
    archive: list[archive_module.Entry],
) -> None:
    """Убрано ровно одно, а не «сводка стала тише».

    Сообщение по-прежнему **даёт**: дела на сегодня и вопрос, который владелец
    сам себе задал. Последняя строка — этот вопрос: приписки после него больше
    нет, и проверяется это концом текста, а не отсутствием подстроки, потому
    что вернуть счётчик проще всего именно припиской в конец.
    """
    await _deeds_and_loop(archive)
    await _disputed_yesterday(archive)

    notifier = FakeNotifier()
    await service.tick(
        now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None
    )

    _, text, commitment_ids, loop_id = notifier.mornings[0]
    assert len(commitment_ids) == 2
    assert "Вот это по боту" in text
    assert SECOND_DEED in text
    assert loop_id is not None
    assert LOOP_SENTENCE in text
    assert text.splitlines()[-1] == "Сдвинулось?"
