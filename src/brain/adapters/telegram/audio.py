"""Скачивание голосовых из Telegram.

Реализация `AudioSource`. Живёт в адаптере, потому что «идентификатор файла»
— понятие Telegram: ядро о нём не знает и знать не должно.
"""

import io

from aiogram import Bot


class TelegramAudioSource:
    """Файл голосового по его идентификатору."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def download(self, file_id: str) -> bytes:
        buffer = io.BytesIO()
        await self._bot.download(file_id, destination=buffer)
        return buffer.getvalue()
