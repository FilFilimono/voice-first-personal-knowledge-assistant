"""Смена состояния записи, которую нельзя изменить.

`entries` не правится — ни UPDATE, ни DELETE, и запрет живёт в базе
(инвариант 3). Всё, что меняется у записи со временем, меняется здесь:
строкой, которая ничего не стирает. Актуальное состояние — последняя строка
по записи.

Такова цена, названная в ADR-0003: «актуальная запись» перестаёт быть
`SELECT * FROM entries`. Чтобы эта цена не расползлась по коду, читать
оверрайды имеет право **только** `core/memory` — там же, где считается
эффективная приватность.

Значений действия ровно два, хотя ADR-0003 набрасывал четыре (`hide`,
`unhide` тоже). Скрытие записей появится вместе с тем, кто его нажимает;
до тех пор это значение, которое никто не пишет, — такой же мёртвый код,
как ветка без вызывающего. Добавить его — одна строка `ALTER TYPE`.
"""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.storage.models.base import Base


class OverrideAction(enum.StrEnum):
    """Что владелец сделал с записью после того, как её разобрали."""

    # «Не туда»: тип поправлен руками. В payload — верный тип и тот, что был.
    RECLASSIFY = "reclassify"
    # «Сделать приватной» задним числом: вектора и разметка удаляются,
    # но текст уже уходил наружу, и бот обязан сказать об этом прямо.
    MAKE_PRIVATE = "make_private"


class EntryOverride(Base):
    """Одна поправка к записи."""

    __tablename__ = "entry_overrides"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_entry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("entries.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    action: Mapped[OverrideAction] = mapped_column(
        Enum(
            OverrideAction,
            name="override_action",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )

    # Содержимое поправки: у `reclassify` — новый и старый тип, у `make_private`
    # — сколько векторов и аннотаций пришлось убрать. Схемы у него нет и запросов
    # по нему тоже: читается оно глазами, когда что-то пошло не так.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        # «Последняя поправка записи» спрашивается на каждом чтении приватности.
        Index("ix_entry_overrides_entry_latest", "source_entry_id", text("id DESC")),
    )
