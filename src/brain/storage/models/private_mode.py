"""Переключения приватного режима.

Таблица — журнал, а не настройка: текущий режим это последняя строка.
Так сделано не из любви к append-only, а потому что вопрос «а была ли эта
запись сделана в приватном режиме» когда-нибудь возникнет, и ответ на него
должен существовать. Строк здесь несколько десятков в год.

Сам флаг записи хранится в `entries.is_private`: режим на момент приёма
фиксируется в записи, чтобы последующее переключение её не переиначило.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from brain.storage.models.base import Base


class PrivateModeChange(Base):
    """Одно нажатие `/private on` или `/private off`."""

    __tablename__ = "private_mode"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
