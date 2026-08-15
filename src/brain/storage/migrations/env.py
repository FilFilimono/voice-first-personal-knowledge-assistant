"""Окружение Alembic.

Драйвер асинхронный (asyncpg), поэтому онлайн-режим гоняется через
`asyncio.run` и `run_sync`. Синхронного подключения к базе в проекте нет
нигде — второй драйвер ради миграций заводить не за чем.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from brain.config import get_settings
from brain.storage.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# То, с чем autogenerate сравнивает состояние базы.
target_metadata = Base.metadata


def _database_url() -> str:
    """URL из окружения. В `alembic.ini` его нет намеренно."""
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Сгенерировать SQL, не подключаясь к базе (`alembic upgrade head --sql`)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: object) -> None:
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        # Смену типа колонки Alembic сам не замечает — включаем сравнение явно.
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Накатить миграции на живую базу."""
    engine = create_async_engine(_database_url(), pool_pre_ping=True)

    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
