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
from aiogram.types import CallbackQuery, Message

import brain.core.retrieval.embeddings as embeddings
from brain.adapters.telegram import search_text, status_text
from brain.adapters.telegram.notifier import CALLBACK_PREFIX, NO, yes_no_keyboard
from brain.config import Settings
from brain.core import ingest
from brain.core import status as core_status
from brain.core.memory import repository
from brain.core.memory.unit_of_work import capture_transaction
from brain.core.retrieval import search
from brain.core.scheduler import reminders

logger = logging.getLogger(__name__)

ACK = "Принял."
STORED = "Записал."
CAPTURE_FAILED = "Не смог сохранить — повторите, пожалуйста."
ALREADY_SCHEDULED = "Это напоминание уже заведено."
QUESTION_LOST = "Не нашёл запись, к которой был вопрос — посмотрите логи."
PRIVATE_VOICE_REFUSED = (
    "Приватный режим — только текст.\n"
    "Расшифровка идёт через внешний сервис, поэтому голосовое пришлось бы "
    "отправить наружу. Наберите текстом."
)
SEARCH_FAILED = "Поиск не отработал — посмотрите логи. Записи целы."

# Сколько результатов показывать. Пять — потому что recall меряется на пяти
# (ADR-0006), и показывать больше, чем меряешь, значит не знать цену выдачи.
RESULTS = 5

HELP = (
    "Пишите или наговаривайте — сохраню.\n"
    "Скажите «напомни завтра в 10 …» — напомню.\n\n"
    "/find <что искать> — поиск по архиву\n"
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

    @router.message(Command("find"))
    async def find(message: Message, command: CommandObject) -> None:
        """Поиск по архиву.

        Командой, а не просто словом «найди» в начале сообщения. Обычное
        сообщение обязано всегда записываться: перехват сделал бы захват
        капризным — а захват это единственное, что в этой системе не имеет
        права работать через раз.
        """
        query = (command.args or "").strip()
        if not query:
            await message.answer(search_text.EMPTY_QUERY)
            return

        embedder = embeddings.build_embedder(settings)
        try:
            found = await search.search(embedder, query, limit=RESULTS)
        except Exception:
            # Поиск ходит наружу за вектором запроса, и провайдер падает.
            # Молчать нельзя: владелец решит, что в архиве ничего нет.
            logger.exception("поиск не отработал: %r", query)
            await message.answer(SEARCH_FAILED)
            return

        await message.answer(search_text.render(query, found, today=datetime.now(UTC)))

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
        # Ответ на вопрос бота — это реплика диалога, а не новая мысль.
        # Проверяется до подтверждения приёма: «сегодня в 19:00» не должно
        # ни лечь в архив, ни получить «Записал.».
        if await _answered_a_question(message):
            return

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

        if result.ask is reminders.Ask.CONFIRM and result.entry_id is not None:
            # Время уже разобрано — остаётся подтвердить. Открытый вопрос
            # («когда напомнить?») кнопок не получает: ответ там свободный,
            # и кнопка под ним обещала бы не то.
            await ack.edit_text(result.reply, reply_markup=yes_no_keyboard(result.entry_id))
            return

        await ack.edit_text(result.reply)

    async def _answered_a_question(message: Message) -> bool:
        """Разобрать ответ на «когда напомнить?». `False` — это не ответ.

        Запись ищется по сообщению, на которое отвечают: вопрос — это
        отредактированное подтверждение приёма, и его идентификатор лежит
        в `entries.meta`. Поэтому ни таблицы ожидающих вопросов, ни состояния
        в памяти процесса не нужно.
        """
        replied_to = message.reply_to_message
        if replied_to is None or message.text is None:
            return False

        found = await repository.entry_by_ack_message(message.chat.id, replied_to.message_id)
        if found is None:
            return False

        entry_id, subject = found
        if await repository.has_reminder(entry_id):
            return False

        async with capture_transaction() as uow:
            outcome = await reminders.schedule_confirmed(
                uow,
                entry_id=entry_id,
                chat_id=message.chat.id,
                subject=subject,
                when=message.text,
                timezone=settings.brain_timezone,
                now=datetime.now(UTC),
            )

        await replied_to.edit_text(_answered(outcome))
        return True

    @router.callback_query(F.data.startswith(f"{CALLBACK_PREFIX}:"))
    async def answer_question(callback: CallbackQuery) -> None:
        """Нажатие «Да» или «Нет» под вопросом «это напоминание?»."""
        await callback.answer()

        if callback.data is None or callback.message is None:  # pragma: no cover — фильтр выше
            return

        _, answer, raw_entry_id = callback.data.split(":", maxsplit=2)
        entry_id = int(raw_entry_id)
        question = callback.message

        if not isinstance(question, Message):  # pragma: no cover — слишком старое сообщение
            return

        if answer == NO:
            await question.edit_text(STORED)
            return

        # Нажать кнопку можно сколько угодно раз: она живёт в переписке вечно.
        # Второе напоминание об одном и том же раздражает сильнее, чем его
        # отсутствие, — та же причина, что и у повтора задания расшифровки.
        if await repository.has_reminder(entry_id):
            await question.edit_text(ALREADY_SCHEDULED)
            return

        found = await repository.entry_by_ack_message(question.chat.id, question.message_id)
        if found is None:
            logger.error("ответ на вопрос без записи: entry_id=%s", entry_id)
            await question.edit_text(QUESTION_LOST)
            return

        _, subject = found
        async with capture_transaction() as uow:
            outcome = await reminders.schedule_confirmed(
                uow,
                entry_id=entry_id,
                chat_id=question.chat.id,
                # «Да» означает «время в записи верное» — оттуда его и берём.
                subject=subject,
                when=subject,
                timezone=settings.brain_timezone,
                now=datetime.now(UTC),
            )

        await question.edit_text(_answered(outcome))

    return router


def _answered(outcome: reminders.ScheduleOutcome) -> str:
    """Что показать вместо вопроса, когда на него ответили."""
    return f"⏰ {outcome.message}" if outcome.created else outcome.message
