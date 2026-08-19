"""Учёт вызовов модели.

ADR-0004 требует его с первого дня, и требование не про аккуратность:
каждый отдельный вызов стоит доли цента, поэтому рост расхода заметен только
в сумме — а суммы без этой таблицы нет нигде.

Запись сюда идёт **и на упавший вызов тоже**: провайдер берёт деньги
за попытку, а ретраи очереди умеют превратить одну разметку в пять.

Ошибка учёта не должна ронять работу. Разметка, не сохранённая из-за того,
что не записалась строчка статистики, — это потеря данных ради наблюдаемости,
то есть ровно наоборот.
"""

import logging
from decimal import Decimal

from sqlalchemy import insert

from brain.core.llm.pricing import cost_usd
from brain.storage.db import session_scope
from brain.storage.models.llm_call import LlmCall

logger = logging.getLogger(__name__)


async def record(
    *,
    provider: str,
    model: str,
    tier: str,
    purpose: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    entries_count: int = 0,
    latency_ms: int = 0,
    succeeded: bool = True,
    error: str | None = None,
) -> Decimal | None:
    """Записать вызов. Вернуть его стоимость, если она известна."""
    cost = cost_usd(model, input_tokens=input_tokens, output_tokens=output_tokens)
    if cost is None:
        # Не крах, но и не мелочь: незнакомая модель означает, что расход
        # перестал считаться, а заметить это можно только по логу.
        logger.warning("цена модели %s неизвестна — вызов учтён без стоимости", model)

    try:
        async with session_scope() as session:
            await session.execute(
                insert(LlmCall).values(
                    provider=provider,
                    model=model,
                    tier=tier,
                    purpose=purpose,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    entries_count=entries_count,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                    succeeded=succeeded,
                    error=error,
                )
            )
    except Exception:
        logger.exception("вызов модели не удалось учесть: %s/%s, %s", provider, model, purpose)

    return cost
