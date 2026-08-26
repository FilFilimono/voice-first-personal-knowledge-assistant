"""Бот без Telegram.

Подменяется транспорт — всё остальное настоящее: тот же `Dispatcher`, тот же
whitelist, те же обработчики. Иначе проверялись бы наши представления о том,
как aiogram разбирает апдейт, а не сам разбор.

Сессия запоминает каждый вызов, поэтому в тесте видно и порядок обращений
к Telegram, и время каждого из них.
"""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    DeleteMessage,
    EditMessageText,
    GetMe,
    GetUpdates,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    Update,
    User,
    Voice,
)

OWNER_ID = 111
STRANGER_ID = 222
CHAT_ID = 111


class RecordedCall:
    """Один вызов Telegram: что позвали и через сколько после старта."""

    def __init__(self, method: TelegramMethod[Any], elapsed: float) -> None:
        self.method = method
        self.elapsed = elapsed

    @property
    def name(self) -> str:
        return type(self.method).__name__


class FakeSession(BaseSession):
    """Транспорт, который никуда не ходит и всё запоминает."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[RecordedCall] = []
        self._started = time.perf_counter()
        self._next_message_id = 900

    def reset_clock(self) -> None:
        self._started = time.perf_counter()

    def calls_named(self, name: str) -> list[RecordedCall]:
        return [call for call in self.calls if call.name == name]

    @property
    def texts(self) -> list[str]:
        """Всё, что бот сказал: и отправленное, и то, чем заменил."""
        return [
            str(getattr(call.method, "text", ""))
            for call in self.calls
            if hasattr(call.method, "text")
        ]

    def last_buttons(self) -> list[tuple[str, str]]:
        """Кнопки под последним сообщением: подпись и то, что вернётся в ответ.

        Пусто — кнопок не было. Отличать «кнопок нет» от «кнопки другие»
        обязательно: открытый вопрос «когда напомнить?» ждёт свободного текста,
        и кнопка под ним обещала бы не то.
        """
        for call in reversed(self.calls):
            markup = getattr(call.method, "reply_markup", None)
            if markup is None:
                continue
            return [
                (button.text, button.callback_data or "")
                for row in markup.inline_keyboard
                for button in row
            ]
        return []

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        self.calls.append(RecordedCall(method, time.perf_counter() - self._started))

        if isinstance(method, SendMessage | EditMessageText):
            self._next_message_id += 1
            return make_message(
                message_id=self._next_message_id,
                text=str(getattr(method, "text", "")),
            ).as_(bot)
        if isinstance(method, DeleteMessage):
            return True
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> Any:
        yield b""


def make_message(
    *,
    message_id: int = 10,
    text: str | None = None,
    voice: Voice | None = None,
    user_id: int = OWNER_ID,
) -> Message:
    """Сообщение так, как его собрал бы aiogram из апдейта."""
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Владелец"),
        text=text,
        voice=voice,
    )


def make_voice(duration: int = 12) -> Voice:
    return Voice(file_id="file-abc", file_unique_id="uniq-abc", duration=duration)


def make_update(message: Message, update_id: int = 1) -> Update:
    return Update(update_id=update_id, message=message)


def make_reply_update(
    text: str,
    *,
    reply_to_message_id: int,
    update_id: int = 2,
    user_id: int = OWNER_ID,
) -> Update:
    """Ответ владельца **на сообщение бота** — так приходит ответ на вопрос.

    Связка держится на `reply_to_message`: идентификатор сообщения, на которое
    отвечают, — это подтверждение приёма, а оно записано в `entries.meta`.
    """
    message = make_message(message_id=970, text=text, user_id=user_id)
    return Update(
        update_id=update_id,
        message=message.model_copy(
            update={
                "reply_to_message": Message(
                    message_id=reply_to_message_id,
                    date=datetime.now(UTC),
                    chat=Chat(id=CHAT_ID, type="private"),
                    from_user=User(id=999, is_bot=True, first_name="Brain"),
                    text="Похоже на напоминание.",
                )
            }
        ),
    )


def make_callback_update(
    data: str,
    *,
    message_id: int,
    update_id: int = 3,
    user_id: int = OWNER_ID,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Update:
    """Нажатие кнопки под сообщением бота.

    Кнопки сообщения приезжают вместе с нажатием — так их и присылает Telegram.
    Обработчику сводки это не удобство, а вход: он перерисовывает ряд одного
    дела и остальные ряды берёт из того, что пришло. Без `reply_markup`
    проверялась бы работа с сообщением, у которого кнопок нет, — а это другой,
    запасной путь.
    """
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=f"cb-{update_id}",
            from_user=User(id=user_id, is_bot=False, first_name="Владелец"),
            chat_instance="chat-instance",
            data=data,
            message=Message(
                message_id=message_id,
                date=datetime.now(UTC),
                chat=Chat(id=CHAT_ID, type="private"),
                from_user=User(id=999, is_bot=True, first_name="Brain"),
                text="Похоже на напоминание.",
                reply_markup=reply_markup,
            ),
        ),
    )


def make_bot(session: FakeSession) -> Bot:
    return Bot(token="123:test-token", session=session)


class PollingSession(FakeSession):
    """Транспорт для настоящего цикла `start_polling`.

    Нужен потому, что подтверждение апдейта делает не наш код, а цикл aiogram:
    он двигает `offset`, и следующий запрос `getUpdates` говорит Telegram
    «эти можешь забыть». Проверять это на своей имитации цикла бессмысленно —
    имитация подтвердит ровно то, что мы про неё думаем.

    Сессия запоминает каждый `getUpdates` вместе с тем, сколько записей к тому
    моменту уже дошло до базы. По этой паре и видно, подтверждаются апдейты
    до записи или после.
    """

    def __init__(self, updates: list[Update], stored: Callable[[], int]) -> None:
        super().__init__()
        self._updates = updates
        self._stored = stored
        # (offset, сколько записей сохранено к моменту запроса)
        self.polls: list[tuple[int, int]] = []
        self.confirmed = asyncio.Event()

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="Brain", username="brain_bot")

        if isinstance(method, GetUpdates):
            offset = method.offset or 0
            self.polls.append((offset, self._stored()))
            if offset == 0:
                return self._updates
            # Апдейты кончились, а offset уехал вперёд — значит пачка уже
            # подтверждена. Дальше держим долгий опрос, пока тест не остановит.
            self.confirmed.set()
            await asyncio.sleep(LONG_POLL)
            return []

        return await super().make_request(bot, method, timeout)


# Столько «висит» долгий опрос после того, как апдейты кончились. Тест
# столько не ждёт — он снимает задачу, как только увидел подтверждение.
LONG_POLL = 3600
