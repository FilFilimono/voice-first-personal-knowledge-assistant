"""Общие фикстуры.

Быстрый прогон обязан работать без Postgres и без сети: этот же набор
гоняет Stop-хук после каждой сессии, и он должен отрабатывать за секунды.
Поэтому доступ к базе подменяется на уровне зависимости FastAPI.

Интеграционные тесты работают в **одноразовой базе**, которую фикстура создаёт,
мигрирует и дропает. Рабочую базу они не трогают вовсе — и не могут: `entries`
запрещает DELETE и TRUNCATE (инвариант 3), так что «прибраться за собой»
после теста технически невозможно. Одноразовая база — единственный способ
гонять их повторно.
"""

import asyncio
import os
from collections.abc import AsyncGenerator, Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from brain.api.app import create_app
from brain.api.dependencies import database_is_reachable
from brain.config import get_settings
from brain.storage.db import dispose_engine
from tests import archive as archive_module

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"

# Имя намеренно не пересекается с рабочим `brain`: фикстура эту базу дропает,
# и промахнуться здесь нельзя.
#
# **Суффикс из окружения — чтобы два прогона не дрались за одну базу.**
# До 01.09.2026 имя было одно на всех, и это стоило дня дважды: фикстура
# дропает базу `WITH (FORCE)`, то есть **из-под чужой сессии**, и соседний
# прогон падал `ConnectionDoesNotExistError` и `DuplicateDatabaseError`.
# Ломался при этом не код, а прогон — и ошибка, пришедшая от соседа,
# читается как своя. Один раз это стоило отката двух блоков, второй —
# испорченной охоты за шатким тестом.
#
# По умолчанию суффикса нет: одиночный прогон обязан работать без всякой
# настройки, а `PYTEST_DB_SUFFIX` ставится только там, где прогонов два.
INTEGRATION_DB = "brain_integration" + os.environ.get("PYTEST_DB_SUFFIX", "")


def _app_with_db_state(reachable: bool) -> FastAPI:
    app = create_app()

    async def _stub() -> bool:
        return reachable

    app.dependency_overrides[database_is_reachable] = _stub
    return app


@pytest.fixture
def make_client() -> Callable[[bool], AsyncClient]:
    """Клиент к приложению с заданным состоянием базы."""

    def factory(db_reachable: bool) -> AsyncClient:
        app = _app_with_db_state(db_reachable)
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return factory


@pytest.fixture
async def client(
    make_client: Callable[[bool], AsyncClient],
) -> AsyncGenerator[AsyncClient]:
    """Клиент к приложению с живой базой."""
    async with make_client(True) as ac:
        yield ac


# --- Живая база для интеграционных тестов ---


def database_url(database: str) -> str:
    """URL к соседней базе на том же сервере, что и рабочая."""
    return f"{_server_url()}/{database}"


def _server_url() -> str:
    return get_settings().database_url.rsplit("/", 1)[0]


async def _admin(server_url: str, sql: str) -> None:
    """CREATE/DROP DATABASE: вне транзакции и не из той базы, которую трогаем."""
    engine = create_async_engine(f"{server_url}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(sql))
    finally:
        await engine.dispose()


def _upgrade_to_head() -> None:
    """Накатить миграции. Синхронно: Alembic сам крутит свой event loop.

    Адрес базы берётся из окружения — тем же путём, что и в проде.
    """
    command.upgrade(Config(str(ALEMBIC_INI)), "head")


@pytest.fixture
async def migrated_database(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[str]:
    """Пустая база со схемой проекта. Создаётся и дропается на каждый тест.

    Схема ставится теми же миграциями, что и в проде, — включая схему очереди
    (ADR-0008). Команда `pgq install` не используется здесь ровно так же,
    как не используется нигде.
    """
    server = _server_url()
    url = database_url(INTEGRATION_DB)

    await _admin(server, f'DROP DATABASE IF EXISTS "{INTEGRATION_DB}" WITH (FORCE)')
    # Локаль задаётся явно, а не наследуется от `template1`. Под `C` (а именно
    # такой была рабочая база до 18.08.2026) pg_trgm не видит в кириллице букв
    # вовсе, и третий канал поиска молчит — при полностью зелёных тестах,
    # потому что «PGVector» латиницей находится прекрасно. `template0` нужен
    # потому, что база с другой локалью копируется только с него.
    await _admin(
        server,
        f"CREATE DATABASE \"{INTEGRATION_DB}\" LOCALE 'C.utf8' TEMPLATE template0",
    )

    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    await dispose_engine()
    await asyncio.to_thread(_upgrade_to_head)

    try:
        yield url
    finally:
        await dispose_engine()
        get_settings.cache_clear()
        await _admin(server, f'DROP DATABASE IF EXISTS "{INTEGRATION_DB}" WITH (FORCE)')


@pytest.fixture(scope="session")
def archive() -> list[archive_module.Entry]:
    """Живой архив: тексты берутся из базы, в репозитории их нет.

    Сессионная и синхронная намеренно. Синхронная — потому что читает базу
    через `asyncio.run`, а внутри тестового event loop это невозможно;
    сессионная — потому что читать архив на каждый тест незачем.

    Нет базы и нет кэша — скип с объяснением, а не выдуманные данные.
    """
    entries = archive_module.load()
    if entries is None:
        pytest.skip(archive_module.NO_ARCHIVE)
    return entries


@pytest.fixture(autouse=True)
def _forget_settings_between_tests() -> Iterator[None]:
    """Кэш настроек не должен протекать между тестами."""
    yield
    get_settings.cache_clear()


# База, которой нет. Быстрый тест, случайно дошедший до Postgres, обязан
# упереться в неё, а не в рабочий архив.
NOWHERE = "postgresql+asyncpg://nobody@127.0.0.1:1/there-is-no-database"


@pytest.fixture(autouse=True)
def _no_database_in_fast_tests(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Быстрый тест не ходит в базу. Теперь это не договорённость, а факт.

    Найдено 19 августа 2026 при мутационной проверке: учёт стоимости
    (`core/llm/accounting`) живёт внутри двери наружу и пишет строку на каждый
    вызов модели. Значит, любой быстрый тест этой двери открывает сессию —
    и открывает её к **рабочей** базе, потому что адрес берётся из `.env`.
    Спасло только то, что таблицы `llm_calls` там ещё не было: `record`
    глотает свои ошибки нарочно, чтобы не ронять разметку из-за статистики.
    То есть тест писал бы в живой архив молча.

    Поэтому у быстрых тестов адрес базы подменяется на несуществующий. Это
    та же мысль, что и у одноразовой базы для интеграционных: тест не имеет
    права трогать то, что нельзя восстановить.
    """
    if request.node.get_closest_marker("integration") is None:
        monkeypatch.setenv("DATABASE_URL", NOWHERE)
        get_settings.cache_clear()
    yield
