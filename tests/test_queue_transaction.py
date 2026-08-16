"""Инвариант 7: запись и задание в очередь пишутся одной транзакцией.

Проверка нужна не ради pgqueuer — транзакционная постановка задания это его
заявленная главная фича. Проверяется стык: приложение пишет через SQLAlchemy,
а pgqueuer умеет говорить только с сырым соединением asyncpg. Если сессия и
очередь окажутся на разных соединениях, обе вставки уедут в разные транзакции,
инвариант 7 развалится молча — и вернётся подчищающий sweep, от которого
ADR-0005 избавился.

Здесь проверяется сам стык — `queue_on`, — на голой таблице. То же самое
на настоящих `entries` и через настоящий приём записи проверяет
`test_capture_integration.py`: там видно, что стыком действительно
пользуются, а не что он теоретически работает.

Схему очереди ставит Alembic (ADR-0008), а не `Queries.install()`:
`pgq install` не используется в проекте нигде, и тесты не исключение —
иначе они проверяли бы схему, которой в проде нет.

Требует поднятого Postgres: `just up`.
"""

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from brain.core.scheduler.queue import queue_on

ENTRYPOINT = "invariant7_probe"

pytestmark = pytest.mark.integration


async def _counts(engine: AsyncEngine) -> tuple[int, int]:
    """Сколько записей и заданий видно со стороны — отдельным соединением."""
    async with engine.connect() as connection:
        probe = await connection.scalar(text("SELECT count(*) FROM probe"))
        jobs = await connection.scalar(
            text("SELECT count(*) FROM pgqueuer WHERE entrypoint = :entrypoint"),
            {"entrypoint": ENTRYPOINT},
        )
    return int(probe or 0), int(jobs or 0)


@pytest.fixture
async def probe_engine(migrated_database: str) -> AsyncGenerator[AsyncEngine]:
    """Мигрированная база плюс таблица-заглушка вместо `entries`.

    Заглушка, а не `entries`, намеренно: здесь проверяется механика стыка,
    и она не должна зависеть от того, как устроена наша схема.
    """
    engine = create_async_engine(migrated_database)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE probe (id BIGSERIAL PRIMARY KEY)"))
        yield engine
    finally:
        await engine.dispose()


async def _enqueue_on(session: AsyncSession) -> None:
    await (await queue_on(session)).enqueue(ENTRYPOINT, b"1")


async def test_job_is_invisible_until_the_entry_commits(probe_engine: AsyncEngine) -> None:
    """Задание живёт внутри транзакции сессии, а не в своей собственной."""
    factory = async_sessionmaker(probe_engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        await session.execute(text("INSERT INTO probe DEFAULT VALUES"))
        await _enqueue_on(session)

        # Ещё не коммит: снаружи не должно быть видно ни записи, ни задания.
        assert await _counts(probe_engine) == (0, 0)

    assert await _counts(probe_engine) == (1, 1)


async def test_rollback_takes_the_job_with_it(probe_engine: AsyncEngine) -> None:
    """Откат уносит задание: неразмеченных записей без задания не остаётся."""
    factory = async_sessionmaker(probe_engine, expire_on_commit=False)

    with pytest.raises(RuntimeError):
        async with factory() as session, session.begin():
            await session.execute(text("INSERT INTO probe DEFAULT VALUES"))
            await _enqueue_on(session)
            raise RuntimeError("падение между постановкой задания и коммитом")

    assert await _counts(probe_engine) == (0, 0)
