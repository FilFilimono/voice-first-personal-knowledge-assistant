"""«Замолчи» — пауза на неделю и «сегодня выходной».

Устроено как `private_mode`: журнал нажатий, а не настройка. Текущее состояние
— последняя строка, и тишина действует, пока `until` в будущем. Так сделано
не из любви к append-only, а потому что вопрос «а не была ли эта неделя
на паузе» когда-нибудь возникнет при разборе, почему сводок нет. Строк здесь
несколько десятков в год.

**Что глушится.** Инициативные касания **и утренняя сводка** (`vision.md` §7).
Напоминания проходят всегда: система не имеет права молчать о том, о чём её
просили сами. Проверка живёт в одном месте — `core/digest/quiet.py`, — и это
то же правило, по которому приватность решается только `is_private_now()`.
"""

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, func
from sqlalchemy.orm import Mapped, mapped_column

from brain.storage.models.base import Base


class QuietKind(enum.StrEnum):
    """Чем именно попросили замолчать.

    `RESUME` — это тоже строка, а не удаление предыдущей: снятие паузы такое же
    событие, как её установка, и «почему сводка вдруг снова пошла» должно иметь
    ответ. У неё `until` в прошлом, поэтому проверка «тишина ли сейчас» одна
    на все три вида и без ветвлений по виду.
    """

    PAUSE = "pause"
    DAY_OFF = "day_off"
    RESUME = "resume"


class QuietPeriod(Base):
    """Одно нажатие: `/pause`, `/dayoff` или `/resume`."""

    __tablename__ = "quiet_periods"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    kind: Mapped[QuietKind] = mapped_column(
        Enum(QuietKind, name="quiet_kind", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    # До какого момента молчим. У `resume` — момент нажатия, то есть прошлое.
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
