"""Сообщения, отправленные пока бот лежал, не должны пропадать.

Telegram хранит недоставленные апдейты около суток и отдаёт их при следующем
`getUpdates`. Проект ничего не сбрасывает: `drop_pending_updates` живёт только
у `deleteWebhook`/`setWebhook`, а их бот не зовёт вовсе — так что накопленное
приходит. Проверено чтением aiogram 3.30, не памятью.

Ломается это дальше. Апдейт считается доставленным не тогда, когда мы его
обработали, а тогда, когда цикл aiogram попросил следующую пачку с увеличенным
`offset`, — Telegram по этому запросу помечает предыдущие подтверждёнными
и больше не отдаёт никогда. При `handle_as_tasks=True` (умолчание) обработчик
уходит в фоновую задачу, а `offset` двигается сразу: подтверждение обгоняет
запись в базу. Накопленная за ночь пачка подтверждается за миллисекунды,
а пишется секундами — и всё, что не успело, исчезает у Telegram навсегда.

Отсюда единственное утверждение этого файла: **апдейт подтверждается только
после того, как он записан.** Цена — сообщения обрабатываются по очереди,
а не разом; для одного пользователя это ровно то, что нужно.

Обратная сторона тоже проверяется: если процесс убить на полпути, апдейт
останется неподтверждённым и придёт снова. Повторную доставку ловит
дедупликация по `chat_id:message_id` — она уже есть, и вместе с ней
доставка становится «хотя бы раз», а не «не более раза».
"""

import asyncio
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager

import pytest
from aiogram import Bot, Dispatcher

from brain.adapters.telegram import bot as bot_module
from brain.config import Settings
from tests.fakes import FakeUnitOfWork
from tests.telegram_harness import (
    OWNER_ID,
    PollingSession,
    make_bot,
    make_message,
    make_update,
)

# Задержка базы. Без неё запись успевает пройти между двумя запросами
# и гонка не видна — а в жизни база отвечает не мгновенно.
SLOW_DATABASE = 0.2

# Сколько ждать подтверждения, прежде чем считать тест зависшим.
PATIENCE = 5.0


@pytest.fixture
def uow() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture
def dispatcher(monkeypatch: pytest.MonkeyPatch, uow: FakeUnitOfWork) -> Dispatcher:
    """Настоящий диспетчер с медленной записью вместо базы."""

    @asynccontextmanager
    async def fake_transaction() -> AsyncGenerator[FakeUnitOfWork]:
        await asyncio.sleep(SLOW_DATABASE)
        yield uow

    async def not_private() -> bool:
        return False

    monkeypatch.setattr("brain.adapters.telegram.handlers.capture_transaction", fake_transaction)
    monkeypatch.setattr("brain.adapters.telegram.handlers.repository.is_private_mode", not_private)

    settings = Settings(
        _env_file=None,
        telegram_bot_token="123:test-token",
        google_api_key="AI-test",
        telegram_allowed_user_ids=[OWNER_ID],
    )
    return bot_module.build_dispatcher(settings)


@pytest.fixture
def backlog() -> Iterator[list[int]]:
    """Три сообщения, накопленных пока ноутбук был закрыт."""
    yield [101, 102, 103]


@pytest.fixture
def session(backlog: list[int], uow: FakeUnitOfWork) -> PollingSession:
    updates = [
        make_update(
            make_message(text=f"мысль {number}", message_id=number),
            update_id=number,
        )
        for number in backlog
    ]
    return PollingSession(updates, stored=lambda: len(uow.entries))


@pytest.fixture
def bot(session: PollingSession) -> Bot:
    return make_bot(session)


async def _poll_until_confirmed(dispatcher: Dispatcher, bot: Bot, session: PollingSession) -> None:
    """Погонять настоящий опрос, пока пачка не будет подтверждена Telegram."""
    polling = asyncio.create_task(bot_module.poll(dispatcher, bot))
    try:
        async with asyncio.timeout(PATIENCE):
            await session.confirmed.wait()
    finally:
        polling.cancel()
        await asyncio.gather(polling, return_exceptions=True)


async def test_backlog_is_confirmed_only_after_it_is_stored(
    dispatcher: Dispatcher,
    bot: Bot,
    session: PollingSession,
    uow: FakeUnitOfWork,
    backlog: list[int],
) -> None:
    """Пачка, накопленная за простой, доходит до базы прежде, чем её забудет Telegram.

    Именно здесь терялись сообщения, отправленные при закрытом ноутбуке:
    подтверждение уходило раньше записи, и второго шанса у них не было —
    повторно Telegram подтверждённый апдейт не отдаёт.
    """
    await _poll_until_confirmed(dispatcher, bot, session)

    # Запрос, которым пачка была подтверждена: offset уехал за последний апдейт.
    confirming = [poll for poll in session.polls if poll[0] > backlog[-1]]
    assert confirming, "пачка так и не была подтверждена — тест ничего не проверил"

    _, stored_by_then = confirming[0]
    assert stored_by_then == len(backlog), (
        f"Telegram забыл пачку, когда в базе лежало {stored_by_then} из {len(backlog)}"
    )


async def test_whole_backlog_is_stored(
    dispatcher: Dispatcher,
    bot: Bot,
    session: PollingSession,
    uow: FakeUnitOfWork,
    backlog: list[int],
) -> None:
    """Ни одна накопленная мысль не пропала по дороге."""
    await _poll_until_confirmed(dispatcher, bot, session)

    assert [entry.raw_text for entry in uow.entries] == [f"мысль {number}" for number in backlog]
