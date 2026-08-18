"""Напоминание, чей срок прошёл, пока воркер лежал.

Два разных утверждения, и путать их нельзя:

1. **задание не теряется** — дежурный запрос pgqueuer берёт всё, у чего
   `execute_after <= NOW()`, поэтому просроченное подхватывается при первом же
   старте. Это здесь проверяется, а не предполагается: на нём держится решение
   не заводить свой sweep (ADR-0005);
2. **владелец узнаёт, что оно просрочено** — а вот этого не было. 17 августа
   напоминание на 20:00 пришло в 20:46 и выглядело как обычное: ни слова
   о том, что оно опоздало на три четверти часа. В базе это видно до сих пор —
   `reminders.id = 3`, `fire_at` 17:00 UTC, `fired_at` 17:46 UTC.

Разница между «не потерялось» и «пришло понятным» стоила того же, что и дыра
с удалёнными заданиями: система выглядела здоровой, а владелец получал
неверную картину времени.

Требует поднятого Postgres: `just up`.
"""

import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from pgqueuer.db import AsyncpgDriver
from pgqueuer.ports.repository import EntrypointExecutionParameter
from pgqueuer.queries import Queries

from brain.core.memory.unit_of_work import capture_transaction
from brain.core.ports import EntryDraft, ReminderDraft
from brain.core.scheduler import jobs
from brain.core.scheduler.queue import REMINDER
from brain.storage.db import asyncpg_dsn
from brain.storage.models.entry import EntryKind, EntrySource
from tests.fakes import FakeNotifier

pytestmark = pytest.mark.integration

TIMEZONE = "Europe/Moscow"
CHAT_ID = 111
HOUR = timedelta(hours=1)


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


async def _reminder_due_at(fire_at: datetime) -> int:
    """Запись и напоминание к ней — так же, как их заводит разбор текста."""
    async with capture_transaction() as uow:
        entry_id = await uow.add_entry(
            EntryDraft(
                source=EntrySource.TELEGRAM,
                kind=EntryKind.TEXT,
                raw_text="напомни мне уйти в 19:40 вечера домой",
                audio_path=None,
                duration_sec=None,
                is_private=False,
                timezone=TIMEZONE,
                dedup_key=f"{CHAT_ID}:{fire_at.timestamp()}",
                meta={},
            )
        )
        assert entry_id is not None
        return await uow.add_reminder(
            ReminderDraft(source_entry_id=entry_id, fire_at=fire_at, text="уйти домой")
        )


async def test_job_whose_time_passed_is_picked_up_after_a_restart() -> None:
    """Пока воркер лежал, срок прошёл — задание всё равно ждёт в очереди.

    Это проверка библиотеки, и она здесь намеренно: на «pgqueuer сам разберётся
    с отложенными» опирается решение не писать свой sweep. Утверждение, на
    которое опираются, должно быть проверено, а не принято на слово.
    """
    async with capture_transaction() as uow:
        await uow.enqueue(REMINDER, {"reminder_id": 1, "chat_id": CHAT_ID}, execute_after=-HOUR)

    # Соединение сырое: pgqueuer читает очередь в обход ORM — так же,
    # как это делает воркер.
    connection = await asyncpg.connect(dsn=asyncpg_dsn())
    try:
        queries = Queries(AsyncpgDriver(connection))
        assert await queries.eligible_queued_work([REMINDER]) == 1

        picked = await queries.dequeue(
            batch_size=10,
            entrypoints={REMINDER: EntrypointExecutionParameter(concurrency_limit=0)},
            queue_manager_id=uuid.uuid4(),
            global_concurrency_limit=None,
            heartbeat_timeout=timedelta(seconds=30),
        )
    finally:
        await connection.close()

    assert [job.entrypoint for job in picked] == [REMINDER], "просроченное задание не подхвачено"


async def test_overdue_reminder_says_it_is_overdue() -> None:
    """Опоздавшее напоминание обязано признаться, что опоздало.

    Иначе владелец читает «⏰ уйти домой» в 20:46 и понимает это как «пора»,
    хотя пора было в 20:00. Молчаливое опоздание искажает то единственное,
    ради чего напоминание существует, — время.
    """
    notifier = FakeNotifier()
    was_due = datetime.now(UTC) - timedelta(minutes=46)
    reminder_id = await _reminder_due_at(was_due)

    await jobs.remind(reminder_id=reminder_id, chat_id=CHAT_ID, notifier=notifier)

    _, sent = notifier.sent[0]
    assert "уйти домой" in sent
    assert "росрочен" in sent, "опоздание не названо вслух"
    # Названо и то, когда должно было прийти, — в таймзоне владельца.
    assert "19:40" in sent or was_due.astimezone().strftime("%H:%M") in sent


async def test_on_time_reminder_says_nothing_about_being_late() -> None:
    """Обычное напоминание остаётся обычным: лишнее предупреждение обесценит нужное."""
    notifier = FakeNotifier()
    reminder_id = await _reminder_due_at(datetime.now(UTC))

    await jobs.remind(reminder_id=reminder_id, chat_id=CHAT_ID, notifier=notifier)

    _, sent = notifier.sent[0]
    assert "росрочен" not in sent


HOUR = timedelta(hours=1)
