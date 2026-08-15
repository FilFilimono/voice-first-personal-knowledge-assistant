"""Инвариант 7: запись и задание в очередь пишутся одной транзакцией.

Проверка нужна не ради pgqueuer — транзакционная постановка задания это его
заявленная главная фича. Проверяется стык: приложение пишет через SQLAlchemy,
а pgqueuer умеет говорить только с сырым соединением asyncpg. Если сессия и
очередь окажутся на разных соединениях, обе вставки уедут в разные транзакции,
инвариант 7 развалится молча — и вернётся подчищающий sweep, от которого
ADR-0005 избавился.

Отсюда три проверки: до коммита задания не видно со стороны (значит, оно
действительно внутри транзакции сессии), после коммита есть и запись и задание,
после отката нет ни того ни другого.

Требует поднятого Postgres: `just up`. Работает в отдельной одноразовой базе,
рабочую не трогает и схему проекта не создаёт.
"""

from collections.abc import AsyncGenerator

import pytest
from pgqueuer.db import AsyncpgDriver
from pgqueuer.queries import Queries
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from brain.config import get_settings

# Одноразовая база под проверку. Имя намеренно не пересекается с рабочим `brain`:
# фикстура её дропает, и промахнуться здесь нельзя.
PROBE_DB = "brain_invariant7_probe"
ENTRYPOINT = "invariant7_probe"

pytestmark = pytest.mark.integration


def _url(database: str) -> str:
    """URL к соседней базе на том же сервере, что и рабочая."""
    return f"{get_settings().database_url.rsplit('/', 1)[0]}/{database}"


async def _admin(sql: str) -> None:
    """CREATE/DROP DATABASE: вне транзакции и не из той базы, которую трогаем."""
    engine = create_async_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(sql))
    finally:
        await engine.dispose()


async def _queue_on(session: AsyncSession) -> Queries:
    """Очередь поверх того же соединения, на котором работает сессия.

    Здесь и держится инвариант 7: `driver_connection` — это ровно то соединение
    asyncpg, которым SQLAlchemy выполняет INSERT и на котором уже открыта
    транзакция. Отдельного соединения для очереди не берётся намеренно.
    """
    raw = await (await session.connection()).get_raw_connection()
    return Queries(AsyncpgDriver(raw.driver_connection))


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
async def probe_engine() -> AsyncGenerator[AsyncEngine]:
    """Пустая база со схемой pgqueuer и таблицей-заглушкой вместо `entries`."""
    await _admin(f'DROP DATABASE IF EXISTS "{PROBE_DB}"')
    await _admin(f'CREATE DATABASE "{PROBE_DB}"')

    engine = create_async_engine(_url(PROBE_DB))
    try:
        async with engine.begin() as connection:
            raw = await connection.get_raw_connection()
            await Queries(AsyncpgDriver(raw.driver_connection)).install()
            await connection.execute(text("CREATE TABLE probe (id BIGSERIAL PRIMARY KEY)"))
        yield engine
    finally:
        await engine.dispose()
        await _admin(f'DROP DATABASE IF EXISTS "{PROBE_DB}"')


async def test_job_is_invisible_until_the_entry_commits(probe_engine: AsyncEngine) -> None:
    """Задание живёт внутри транзакции сессии, а не в своей собственной."""
    factory = async_sessionmaker(probe_engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        await session.execute(text("INSERT INTO probe DEFAULT VALUES"))
        await (await _queue_on(session)).enqueue(ENTRYPOINT, b"1")

        # Ещё не коммит: снаружи не должно быть видно ни записи, ни задания.
        assert await _counts(probe_engine) == (0, 0)

    assert await _counts(probe_engine) == (1, 1)


async def test_rollback_takes_the_job_with_it(probe_engine: AsyncEngine) -> None:
    """Откат уносит задание: неразмеченных записей без задания не остаётся."""
    factory = async_sessionmaker(probe_engine, expire_on_commit=False)

    with pytest.raises(RuntimeError):
        async with factory() as session, session.begin():
            await session.execute(text("INSERT INTO probe DEFAULT VALUES"))
            await (await _queue_on(session)).enqueue(ENTRYPOINT, b"1")
            raise RuntimeError("падение между постановкой задания и коммитом")

    assert await _counts(probe_engine) == (0, 0)
