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
from dataclasses import dataclass
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
from brain.core.provider_errors import ProviderError, advice
from brain.core.scheduler import jobs
from brain.core.scheduler.queue import (
    AUDIO_RETENTION,
    AUDIO_RETENTION_SCHEDULE,
    CLEANUP,
    ENRICH,
    HEARTBEAT,
    HEARTBEAT_SCHEDULE,
    INDEX,
    REGENERALIZE,
    REGENERALIZE_SCHEDULE,
    REMINDER,
    SHOW_TRANSCRIPT,
    TOUCH,
    TOUCH_SCHEDULE,
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


@dataclass(frozen=True, slots=True)
class ModelCheck:
    """Модель, которую при старте проверяют настоящим вызовом."""

    # Как назвать её владельцу: «расшифровки gemini/...».
    what: str
    # Настройка, которую он пойдёт править, если дело в ней.
    setting: str
    check: Callable[[], Awaitable[None]]
    # Имеет ли отказ этой модели право остановить воркер.
    #
    # `False` стоит у **косметических** моделей, и правило у них другое
    # не по недосмотру. Работа без чистки — рабочая система: расшифровка
    # приходит, напоминания приходят, владелец просто читает свои «э».
    # Работа без расшифровки — не система вовсе. Поэтому неверное имя
    # в `ENRICH_MODEL` обязано валить старт громко, а такое же неверное имя
    # в `CLEANUP_MODEL` — нет: иначе опечатка в косметике кладёт «заказанное»,
    # о чём система не имеет права молчать (`vision.md` §7). Это тот же вывод,
    # что у ADR-0014 про квоту, и пункт 6 ADR-0015 распространяет его
    # на настроечные отказы: под нехваткой чего угодно косметика отключается
    # первой, а расшифровка и напоминания не трогаются никогда.
    #
    # Проверять косметическую модель при старте всё равно стоит: неверное имя
    # видно сразу, в логе и одной строкой, а не через сутки по молчанию чистки.
    vital: bool = True


async def check_models(checks: list[ModelCheck]) -> list[str]:
    """Проверить модели вызовом. Вернуть предупреждения о том, что не отвечает.

    Проверка обязана быть **настоящим вызовом**, а не сверкой со списком
    моделей: 16 августа 2026 список показывал снятую с обслуживания модель
    как рабочую, и правду сказал только вызов. Это в силе.

    Изменилось другое, и изменилось 20 августа 2026. Кончилась бесплатная
    суточная квота эмбеддингов, провайдер ответил `429` — и воркер не поднялся
    вовсе. Вместе с эмбеддингами встали расшифровка, напоминания и утренняя
    сводка, то есть в том числе «заказанное», о чём система не имеет права
    молчать (`vision.md` §7). Квота при этом проходит сама к полуночи
    по тихоокеанскому времени.

    Поэтому теперь: **временный отказ — предупреждение, настроечный —
    остановка.** Задания, которым нужна отказавшая модель, уйдут в ретраи
    очереди и не потеряются (инвариант 7); сколько запросов потрачено сегодня,
    видно в `/status`.

    У косметических моделей (`vital=False`) остановки нет вовсе — ни на каком
    отказе. Причина в `ModelCheck.vital`: работа без чистки — рабочая система,
    работа без расшифровки — нет.

    Проверяются все модели, даже после первого предупреждения: иначе неверное
    имя второй выяснилось бы только назавтра, когда пройдёт квота у первой.
    """
    warnings: list[str] = []
    failure: tuple[str, str, ProviderError] | None = None

    for item in checks:
        try:
            await item.check()
        except ProviderError as raised:
            # Python стирает переменную исключения на выходе из `except`,
            # поэтому отказ сохраняется в отдельное имя.
            refusal = raised
            if refusal.fatal and item.vital:
                # Не выходим сразу: остальные модели тоже надо проверить,
                # чтобы владелец узнал обо всём за один запуск.
                failure = failure or (item.what, item.setting, refusal)
                continue
            # «Временно» говорится только там, где это правда. Настроечный
            # отказ косметической модели сам не пройдёт, и обещать обратное
            # значило бы научить владельца не читать это предупреждение.
            passing = "временно недоступна" if not refusal.fatal else "недоступна"
            warning = f"модель {item.what} {passing}: {refusal}\n{_advice(item.setting, refusal)}"
            logger.warning("%s", warning)
            warnings.append(warning)
            continue
        logger.info("модель %s доступна", item.what)

    if failure is not None:
        broken, setting, refusal = failure
        raise SystemExit(
            f"\nВоркер не запущен.\nМодель {broken}: {refusal}\n\n{_advice(setting, refusal)}\n"
        )

    return warnings


def _advice(setting: str, error: ProviderError) -> str:
    """Совет по коду ответа, а не один и тот же на все отказы."""
    return advice(error.status, setting, message=str(error))


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
    # Модель чистки — своя, а не та же, что у разметки: задачи разные, цена
    # ошибки разная, и выбираются они порознь (ADR-0015). Строится всегда,
    # даже с выключенной чисткой: `build_model` в сеть не ходит, а ветвление
    # здесь стоило бы `None` в трёх местах ниже.
    cleaner = llm.build_model(
        settings,
        model=settings.cleanup_model,
        thinking_level=settings.cleanup_thinking_level,
    )
    # Модель названий — тоже своя. Сегодня в `TITLES_MODEL` и `CLEANUP_MODEL`
    # стоит одно и то же имя, но снимать их надо порознь: вычищенный текст
    # правится следующим прогоном, а название лежит в базе и показывается
    # месяцами (ADR-0015, п. 4).
    #
    # Пробы при старте у неё нет, в отличие от трёх остальных: бесплатный тир
    # даёт 20 запросов в сутки на модель, а воркер в разработке
    # перезапускается по десятку раз в день — проба съедала бы половину
    # дневного бюджета названий ради строки в логе. Неверное имя видно первой
    # же разметкой, и разметку оно не роняет: `jobs._name_fresh` глотает отказ
    # названий нарочно.
    namer = llm.build_model(settings, model=settings.titles_model)
    # Модель утренней сводки — тоже своя, и пробы при старте у неё тоже нет.
    # Зовётся она раз в сутки: проба при каждом перезапуске воркера стоила бы
    # дороже самой сводки. Неверное имя видно первой же строкой в логе касаний,
    # и касания оно не роняет — сводка уходит сухим рендером (ADR-0015, п. 5).
    summariser = llm.build_model(settings, model=settings.digest_model)

    # Проверка моделей — до того, как воркер возьмёт первое задание. Иначе
    # неверное имя выясняется на первом голосовом, пятью попытками подряд
    # и уже после того, как владелец что-то наговорил. Все три — настоящие
    # вызовы: список моделей 16 августа 2026 показывал снятую с обслуживания
    # модель как рабочую, и правду сказал только вызов.
    #
    # Текстовые модели проверяются **настоящей схемой** — объектом со строковым
    # полем, как у чистки. Прежняя проба слала одно булево поле и ловила ровно
    # «модели нет»: 25 августа 2026 воркер написал «модель чистки доступна»
    # про модель, которая на рабочем запросе не отвечает и за 900 секунд.
    # Текст пробы при этом минимальный: проверяется, что модель умеет ответить
    # по схеме, а не то, как быстро она это делает.
    #
    # Что из этого останавливает воркер, а что нет — в `check_models`.
    try:
        await check_models(
            [
                ModelCheck(
                    f"расшифровки {transcriber.provider}/{transcriber.model}",
                    "STT_MODEL",
                    transcriber.check_available,
                ),
                ModelCheck(
                    f"эмбеддингов {embedder.provider}/{embedder.model}",
                    "EMBEDDING_MODEL",
                    embedder.check_available,
                ),
                ModelCheck(
                    f"разметки {model.provider}/{model.model}",
                    "ENRICH_MODEL",
                    # Проверка текстовой модели идёт **дверью** `core/llm`,
                    # а не методом провайдера: там ведётся учёт, а проба —
                    # такой же запрос против квоты, как рабочий. Пока она была
                    # методом, её вызовов в `llm_calls` не было вовсе.
                    lambda: llm.check_available(model),
                ),
                # Четвёртая проверка — чистка, и она **никогда не валит старт**,
                # даже на неверном имени модели: `vital=False`. Косметика
                # отключается первой, расшифровка и напоминания не трогаются
                # никогда (ADR-0015, пункт 6) — а `SystemExit` из-за опечатки
                # в `CLEANUP_MODEL` останавливал бы как раз их. Проверка при
                # этом остаётся: неверное имя видно сразу и одной строкой,
                # а не через сутки по молчанию чистки.
                *(
                    [
                        ModelCheck(
                            f"чистки {cleaner.provider}/{cleaner.model}",
                            "CLEANUP_MODEL",
                            lambda: llm.check_available(cleaner),
                            vital=False,
                        )
                    ]
                    if settings.cleanup_enabled
                    else []
                ),
            ]
        )
    except SystemExit:
        await bot.session.close()
        await connection.close()
        raise

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

    @pgq.qm.entrypoint(CLEANUP, executor_factory=retry_executor, on_failure="hold")
    async def cleanup(job: Job) -> None:
        """Чистка расшифровки. Задание ставит сама расшифровка, в своей транзакции."""
        payload = payload_of(job)
        await jobs.cleanup(
            entry_id=payload["entry_id"],
            chat_id=payload["chat_id"],
            ack_message_id=payload["ack_message_id"],
            model=cleaner,
            notifier=notifier,
        )

    @pgq.qm.entrypoint(SHOW_TRANSCRIPT, executor_factory=retry_executor, on_failure="hold")
    async def show_transcript(job: Job) -> None:
        """Срок ожидания чистки истёк — показать то, что есть.

        Это и есть ожидание из ADR-0015: отложенное задание, а не сон
        в обработчике расшифровки. Спящий обработчик держал бы слот воркера
        и не пережил бы перезапуск.
        """
        payload = payload_of(job)
        await jobs.show_transcript(
            entry_id=payload["entry_id"],
            chat_id=payload["chat_id"],
            ack_message_id=payload["ack_message_id"],
            notifier=notifier,
        )

    @pgq.qm.entrypoint(INDEX, executor_factory=retry_executor, on_failure="hold")
    async def index(job: Job) -> None:
        payload = payload_of(job)
        await jobs.index(entry_id=payload["entry_id"], embedder=embedder)

    @pgq.qm.entrypoint(ENRICH, executor_factory=retry_executor, on_failure="hold")
    async def enrich(job: Job) -> None:
        payload = payload_of(job)
        await jobs.enrich(
            # Перепрогон приходит со списком, обычная разметка — с одной
            # записью и разбирает всё неразмеченное разом.
            entry_id=payload.get("entry_id", 0),
            entry_ids=payload.get("entry_ids"),
            model=model,
            titler=namer,
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

    @pgq.sm.schedule(REGENERALIZE, REGENERALIZE_SCHEDULE)
    async def regeneralize(schedule: Schedule) -> None:
        """Переобобщение тем. Срабатывает по накоплению, а не по расписанию.

        Расписание здесь только повод посмотреть: сам проход считает, сколько
        записей появилось с прошлого раза, и на трёх новых не делает ничего.
        """
        await jobs.regeneralize_topics(model=model, embedder=embedder)

    @pgq.sm.schedule(TOUCH, TOUCH_SCHEDULE)
    async def touch(schedule: Schedule) -> None:
        """Касания: утренняя сводка, всплывание старого, вечерний разбор, ревью.

        Расписание частое, а сообщений мало: тик смотрит, наступил ли момент
        какого-нибудь слота у владельца, и почти всегда не делает ничего.
        Почему не `0 9 * * *` — см. `TOUCH_SCHEDULE`.

        Чат берётся из whitelist: пользователь один (ADR-0003), и в личной
        переписке `chat_id` совпадает с `user_id`. Пустой whitelist означает,
        что бот никого не слушает, — тогда и говорить некому.
        """
        if not settings.telegram_allowed_user_ids:
            return
        await jobs.touch(
            notifier=notifier,
            chat_id=settings.telegram_allowed_user_ids[0],
            timezone=settings.brain_timezone,
            model=summariser,
        )

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
