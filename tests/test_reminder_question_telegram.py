"""Вопрос «это напоминание?» в переписке: кнопки и ответ текстом.

Проверяется путь целиком через настоящий `Dispatcher` — от апдейта до того,
что владелец видит и что ушло в базу. Иначе проверялись бы наши представления
о том, как aiogram разбирает нажатие кнопки, а не сам разбор.

Связка ответа с записью держится на `meta.ack_message_id`: вопрос — это
отредактированное подтверждение приёма, его идентификатор лежит в записи
с момента вставки. Поэтому ни новой таблицы, ни состояния в памяти не нужно,
и кнопки переживают перезапуск бота.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from aiogram import Bot, Dispatcher

from brain.adapters.telegram.bot import build_dispatcher
from brain.config import Settings
from brain.core.scheduler.queue import REMINDER
from tests.fakes import FakeUnitOfWork
from tests.telegram_harness import (
    OWNER_ID,
    STRANGER_ID,
    FakeSession,
    make_bot,
    make_callback_update,
    make_message,
    make_reply_update,
    make_update,
)

# entries.id = 27 — расшифровка съела «На» у «Напомни».
LOST_TO_STT = "Помни не уйти в 7:40 вечера."
NO_TIME_AT_ALL = "не забудь позвонить маме"

ACK_MESSAGE_ID = 901


class EntryStore:
    """Записи в памяти вместо базы — ровно то, что нужно обработчику ответа."""

    def __init__(self, uow: FakeUnitOfWork) -> None:
        self._uow = uow
        self.reminders_by_entry: set[int] = set()

    async def find_by_ack(self, chat_id: int, ack_message_id: int) -> tuple[int, str] | None:
        for number, draft in enumerate(self._uow.entries, start=1):
            meta = dict(draft.meta)
            if meta.get("chat_id") == chat_id and meta.get("ack_message_id") == ack_message_id:
                return number, draft.raw_text or ""
        return None

    async def has_reminder(self, entry_id: int) -> bool:
        return entry_id in self.reminders_by_entry


@pytest.fixture
def uow() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture
def store(uow: FakeUnitOfWork) -> EntryStore:
    return EntryStore(uow)


@pytest.fixture
def dispatcher(
    monkeypatch: pytest.MonkeyPatch, uow: FakeUnitOfWork, store: EntryStore
) -> Dispatcher:
    @asynccontextmanager
    async def fake_transaction() -> AsyncGenerator[FakeUnitOfWork]:
        yield uow

    async def not_private() -> bool:
        return False

    monkeypatch.setattr("brain.adapters.telegram.handlers.capture_transaction", fake_transaction)
    monkeypatch.setattr("brain.adapters.telegram.handlers.repository.is_private_mode", not_private)
    monkeypatch.setattr(
        "brain.adapters.telegram.handlers.repository.entry_by_ack_message", store.find_by_ack
    )
    monkeypatch.setattr(
        "brain.adapters.telegram.handlers.repository.has_reminder", store.has_reminder
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


async def _capture(dispatcher: Dispatcher, bot: Bot, text: str) -> None:
    await dispatcher.feed_update(bot, make_update(make_message(text=text, message_id=42)))


# --- Ветка 1: время известно, показываем кнопки ---


async def test_imperative_with_a_time_shows_buttons(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, uow: FakeUnitOfWork
) -> None:
    """Запись 27 больше не ложится в архив молча."""
    await _capture(dispatcher, bot, LOST_TO_STT)

    shown = session.texts[-1]
    assert "Похоже на напоминание" in shown
    assert "19:40" in shown

    buttons = session.last_buttons()
    assert [label for label, _ in buttons] == ["Да", "Нет"]
    assert all(data.endswith(":1") for _, data in buttons), "в кнопке нет записи, к которой вопрос"

    # Пока не ответили — ничего не заведено.
    assert uow.reminders == []


async def test_yes_creates_the_reminder(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, uow: FakeUnitOfWork
) -> None:
    await _capture(dispatcher, bot, LOST_TO_STT)
    _, data = session.last_buttons()[0]

    await dispatcher.feed_update(bot, make_callback_update(data, message_id=ACK_MESSAGE_ID))

    assert len(uow.reminders) == 1
    draft = uow.reminders[0]
    assert (draft.fire_at.hour, draft.fire_at.minute) == (19, 40)
    assert uow.jobs_named(REMINDER)
    assert "Напомню" in session.texts[-1]


async def test_no_creates_nothing_and_says_so(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, uow: FakeUnitOfWork
) -> None:
    await _capture(dispatcher, bot, LOST_TO_STT)
    _, data = session.last_buttons()[1]

    await dispatcher.feed_update(bot, make_callback_update(data, message_id=ACK_MESSAGE_ID))

    assert uow.reminders == []
    assert uow.jobs_named(REMINDER) == []
    assert session.texts[-1] == "Записал."


async def test_second_yes_does_not_create_a_second_reminder(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, uow: FakeUnitOfWork, store: EntryStore
) -> None:
    """Кнопка живёт в переписке вечно, нажать её можно сколько угодно раз."""
    await _capture(dispatcher, bot, LOST_TO_STT)
    _, data = session.last_buttons()[0]

    await dispatcher.feed_update(bot, make_callback_update(data, message_id=ACK_MESSAGE_ID))
    store.reminders_by_entry.add(1)
    await dispatcher.feed_update(bot, make_callback_update(data, message_id=ACK_MESSAGE_ID))

    assert len(uow.reminders) == 1


# --- Ветка 2: времени нет, ответ приходит текстом ---


async def test_imperative_without_a_time_asks_when(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, uow: FakeUnitOfWork
) -> None:
    await _capture(dispatcher, bot, NO_TIME_AT_ALL)

    shown = session.texts[-1]
    assert "Когда напомнить" in shown
    assert "позвонить маме" in shown
    # Открытый вопрос — кнопок здесь быть не должно, ответ свободный.
    assert session.last_buttons() == []
    assert uow.reminders == []


async def test_reply_supplies_the_time(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, uow: FakeUnitOfWork
) -> None:
    """Ответ на вопрос — это ответ, а не новая мысль."""
    await _capture(dispatcher, bot, NO_TIME_AT_ALL)
    captured = len(uow.entries)

    await dispatcher.feed_update(
        bot, make_reply_update("сегодня в 19:00", reply_to_message_id=ACK_MESSAGE_ID)
    )

    assert len(uow.reminders) == 1
    draft = uow.reminders[0]
    assert draft.fire_at.hour == 19
    assert "позвонить маме" in draft.text
    assert uow.jobs_named(REMINDER)
    assert "Напомню" in session.texts[-1]
    # Ответ в архив не попал: это реплика диалога, а не мысль.
    assert len(uow.entries) == captured


async def test_unparsable_reply_says_so_and_creates_nothing(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, uow: FakeUnitOfWork
) -> None:
    await _capture(dispatcher, bot, NO_TIME_AT_ALL)

    await dispatcher.feed_update(
        bot, make_reply_update("как-нибудь потом", reply_to_message_id=ACK_MESSAGE_ID)
    )

    assert uow.reminders == []
    assert "не понял" in session.texts[-1].lower()


async def test_reply_to_something_else_is_an_ordinary_thought(
    dispatcher: Dispatcher, bot: Bot, uow: FakeUnitOfWork
) -> None:
    """Ответ на постороннее сообщение — обычная запись, а не ответ на вопрос."""
    await dispatcher.feed_update(
        bot, make_reply_update("просто мысль", reply_to_message_id=555_000)
    )

    assert [draft.raw_text for draft in uow.entries] == ["просто мысль"]


# --- Чужой не нажимает наши кнопки ---


async def test_stranger_cannot_press_the_buttons(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, uow: FakeUnitOfWork
) -> None:
    """Whitelist обязан стоять и на нажатиях, а не только на сообщениях."""
    await _capture(dispatcher, bot, LOST_TO_STT)
    _, data = session.last_buttons()[0]
    before = len(session.calls)

    await dispatcher.feed_update(
        bot, make_callback_update(data, message_id=ACK_MESSAGE_ID, user_id=STRANGER_ID)
    )

    assert uow.reminders == []
    assert len(session.calls) == before, "чужому что-то ответили"


async def test_question_survives_a_restart(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, uow: FakeUnitOfWork
) -> None:
    """Состояние вопроса не живёт в памяти процесса — оно в самой записи.

    Свежий диспетчер, ничего не знающий о заданном вопросе, обязан
    обработать нажатие: всё нужное приехало в `callback_data`.
    """
    await _capture(dispatcher, bot, LOST_TO_STT)
    _, data = session.last_buttons()[0]

    await asyncio.sleep(0)
    await dispatcher.feed_update(bot, make_callback_update(data, message_id=ACK_MESSAGE_ID))

    assert len(uow.reminders) == 1
