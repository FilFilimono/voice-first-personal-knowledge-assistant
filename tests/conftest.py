"""Общие фикстуры.

Быстрый прогон обязан работать без Postgres и без сети: этот же набор
гоняет Stop-хук после каждой сессии, и он должен отрабатывать за секунды.
Поэтому доступ к базе подменяется на уровне зависимости FastAPI.
"""

from collections.abc import AsyncGenerator, Callable

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from brain.api.app import create_app
from brain.api.dependencies import database_is_reachable


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
