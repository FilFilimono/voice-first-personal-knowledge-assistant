"""Отказы механизмов внутри тика: записать и показать.

**Половина изоляции, без которой вторая половина вредна.** Решение владельца
7 сентября 2026 звучит «упавший механизм не роняет остальные» — но `try/except`
сам по себе меняет громкое падение на тихое, а тихая поломка в этом проекте
стоила недели уже дважды. Поэтому отказ не проглатывается: он пишется строкой
и виден в `/status`.

**Запись отказа не имеет права упасть сама.** Она стоит внутри обработчика
исключений, и её собственный отказ отменил бы всю изоляцию — тик снова упал бы
целиком, только теперь на попытке записать, что он упал. Отсюда `try/except`
внутри `record`: то же решение, что у учёта стоимости в `core/llm`, и по той же
причине.

Владельцу система про свои отказы **не пишет**. Строка читается по спросу,
из `/status`: сообщение «у меня сломалось» — это требование внимания,
а сообщение обязано давать (`vision.md` §7.0, принцип 1).
"""

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from brain.config import get_settings
from brain.storage.db import session_scope
from brain.storage.models.tick_failure import TickFailure

logger = logging.getLogger(__name__)

# Сколько текста отказа сохранять. Строку читает владелец с телефона: ему нужно
# понять, что сломалось, а отлаживают это по трассировке в логе воркера.
ERROR_LIMIT = 200


async def record(mechanism: str, error: BaseException, *, day: date) -> None:
    """Записать отказ механизма. Не поднимает исключений никогда.

    `mechanism` — имя по-русски: строку читает владелец, а не программа.
    `day` — сутки владельца, приходят от тика: он один знает таймзону.
    """
    try:
        async with session_scope() as session:
            session.add(
                TickFailure(
                    local_date=day,
                    mechanism=mechanism,
                    error=f"{type(error).__name__}: {error}"[:ERROR_LIMIT],
                )
            )
    except Exception:
        # Отказ записи отказа не имеет права уронить тик — иначе изоляция
        # отменяется ровно в тот момент, когда она нужна.
        logger.exception("отказ механизма %r не записан", mechanism)


async def of_day(now: datetime, *, timezone: str | None = None) -> dict[str, int]:
    """Что упало за сутки владельца и сколько раз. Пусто — всё отработало.

    Таймзона по умолчанию берётся из настроек, а не приходит обязательным
    параметром: единственный вызывающий — `/status`, и там она уже своя,
    а лишний аргумент в подписи означал бы, что её можно передать другую.
    """
    zone = ZoneInfo(timezone or get_settings().brain_timezone)
    day = now.astimezone(zone).date()

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(TickFailure.mechanism, func.count())
                .where(TickFailure.local_date == day)
                .group_by(TickFailure.mechanism)
                .order_by(func.count().desc(), TickFailure.mechanism)
            )
        ).all()
    return {str(row[0]): int(row[1]) for row in rows}
