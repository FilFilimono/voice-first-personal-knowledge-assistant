"""Открытая петля — «думал об этом и не додумал».

`vision.md` §5.2 называет петли ядром системы: именно они превращают свалку
в мышление. Механика оттуда же и целиком в колонках этой таблицы:

- заводится **только с подтверждением владельца**, а не по одной догадке
  модели: тип `loop` у единицы — это предложение, а не факт;
- показывается старейшая среди не спрошенных за 14 дней;
- «ещё думаю» сдвигает на 14 дней вперёд (`snooze_until`);
- между вопросами по одной петле минимум 3 дня;
- после третьего «ещё думаю» вопрос меняется на «закрыть или переформулировать?»
  — петля перестаёт быть раздражителем. Отсюда `times_asked`: это не счётчик
  ради счётчика, а то, из чего выбирается формулировка.

**Таблица изменяемая, и это не оговорка.** `tech_plan.md` §3 относит `loops`
к рабочим сущностям вместе с `reminders`, где `fired_at` тоже правится. Правило
append-only в проекте защищает `entries` (инвариант 3), а не всё подряд.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from brain.storage.models.base import Base


class LoopStatus(enum.StrEnum):
    """Что с петлёй. Три кнопки из `vision.md` §5.2."""

    OPEN = "open"
    CLOSED = "closed"
    IRRELEVANT = "irrelevant"


class Loop(Base):
    """Один незакрытый вопрос."""

    __tablename__ = "loops"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_entry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("entries.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Единица, которую модель предложила как петлю. SET NULL по той же причине,
    # что у дела: петля переживает перепрогон разметки, а единица — нет.
    source_unit_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("entry_units.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Текст петли снимком — как у дела, и по той же причине.
    question: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[LoopStatus] = mapped_column(
        Enum(LoopStatus, name="loop_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=LoopStatus.OPEN,
    )

    last_asked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Сколько раз спрашивали. После третьего «ещё думаю» вопрос меняется.
    times_asked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Куда сдвинули после «ещё думаю». Пусто — спрашивать можно, как только
    # выйдет минимальный промежуток.
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # «Какую петлю показать» — запрос утренней сводки: открытые, по возрасту.
        Index("ix_loops_open_oldest", "status", "created_at"),
    )
