"""Healthcheck.

Сервис один, поэтому отдельный liveness без проверки базы смысла не имеет:
приложение без Postgres не может ничего. При мёртвой базе ручка отдаёт 503 —
это не ошибка, а честный ответ о состоянии.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from brain.api.dependencies import database_is_reachable

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Состояние приложения и его единственной зависимости."""

    status: Literal["ok", "degraded"]
    database: Literal["ok", "unavailable"]


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "База недоступна"}},
)
async def health(
    response: Response,
    db_reachable: Annotated[bool, Depends(database_is_reachable)],
) -> HealthResponse:
    """200, если база отвечает; 503, если нет."""
    if not db_reachable:
        response.status_code = 503
        return HealthResponse(status="degraded", database="unavailable")
    return HealthResponse(status="ok", database="ok")
