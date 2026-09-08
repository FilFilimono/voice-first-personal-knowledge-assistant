"""Событие календаря, созданное из записи. Провенанс того, что ушло наружу.

**Инвариант 4 в чистом виде:** у производной записи есть `source_entry_id`,
без исключений. Событие в Google — производное от записи владельца, и без
этой строки ответить на вопрос «откуда взялось вот это в календаре» можно
было бы только по памяти.

**Вторая причина, и она важнее первой.** Критерий готовности Фазы 5 звучит
как «ни одно событие не создано без моего нажатия» (`tech_plan.md` §8),
и проверяется он не намерением, а данными: строка здесь появляется **только**
на пути, который начинается с нажатия кнопки. Событие в календаре без строки
здесь означало бы, что путь есть какой-то ещё.

**Чего здесь нет намеренно — состояния «отменено».** Прав на удаление
и правку события у системы нет вовсе (ADR-0016, ось 2), поэтому колонка,
которую некому заполнить, была бы обещанием без исполнителя. Владелец удаляет
событие руками в своём календаре; наша строка при этом остаётся и продолжает
честно означать «мы это создали тогда-то».
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from brain.storage.models.base import Base


class EntryCalendarEvent(Base):
    """Одно событие, созданное системой по нажатию владельца."""

    __tablename__ = "entry_calendar_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Инвариант 4. `ondelete` не задан намеренно: записи не удаляются вовсе
    # (инвариант 3), и каскад описывал бы событие, которого не бывает.
    source_entry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("entries.id"), nullable=False
    )

    # Куда именно ушло. Хранится, а не берётся из настройки в момент чтения:
    # ADR-0016 фиксирует ограничение прав как временное, и целевой календарь
    # может смениться — а старые события останутся там, где были созданы.
    calendar_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Идентификатор события у Google. По нему владелец находит событие,
    # а мы — отвечаем «это уже уходило».
    google_event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    # Что именно ушло: текст и время. Дублирует то, что лежит у Google,
    # и это осознанно — на вопрос «что я отправил» система обязана отвечать
    # из своей базы, а не походом в чужую, которая может не ответить.
    title: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
