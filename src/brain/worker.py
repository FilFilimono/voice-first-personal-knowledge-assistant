"""Рабочий цикл: расшифровка, напоминания, ретеншен аудио.

Запускается отдельным процессом (`just worker`), а не внутри бота. Причина
одна: подтверждение приёма должно приходить быстрее 300 мс, а расшифровка
занимает секунды. В одном процессе они бы конкурировали за один event loop
именно в тот момент, когда владелец смотрит в телефон.

Расписание живёт здесь же — отдельного планировщика в проекте нет (ADR-0005).
Обратная сторона: воркер стоит — стоит и cron. Пропущенные запуски догоняются
при следующем старте, потому что «удалить файлы старше тридцати дней» —
операция, которой всё равно, когда её позвали.
"""

import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import asyncpg
from aiogram import Bot
from pgqueuer import PgQueuer
from pgqueuer.db import AsyncpgDriver
from pgqueuer.executors import DatabaseRetryEntrypointExecutor, EntrypointExecutorParameters
from pgqueuer.models import Job, Schedule

from brain.adapters.telegram.audio import TelegramAudioSource
from brain.adapters.telegram.notifier import TelegramNotifier
from brain.config import get_runtime_settings
from brain.core import stt
from brain.core.scheduler import jobs
from brain.core.scheduler.queue import (
    AUDIO_RETENTION,
    AUDIO_RETENTION_SCHEDULE,
    REMINDER,
    TRANSCRIBE,
)
from brain.storage.db import asyncpg_dsn

logger = logging.getLogger(__name__)

# Ретраи расшифровки: пять попыток с удвоением задержки, то есть около получаса
# на то, чтобы провайдер очнулся. Дольше ждать смысла нет — если он не ответил
# за полчаса, не ответит и за час, а запись всё это время цела и с аудио.
MAX_ATTEMPTS = 5
INITIAL_DELAY = timedelta(seconds=10)
MAX_DELAY = timedelta(minutes=10)
BACKOFF_MULTIPLIER = 2.0


def retry_executor(
    parameters: EntrypointExecutorParameters,
) -> DatabaseRetryEntrypointExecutor:
    """Ретраи со счётчиком в базе — переживают перезапуск воркера."""
    return DatabaseRetryEntrypointExecutor(
        parameters,
        max_attempts=MAX_ATTEMPTS,
        initial_delay=INITIAL_DELAY,
        max_delay=MAX_DELAY,
        backoff_multiplier=BACKOFF_MULTIPLIER,
    )


def payload_of(job: Job) -> dict[str, Any]:
    """Разобрать полезную нагрузку задания. В ней только идентификаторы."""
    if job.payload is None:
        raise RuntimeError(f"задание {job.id} без нагрузки")
    parsed: dict[str, Any] = json.loads(job.payload)
    return parsed


@asynccontextmanager
async def create_app() -> AsyncGenerator[PgQueuer]:
    """Фабрика для `pgq run brain.worker:create_app`."""
    settings = get_runtime_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    connection = await asyncpg.connect(dsn=asyncpg_dsn())
    bot = Bot(token=settings.telegram_bot_token)

    notifier = TelegramNotifier(bot)
    audio_source = TelegramAudioSource(bot)
    transcriber = stt.build_transcriber(settings)

    pgq = PgQueuer(AsyncpgDriver(connection))

    @pgq.qm.entrypoint(TRANSCRIBE, executor_factory=retry_executor)
    async def transcribe(job: Job) -> None:
        payload = payload_of(job)
        await jobs.transcribe(
            entry_id=payload["entry_id"],
            chat_id=payload["chat_id"],
            ack_message_id=payload["ack_message_id"],
            transcriber=transcriber,
            audio_source=audio_source,
            notifier=notifier,
            max_duration_sec=settings.stt_max_duration_sec,
        )

    @pgq.qm.entrypoint(REMINDER, executor_factory=retry_executor)
    async def remind(job: Job) -> None:
        payload = payload_of(job)
        await jobs.remind(
            reminder_id=payload["reminder_id"],
            chat_id=payload["chat_id"],
            notifier=notifier,
        )

    @pgq.sm.schedule(AUDIO_RETENTION, AUDIO_RETENTION_SCHEDULE)
    async def clean_audio(schedule: Schedule) -> None:
        await jobs.clean_audio(retention_days=settings.audio_retention_days)

    logger.info("воркер запущен: окружение %s", settings.brain_env)
    try:
        yield pgq
    finally:
        await bot.session.close()
        await connection.close()
