"""Обработчики сообщений.

Порядок в каждом обработчике один и тот же и выбран не случайно:

    подтверждение → транзакция → правка подтверждения

Подтверждение уходит первым, потому что оно должно прийти быстрее 300 мс:
мысль записывается на ходу, и ожидание ответа — единственное, что заставляет
владельца смотреть в телефон дольше, чем нужно.

Идентификатор подтверждения нужен внутри задания, а до отправки его
не существует — отсюда и порядок. Инвариант 7 при этом цел: подтверждение
лежит **вне** транзакции, а запись и задание пишутся вместе. Если транзакция
упала, то же самое сообщение превращается в объяснение, и молчания не возникает.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from brain.adapters.telegram import status_text
from brain.config import Settings
from brain.core import ingest
from brain.core import status as core_status
from brain.core.memory import repository
from brain.core.memory.unit_of_work import capture_transaction

logger = logging.getLogger(__name__)

ACK = "Принял."
CAPTURE_FAILED = "Не смог сохранить — повторите, пожалуйста."
PRIVATE_VOICE_REFUSED = (
    "Приватный режим — только текст.\n"
    "Расшифровка идёт через внешний сервис, поэтому голосовое пришлось бы "
    "отправить наружу. Наберите текстом."
)
HELP = (
    "Пишите или наговаривайте — сохраню.\n"
    "Скажите «напомни завтра в 10 …» — напомню.\n\n"
    "/status — всё ли работает\n"
    "/private on — приватный режим: записи не уходят наружу, голосовые "
    "не принимаются\n"
    "/private off — обычный режим\n"
    "/private — что сейчас"
)


def build_router(settings: Settings) -> Router:
    """Собрать роутер. Настройки замыкаются, а не читаются в каждом обработчике."""
    router = Router(name="capture")
    audio_dir = Path(settings.audio_dir)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer(HELP)

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        """Что сейчас происходит — чтобы не лезть в терминал с телефона."""
        collected = await core_status.collect()
        await message.answer(status_text.render(collected, timezone=settings.brain_timezone))

    @router.message(Command("private"))
    async def private_mode(message: Message, command: CommandObject) -> None:
        argument = (command.args or "").strip().lower()

        if argument not in {"on", "off"}:
            state = "включён" if await repository.is_private_mode() else "выключен"
            await message.answer(f"Приватный режим {state}.\n\n{HELP}")
            return

        enabled = argument == "on"
        await repository.switch_private_mode(enabled=enabled)
        await message.answer(
            "Приватный режим включён. Голосовые не принимаю — только текст."
            if enabled
            else "Приватный режим выключен."
        )

    @router.message(F.voice)
    async def voice(message: Message) -> None:
        ack = await message.answer(ACK)

        # Проверка режима — до всего остального: в приватном режиме файл
        # не скачивается и запись не создаётся, наружу не уходит ничего.
        if await repository.is_private_mode():
            await ack.edit_text(PRIVATE_VOICE_REFUSED)
            return

        if message.voice is None:  # pragma: no cover — исключено фильтром F.voice
            return

        incoming = ingest.IncomingVoice(
            chat_id=message.chat.id,
            message_id=message.message_id,
            ack_message_id=ack.message_id,
            file_id=message.voice.file_id,
            duration_sec=message.voice.duration,
        )

        try:
            async with capture_transaction() as uow:
                result = await ingest.capture_voice(
                    uow,
                    incoming,
                    is_private=False,
                    timezone=settings.brain_timezone,
                    audio_dir=audio_dir,
                )
        except Exception:
            logger.exception("голосовое не сохранено")
            await ack.edit_text(CAPTURE_FAILED)
            return

        if result.duplicate:
            # Тот же апдейт пришёл дважды. Записи не прибавилось — и уведомления
            # тоже не должно: владелец отправлял сообщение один раз.
            await ack.delete()
            return

        # Подтверждение остаётся как есть: его отредактирует воркер,
        # когда расшифровка будет готова.

    @router.message(F.text)
    async def text(message: Message) -> None:
        ack = await message.answer(ACK)

        if message.text is None:  # pragma: no cover — исключено фильтром F.text
            return

        incoming = ingest.IncomingText(
            chat_id=message.chat.id,
            message_id=message.message_id,
            ack_message_id=ack.message_id,
            text=message.text,
        )

        try:
            async with capture_transaction() as uow:
                result = await ingest.capture_text(
                    uow,
                    incoming,
                    is_private=await repository.is_private_mode(),
                    timezone=settings.brain_timezone,
                    now=datetime.now(UTC),
                )
        except Exception:
            logger.exception("запись не сохранена")
            await ack.edit_text(CAPTURE_FAILED)
            return

        if result.duplicate:
            await ack.delete()
            return

        await ack.edit_text(result.reply)

    return router
