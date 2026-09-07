"""Что упало внутри тика касаний.

**Заведено 7 сентября 2026 вместе с изоляцией тика.** До неё падение одного
механизма роняло весь тик, и это было плохо; но простой `try/except` без этой
таблицы был бы хуже — он поменял бы громкое падение на тихое. Механизм,
который молча не работает, в этом проекте уже стоил недели: `loops.is_stuck`
была написана, покрыта тестом и не вызывалась из `src/` ни разу.

**Почему отдельная таблица, а не строка в журнале касаний.** Журнал касаний
отвечает на вопрос «что случилось со слотом», и слот участвует в бюджете трёх
инициативных касаний. А фиксация долга и напоминание привычки слотами
не являются вовсе — они вне бюджета и вне журнала (`vision.md` §7.1), — и
завести им слот значило бы посчитать их в бюджет. Здесь же лежит **отказ
механизма**, любого: и слота, и не-слота.

**Счётчиков в сообщениях из этого не выходит** (`vision.md` §7.0, п. 5):
таблица читается только `/status`, то есть по спросу. Сама система про свои
отказы владельцу не пишет — сообщение «у меня сломалось» это требование
внимания, а сообщение обязано давать.
"""

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from brain.storage.models.base import Base


class TickFailure(Base):
    """Один отказ механизма внутри тика."""

    __tablename__ = "tick_failures"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Сутки владельца, а не сервера: «что упало сегодня» спрашивается в его
    # часах. Та же колонка и по той же причине, что у `touches`.
    local_date: Mapped[date] = mapped_column(Date, nullable=False)

    # Имя механизма по-русски — строку читает владелец, а не программа.
    mechanism: Mapped[str] = mapped_column(Text, nullable=False)
    # Текст отказа одной строкой. Трассировка остаётся в логе воркера: здесь
    # нужно узнать, что сломалось, а не отладить это с телефона.
    error: Mapped[str] = mapped_column(Text, nullable=False)
