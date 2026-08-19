"""Что должно работать:

1. подтверждение приёма уходит быстрее 300 мс — **даже когда база тормозит**;
   именно это и проверяется, потому что скорость здесь следствие порядка
   действий, а не быстрой машины
2. текстовое сообщение сохраняется, а подтверждение превращается в ответ
3. голосовое: подтверждение сразу, дальше его редактирует воркер —
   второго сообщения бот не шлёт
4. сообщение не из whitelist игнорируется молча: ни ответа, ни записи
5. повторная доставка того же апдейта не создаёт вторую запись и не оставляет
   лишнего подтверждения в переписке
6. `/private on` переключает режим, а голосовое в нём отклоняется
   с объяснением — и наружу не уходит ничего
"""

import asyncio
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from aiogram import Bot, Dispatcher

from brain.adapters.telegram.bot import COMMANDS, build_dispatcher, register_commands
from brain.config import Settings
from brain.core.status import SystemStatus, WorkerStatus
from tests.fakes import FakeUnitOfWork
from tests.telegram_harness import (
    OWNER_ID,
    STRANGER_ID,
    FakeSession,
    make_bot,
    make_message,
    make_update,
    make_voice,
)

ACK = "Принял."
SLOW_DATABASE = 0.5
ACK_BUDGET = 0.3


class PrivateModeSpy:
    """Приватный режим в памяти вместо базы."""

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    async def read(self) -> bool:
        return self.enabled

    async def switch(self, *, enabled: bool) -> None:
        self.enabled = enabled


@pytest.fixture
def uow() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture
def private_mode() -> PrivateModeSpy:
    return PrivateModeSpy()


@pytest.fixture
def slow_database() -> Iterator[float]:
    """Насколько медленно отвечает база. По умолчанию — мгновенно."""
    yield 0.0


@pytest.fixture
def dispatcher(
    monkeypatch: pytest.MonkeyPatch,
    uow: FakeUnitOfWork,
    private_mode: PrivateModeSpy,
    slow_database: float,
) -> Dispatcher:
    """Настоящий диспетчер с подменённым доступом к базе."""

    @asynccontextmanager
    async def fake_transaction() -> AsyncGenerator[FakeUnitOfWork]:
        # Пауза изображает медленную запись: подтверждение обязано уйти раньше.
        await asyncio.sleep(slow_database)
        yield uow

    monkeypatch.setattr("brain.adapters.telegram.handlers.capture_transaction", fake_transaction)
    monkeypatch.setattr(
        "brain.adapters.telegram.handlers.repository.is_private_mode", private_mode.read
    )
    monkeypatch.setattr(
        "brain.adapters.telegram.handlers.repository.switch_private_mode", private_mode.switch
    )

    settings = Settings(
        _env_file=None,
        telegram_bot_token="123:test-token",
        google_api_key="AI-test",
        telegram_allowed_user_ids=[OWNER_ID],
    )
    return build_dispatcher(settings)


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def bot(session: FakeSession) -> Bot:
    return make_bot(session)


async def test_text_is_stored_and_acknowledged(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, uow: FakeUnitOfWork
) -> None:
    await dispatcher.feed_update(bot, make_update(make_message(text="надо переписать ретривал")))

    assert uow.entries[0].raw_text == "надо переписать ретривал"
    assert session.texts == [ACK, "Записал."]
    # Одна запись — одно уведомление: ответ это отредактированное подтверждение.
    assert len(session.calls_named("SendMessage")) == 1
    assert len(session.calls_named("EditMessageText")) == 1


@pytest.mark.parametrize("slow_database", [SLOW_DATABASE])
async def test_acknowledgement_arrives_before_the_database_finishes(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession
) -> None:
    """Скорость подтверждения — следствие порядка действий, а не быстрой машины."""
    session.reset_clock()

    await dispatcher.feed_update(bot, make_update(make_message(text="мысль на ходу")))

    ack = session.calls_named("SendMessage")[0]
    assert ack.elapsed < ACK_BUDGET
    # А запись всё это время шла — и дошла.
    assert session.calls_named("EditMessageText")[0].elapsed >= SLOW_DATABASE


@pytest.mark.parametrize("slow_database", [SLOW_DATABASE])
async def test_voice_is_acknowledged_immediately_and_left_for_the_worker(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, uow: FakeUnitOfWork
) -> None:
    session.reset_clock()

    await dispatcher.feed_update(bot, make_update(make_message(voice=make_voice())))

    ack = session.calls_named("SendMessage")[0]
    assert ack.elapsed < ACK_BUDGET
    assert session.texts == [ACK]

    # Бот больше ничего не говорит: подтверждение отредактирует воркер,
    # когда расшифровка будет готова. Идентификатор для этого лежит в задании.
    assert session.calls_named("EditMessageText") == []
    _, payload, _ = uow.jobs_named("transcribe")[0]
    assert payload["ack_message_id"] == 901


async def test_stranger_is_ignored_without_a_word(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, uow: FakeUnitOfWork
) -> None:
    """Ответ «нельзя» подтвердил бы, что бот живой и чей-то личный."""
    await dispatcher.feed_update(bot, make_update(make_message(text="привет", user_id=STRANGER_ID)))

    assert session.calls == []
    assert uow.entries == []


async def test_redelivered_update_leaves_no_second_entry_and_no_extra_message(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, uow: FakeUnitOfWork
) -> None:
    message = make_message(text="одна мысль", message_id=42)

    await dispatcher.feed_update(bot, make_update(message, update_id=1))
    await dispatcher.feed_update(bot, make_update(message, update_id=1))

    assert len(uow.entries) == 1
    # Второе подтверждение убирается за собой: владелец отправлял сообщение один раз.
    assert len(session.calls_named("DeleteMessage")) == 1


async def test_status_reports_the_worker_first(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/status` существует ради вопроса «жив ли воркер» — он и идёт первым."""
    collected = SystemStatus(
        worker=WorkerStatus(last_beat=None, stale_after=timedelta(minutes=3)),
        entries=7,
        voice_without_transcript=2,
        entries_without_index=0,
        entries_without_annotation=0,
        waiting_for_review=0,
        trigrams_work=True,
        jobs_queued=2,
        jobs_failed=0,
        next_reminder=None,
        private_mode=False,
        topics=9,
        archive_age_days=60,
    )

    async def fake_collect() -> SystemStatus:
        return collected

    monkeypatch.setattr("brain.adapters.telegram.handlers.core_status.collect", fake_collect)

    await dispatcher.feed_update(bot, make_update(make_message(text="/status")))

    report = session.texts[-1]
    assert "Воркер" in report.splitlines()[0]
    assert "Записей: 7" in report
    assert "без расшифровки: 2" in report


async def test_command_menu_lists_every_command_the_bot_answers() -> None:
    """Меню не должно обещать того, чего бот не понимает, и наоборот."""
    promised = {command.command for command in COMMANDS}

    assert promised == {"start", "status", "private", "find"}


async def test_command_menu_is_registered_on_start(bot: Bot, session: FakeSession) -> None:
    await register_commands(bot)

    assert len(session.calls_named("SetMyCommands")) == 1


async def test_private_mode_switches(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, private_mode: PrivateModeSpy
) -> None:
    await dispatcher.feed_update(bot, make_update(make_message(text="/private on")))
    assert private_mode.enabled

    await dispatcher.feed_update(bot, make_update(make_message(text="/private off")))
    assert not private_mode.enabled

    assert "Приватный режим включён" in session.texts[0]


async def test_voice_is_refused_in_private_mode_with_an_explanation(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    uow: FakeUnitOfWork,
    private_mode: PrivateModeSpy,
) -> None:
    """Инвариант 5 и ADR-0002: приватное — только текстом, исключений нет."""
    private_mode.enabled = True

    await dispatcher.feed_update(bot, make_update(make_message(voice=make_voice())))

    refusal = session.texts[-1]
    assert "только текст" in refusal
    assert "внешн" in refusal  # объяснение, а не сухой отказ

    # Ничего не сохранено и ничего не поставлено в очередь: наружу не ушло ничто.
    assert uow.entries == []
    assert uow.jobs == []


async def test_text_is_still_accepted_in_private_mode(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    uow: FakeUnitOfWork,
    private_mode: PrivateModeSpy,
) -> None:
    private_mode.enabled = True

    await dispatcher.feed_update(bot, make_update(make_message(text="то, что не для модели")))

    assert uow.entries[0].is_private
    assert session.texts[-1] == "Записал приватно."
