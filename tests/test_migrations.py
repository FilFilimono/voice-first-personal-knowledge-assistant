"""Что должно работать:

1. у истории миграций одна голова — иначе `upgrade head` однажды упадёт
2. каждая миграция обратима хотя бы формально
3. (интеграционно) `upgrade head` на пустой базе включает vector и pg_trgm
"""

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from brain.config import get_settings

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def _script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))


def test_migration_history_has_single_head() -> None:
    assert len(_script_directory().get_heads()) == 1


def test_every_migration_defines_downgrade() -> None:
    for revision in _script_directory().walk_revisions():
        module = revision.module
        assert hasattr(module, "downgrade"), f"{revision.revision}: нет downgrade()"


@pytest.mark.integration
async def test_extensions_are_installed() -> None:
    """Требует поднятого Postgres и накатанных миграций: `just up && just migrate`."""
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT extname FROM pg_extension ORDER BY extname"))
            installed = {row[0] for row in result}
    finally:
        await engine.dispose()

    assert {"vector", "pg_trgm"} <= installed
