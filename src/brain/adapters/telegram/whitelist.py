"""Кого бот вообще слушает.

Всё, что пришло не от владельца, игнорируется **молча**: ответ «вам сюда
нельзя» подтверждает, что бот живой и чей-то личный, а это ровно то, чего
не надо сообщать случайно нашедшему.

Пустой whitelist означает «никого». Безопасный дефолт важнее удобства:
забытая переменная не должна незаметно открыть архив всему миру. Чтобы это
не выглядело как поломка, бот пишет об этом в лог при старте.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

logger = logging.getLogger(__name__)


class WhitelistMiddleware(BaseMiddleware):
    """Пропускает дальше только сообщения из списка разрешённых отправителей."""

    def __init__(self, allowed_user_ids: list[int]) -> None:
        self._allowed = frozenset(allowed_user_ids)
        if not self._allowed:
            logger.warning(
                "TELEGRAM_ALLOWED_USER_IDS пуст: бот будет игнорировать всех. "
                "Снаружи это неотличимо от сломанного бота — заполните список."
            )

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or user.id not in self._allowed:
            logger.info("сообщение проигнорировано: отправитель %s не в whitelist", user)
            return None
        return await handler(event, data)
