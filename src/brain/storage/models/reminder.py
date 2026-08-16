"""Напоминание, вытащенное из записи.

Фаза 1 знает один тип триггера — время. События и темы (`event`, `topic`
из ADR-0003) появятся с календарём в Фазе 5.

Момент срабатывания живёт в двух местах: `fire_at` здесь и `execute_after`
у задания в очереди. Это не дублирование: очередь знает, *когда* разбудить
воркер, а таблица — *что* именно и было ли уже отправлено. Обе строки пишутся
одной транзакцией с записью (инвариант 7).
"""

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from brain.storage.models.base import Base


class ReminderTrigger(enum.StrEnum):
    """Что поднимает напоминание. Фаза 1 умеет только время."""

    TIME = "time"
    EVENT = "event"
    TOPIC = "topic"


class Reminder(Base):
    """«Напомни завтра в 10» — разобранное в конкретный момент времени."""

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_entry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("entries.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    trigger: Mapped[ReminderTrigger] = mapped_column(
        Enum(
            ReminderTrigger,
            name="reminder_trigger",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=ReminderTrigger.TIME,
    )

    # Всегда в UTC: таймзона владельца известна из записи, а хранить местное
    # время — способ поймать переход на зимнее время самым неприятным способом.
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    # Текст напоминания без служебного «напомни»: то, что владелец увидит.
    text: Mapped[str] = mapped_column(Text, nullable=False)

    # Пусто, пока не отправлено. Заодно защищает от повторной отправки,
    # если задание выполнится дважды.
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
