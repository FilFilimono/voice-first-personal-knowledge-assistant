"""Что должно работать: падение STT не теряет запись.

Запись к этому моменту уже в базе вместе с аудио — её сохранил обработчик
сообщения одной транзакцией с заданием (инвариант 7). Дальше важно другое:
задание не должно тихо умереть на первой же ошибке провайдера.

Проверяется наша настройка ретраев, а не библиотека: что упавшее задание
просит повтора, что задержка растёт, что она упирается в потолок и что после
исчерпания попыток ошибка становится окончательной, а не крутится вечно.

Счётчик попыток при этом живёт в базе, а не в процессе, — поэтому перезапуск
воркера ретраи не сбрасывает.
"""

from datetime import UTC, datetime, timedelta

import anyio
import pytest
from pgqueuer.errors import RetryRequested
from pgqueuer.executors import DatabaseRetryEntrypointExecutor, EntrypointExecutorParameters
from pgqueuer.models import Context, Job, JobId

from brain.worker import BACKOFF_MULTIPLIER, INITIAL_DELAY, MAX_ATTEMPTS, MAX_DELAY, retry_executor


class ProviderIsDownError(RuntimeError):
    """То, чем оборачивается недоступный STT-провайдер."""


async def _failing_entrypoint(job: Job) -> None:
    raise ProviderIsDownError("провайдер не ответил")


def _job(attempts: int) -> Job:
    """Задание, которое уже падало `attempts` раз.

    Времена берутся от одного момента: их здесь никто не смотрит, важен
    только счётчик попыток — по нему исполнитель считает задержку.
    """
    moment = datetime.now(UTC)
    return Job.model_construct(
        id=JobId(1),
        priority=0,
        created=moment,
        updated=moment,
        heartbeat=moment,
        execute_after=moment,
        status="picked",
        entrypoint="transcribe",
        payload=b"{}",
        queue_manager_id=None,
        headers=None,
        attempts=attempts,
    )


def _executor() -> DatabaseRetryEntrypointExecutor:
    return retry_executor(
        EntrypointExecutorParameters(
            concurrency_limit=0,
            func=_failing_entrypoint,
            accepts_context=False,
            on_failure="delete",
        )
    )


def _context() -> Context:
    """Контекст задания. Отмену никто не дёргает — обработчик падает сам."""
    return Context(cancellation=anyio.CancelScope())


async def _delay_after(attempts: int) -> timedelta:
    """Через сколько будет следующая попытка после `attempts` падений."""
    executor = _executor()
    with pytest.raises(RetryRequested) as raised:
        await executor.execute(_job(attempts), _context())
    return raised.value.delay


async def test_failed_job_asks_to_be_retried() -> None:
    assert await _delay_after(0) == INITIAL_DELAY


async def test_delay_grows_with_every_attempt() -> None:
    delays = [await _delay_after(attempt) for attempt in range(MAX_ATTEMPTS)]

    assert delays == sorted(delays)
    assert delays[1] == INITIAL_DELAY * BACKOFF_MULTIPLIER
    # Растёт, но не бесконечно: ждать сутки нет смысла, аудио никуда не денется.
    assert all(delay <= MAX_DELAY for delay in delays)


async def test_retries_are_not_endless() -> None:
    """Исчерпав попытки, задание падает окончательно — и это видно в `pgq failed`."""
    executor = _executor()

    with pytest.raises(ProviderIsDownError):
        await executor.execute(_job(MAX_ATTEMPTS), _context())


def test_retry_settings_are_the_ones_we_chose() -> None:
    """Настройки не должны разъехаться с тем, что записано в комментарии воркера."""
    executor = _executor()

    assert executor.max_attempts == MAX_ATTEMPTS == 5
    assert executor.initial_delay == INITIAL_DELAY == timedelta(seconds=10)
    assert executor.max_delay == MAX_DELAY == timedelta(minutes=10)
