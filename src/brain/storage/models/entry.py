"""Сырая запись — то, что сказал или написал владелец.

Таблица только на вставку (инвариант 3). Запрет живёт в базе: права отозваны,
плюс триггер, который валит UPDATE и DELETE. Смена состояния записи —
строка в `entry_overrides`, но эта таблица появится в Фазе 3 вместе с первым,
кто в неё пишет.

Расшифровки голоса здесь нет намеренно: она приходит через секунды после приёма,
а править строку нельзя. Расшифровка — производная запись, см. `transcript.py`.
"""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.storage.models.base import Base


class EntrySource(enum.StrEnum):
    """Откуда пришла запись. Фаза 1 умеет только `telegram`."""

    TELEGRAM = "telegram"
    FORWARD = "forward"
    API = "api"


class EntryKind(enum.StrEnum):
    """Чем запись была в момент приёма. Голос остаётся голосом даже после расшифровки."""

    TEXT = "text"
    VOICE = "voice"


class Entry(Base):
    """Одна мысль, как она пришла."""

    __tablename__ = "entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    source: Mapped[EntrySource] = mapped_column(
        Enum(EntrySource, name="entry_source", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    kind: Mapped[EntryKind] = mapped_column(
        Enum(EntryKind, name="entry_kind", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )

    # Для текстовой записи заполнен `raw_text`, для голосовой — `audio_path`
    # и `duration_sec`, а текст лежит в `transcripts`.
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Приватная запись не уходит наружу никогда (инвариант 5). Проверка —
    # в `core/stt` и `core/llm`, а не в вызывающем коде.
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Таймзона владельца в момент приёма: «завтра в 10» без неё не восстановить.
    timezone: Mapped[str] = mapped_column(Text, nullable=False)

    # Защита от двойной доставки Telegram. Для `source=telegram` это точный
    # идентификатор сообщения, а не хэш содержимого: две одинаковые мысли подряд
    # — законный случай, повторная доставка одного сообщения — нет.
    dedup_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    # Служебное от адаптера: chat_id, message_id, message_id подтверждения.
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
