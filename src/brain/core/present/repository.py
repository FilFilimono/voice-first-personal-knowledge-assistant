"""Чтение, которое нужно слою представления.

Отдельным файлом — как в `core/enrich`, `core/retrieval` и `core/memory`:
запросы живут в репозитории, а сервис решает, что из ответа следует. Смешать
их значит однажды искать `select` внутри функции, которая называется
«можно ли тратить запрос».
"""

from datetime import datetime

from sqlalchemy import select

from brain.core.llm import CHEAP
from brain.storage.db import session_scope
from brain.storage.models.llm_call import LlmCall


async def refused_since(since: datetime, *, status_code: int) -> bool:
    """Отвечал ли провайдер текстовой модели этим кодом после `since`.

    Тир `cheap`, а не любой: у эмбеддингов своя квота и своя дверь, и их отказ
    к текстовой модели отношения не имеет. Смешать их значило бы замолчать
    чисткой из-за кончившейся индексации.
    """
    async with session_scope() as session:
        found = await session.scalar(
            select(LlmCall.id)
            .where(
                LlmCall.tier == CHEAP,
                LlmCall.status_code == status_code,
                LlmCall.created_at >= since,
            )
            .limit(1)
        )
    return found is not None
