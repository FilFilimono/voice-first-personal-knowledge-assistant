"""Сборка приложения FastAPI."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from brain.api.health import router as health_router
from brain.storage.db import dispose_engine


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    """Пул соединений закрывается при остановке, чтобы reload не копил их."""
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    """Собрать приложение. Отдельная функция — чтобы тесты строили своё."""
    app = FastAPI(
        title="Brain",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(health_router)
    return app


app = create_app()
