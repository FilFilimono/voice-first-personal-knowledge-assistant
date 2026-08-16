"""`Notifier` поверх aiogram.

Через него воркер редактирует подтверждение приёма, ничего не зная о Telegram.
"""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Отправка и правка сообщений."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(self, chat_id: int, text: str) -> int:
        message = await self._bot.send_message(chat_id, text)
        return message.message_id

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        """Заменить текст подтверждения на готовый результат.

        Telegram отвечает ошибкой, если новый текст совпадает со старым или
        сообщение уже удалено. Ни то ни другое не повод считать задание
        упавшим и повторять расшифровку — она уже сохранена.
        """
        try:
            await self._bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
        except TelegramBadRequest as error:
            logger.warning("не удалось отредактировать сообщение %s: %s", message_id, error)
