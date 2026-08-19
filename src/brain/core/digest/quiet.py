"""«Замолчи»: пауза на неделю и «сегодня выходной».

Единственное место, где решается вопрос «молчим ли мы сейчас», — по тому же
правилу, по которому приватность решается только `is_private_now()`. Вопрос,
у которого два ответчика, однажды получит два разных ответа.

**Что глушится и что нет.** Инициативные касания и утренняя сводка — да.
Напоминания — никогда: система не имеет права молчать о том, о чём её просили
сами (`vision.md` §7). Технически это обеспечено тем, что путь напоминания
(`core/scheduler`) в этот модуль не заглядывает вовсе; проверкой «а не пауза ли»
там ничего не обвешано, и обвешивать нельзя.

**Состояние — последняя строка журнала**, как у приватного режима. Не «есть ли
активная строка»: пауза, поставленная после снятия, обязана снова включить
тишину, а не проиграть более ранней и более длинной строке.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select

from brain.storage.db import session_scope
from brain.storage.models.quiet import QuietKind, QuietPeriod

# «Замолчи на неделю» из `vision.md` §7. Ровно неделя, а не «пока не передумаю»:
# тишина без срока однажды окажется забытой, и владелец решит, что бот умер.
PAUSE = timedelta(days=7)


async def is_quiet(now: datetime) -> bool:
    """Молчим ли мы сейчас."""
    ends = await until(now)
    return ends is not None


async def until(now: datetime) -> datetime | None:
    """До какого момента молчим. `None` — не молчим.

    Читается последняя строка, а не «максимальный `until`»: журнал описывает
    решения владельца во времени, и последнее из них главное.
    """
    async with session_scope() as session:
        latest = await session.scalar(
            select(QuietPeriod.until).order_by(QuietPeriod.id.desc()).limit(1)
        )
    if latest is None or latest <= now:
        return None
    return latest


async def pause(now: datetime) -> datetime:
    """«Замолчи на неделю». Возвращает момент, когда тишина кончится."""
    return await _write(QuietKind.PAUSE, now + PAUSE)


async def day_off(now: datetime, timezone: str) -> datetime:
    """«Сегодня выходной» — до полуночи **у владельца**, а не в UTC.

    Разница не теоретическая: в 22:00 по Риму в UTC ещё 20:00 того же дня,
    и выходной, посчитанный по UTC, прихватил бы два часа следующего.
    """
    local = now.astimezone(ZoneInfo(timezone))
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return await _write(QuietKind.DAY_OFF, midnight.astimezone(UTC))


async def resume(now: datetime) -> None:
    """Снять тишину.

    Тоже строка журнала, а не удаление предыдущей: «почему сводка вдруг снова
    пошла» должно иметь ответ. `until` в прошлом — поэтому проверка «молчим ли»
    одна на все три вида и без ветвлений по виду.
    """
    await _write(QuietKind.RESUME, now)


async def _write(kind: QuietKind, ends: datetime) -> datetime:
    async with session_scope() as session:
        await session.execute(insert(QuietPeriod).values(kind=kind, until=ends))
    return ends
