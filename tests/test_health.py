"""Что должно работать:

1. /health отвечает 200, когда база жива
2. /health отвечает 503, когда база недоступна — а не 200 с враньём в теле
3. в теле есть состояние базы, а не только общий статус
"""

from collections.abc import Callable

from httpx import AsyncClient


async def test_health_returns_200_when_database_is_up(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_health_returns_503_when_database_is_down(
    make_client: Callable[[bool], AsyncClient],
) -> None:
    async with make_client(False) as ac:
        response = await ac.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "database": "unavailable"}
