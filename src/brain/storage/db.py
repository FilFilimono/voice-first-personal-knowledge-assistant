"""Подключение к Postgres.

Движок один на процесс и создаётся лениво: импорт модуля не должен требовать
живой базы, иначе не собрать ни один быстрый тест.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.sql import text

from brain.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Движок процесса. Создаётся при первом обращении."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            # Один пользователь и десятки заданий в день: большой пул не нужен,
            # а на сервере с 4 ГБ каждое соединение — это память Postgres.
            pool_size=5,
            max_overflow=5,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий. `expire_on_commit=False` — чтобы объекты жили после коммита."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession]:
    """Сессия с транзакцией: коммит на выходе, откат на исключении.

    Инвариант 7 (запись и задание в очередь — одной транзакцией) держится
    именно здесь: обе вставки идут внутри одного `session_scope`.
    """
    async with get_session_factory()() as session, session.begin():
        yield session


async def ping() -> bool:
    """Отвечает ли база. Единственное место, где `SELECT 1` уместен."""
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError):
        return False
    return True


async def dispose_engine() -> None:
    """Закрыть пул. Вызывается при остановке приложения."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
