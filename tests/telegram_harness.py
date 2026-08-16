"""Бот без Telegram.

Подменяется транспорт — всё остальное настоящее: тот же `Dispatcher`, тот же
whitelist, те же обработчики. Иначе проверялись бы наши представления о том,
как aiogram разбирает апдейт, а не сам разбор.

Сессия запоминает каждый вызов, поэтому в тесте видно и порядок обращений
к Telegram, и время каждого из них.
"""

import time
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    DeleteMessage,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import Chat, Message, Update, User, Voice

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


def make_bot(session: FakeSession) -> Bot:
    return Bot(token="123:test-token", session=session)
