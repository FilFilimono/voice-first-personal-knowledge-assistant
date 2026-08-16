"""Стык очереди с сессией SQLAlchemy.

Приложение пишет через SQLAlchemy, а pgqueuer умеет говорить только с сырым
соединением asyncpg. Если взять для очереди отдельное соединение, обе вставки
уедут в разные транзакции и инвариант 7 развалится молча — запись останется
без задания, и никто об этом не узнает.

Поэтому очередь всегда строится поверх того же соединения, на котором уже
открыта транзакция сессии. Одна функция, одно место, где это можно сделать
неправильно. Проверяется тестом `tests/test_queue_transaction.py`.
"""

from pgqueuer.db import AsyncpgDriver
from pgqueuer.queries import Queries
from sqlalchemy.ext.asyncio import AsyncSession

# Имена заданий. Собраны здесь, потому что их знают двое — тот, кто ставит
# задание, и воркер: разъехавшиеся строковые литералы ловятся только в проде.
TRANSCRIBE = "transcribe"
REMINDER = "reminder"
AUDIO_RETENTION = "audio-retention"

# Ретеншен аудио: раз в сутки ночью. Точность здесь не нужна вовсе —
# речь о файлах, которым тридцать дней.
AUDIO_RETENTION_SCHEDULE = "0 4 * * *"


async def queue_on(session: AsyncSession) -> Queries:
    """Очередь поверх соединения сессии — не поверх своего собственного."""
    raw = await (await session.connection()).get_raw_connection()
    return Queries(AsyncpgDriver(raw.driver_connection))
