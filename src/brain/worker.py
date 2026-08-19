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
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import asyncpg
from aiogram import Bot
from pgqueuer import PgQueuer
from pgqueuer.db import AsyncpgDriver
from pgqueuer.executors import DatabaseRetryEntrypointExecutor, EntrypointExecutorParameters
from pgqueuer.models import Job, Schedule

import brain.core.retrieval.embeddings as embeddings
from brain.adapters.telegram.audio import TelegramAudioSource
from brain.adapters.telegram.notifier import TelegramNotifier
from brain.config import get_runtime_settings
from brain.core import llm, stt
from brain.core.scheduler import jobs
from brain.core.scheduler.queue import (
    AUDIO_RETENTION,
    AUDIO_RETENTION_SCHEDULE,
    ENRICH,
    HEARTBEAT,
    HEARTBEAT_SCHEDULE,
    INDEX,
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
    embedder = embeddings.build_embedder(settings)
    model = llm.build_model(settings)

    # Проверка моделей — до того, как воркер возьмёт первое задание. Иначе
    # неверное имя выясняется на первом голосовом, пятью попытками подряд
    # и уже после того, как владелец что-то наговорил. Обе проверки —
    # настоящие вызовы: список моделей 16 августа 2026 показывал снятую
    # с обслуживания модель как рабочую, и правду сказал только вызов.
    async def _check(what: str, setting: str, check: Callable[[], Awaitable[None]]) -> None:
        try:
            await check()
        except (
            stt.ProviderUnavailableError,
            embeddings.ProviderUnavailableError,
            llm.ProviderUnavailableError,
        ) as error:
            await bot.session.close()
            await connection.close()
            raise SystemExit(
                f"\nВоркер не запущен.\n{error}\n\nПоправьте {setting} в .env.\n"
            ) from None
        logger.info("модель %s доступна", what)

    await _check(
        f"расшифровки {transcriber.provider}/{transcriber.model}",
        "STT_MODEL",
        transcriber.check_available,
    )
    await _check(
        f"эмбеддингов {embedder.provider}/{embedder.model}",
        "EMBEDDING_MODEL",
        embedder.check_available,
    )
    # Третья проверка — по той же причине, что первые две, плюс одна своя:
    # она заодно проверяет структурированный ответ. Модель может отвечать
    # на generateContent и не уметь responseSchema, и выяснять это на первой
    # пачке было бы поздно — записи к тому моменту уже накопились.
    await _check(
        f"разметки {model.provider}/{model.model}",
        "ENRICH_MODEL",
        model.check_available,
    )

    pgq = PgQueuer(AsyncpgDriver(connection))

    # on_failure="hold": исчерпав попытки, задание остаётся в очереди
    # со статусом failed, а не исчезает. Запись при этом цела в любом случае
    # (инвариант 7), но удалённое задание значит, что расшифровки не будет
    # никогда и никто об этом не узнает. Held-задание видно в `pgq failed`
    # и возвращается в работу `pgq requeue`.
    @pgq.qm.entrypoint(TRANSCRIBE, executor_factory=retry_executor, on_failure="hold")
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

    @pgq.qm.entrypoint(INDEX, executor_factory=retry_executor, on_failure="hold")
    async def index(job: Job) -> None:
        payload = payload_of(job)
        await jobs.index(entry_id=payload["entry_id"], embedder=embedder)

    @pgq.qm.entrypoint(ENRICH, executor_factory=retry_executor, on_failure="hold")
    async def enrich(job: Job) -> None:
        payload = payload_of(job)
        await jobs.enrich(
            entry_id=payload["entry_id"],
            model=model,
            embedder=embedder,
            notifier=notifier,
            limit=settings.enrich_batch_limit,
        )

    @pgq.qm.entrypoint(REMINDER, executor_factory=retry_executor, on_failure="hold")
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

    @pgq.sm.schedule(HEARTBEAT, HEARTBEAT_SCHEDULE)
    async def heartbeat(schedule: Schedule) -> None:
        """Пульс. Ничего не делает — важен сам факт срабатывания.

        Библиотека обновляет `last_run` у расписания, и по этой отметке
        `/status` отличает спящий воркер от мёртвого. Без пульса пустая
        очередь выглядит одинаково в обоих случаях.
        """

    logger.info("воркер запущен: окружение %s", settings.brain_env)
    try:
        yield pgq
    finally:
        await bot.session.close()
        await connection.close()
