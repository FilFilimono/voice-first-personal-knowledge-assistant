"""Расшифровка голосовой записи.

Производная запись, поэтому хранит `source_entry_id` (инвариант 4) и то, чем
расшифровано: через полгода надо уметь отличить текст, сказанный владельцем,
от текста, выданного моделью, и понять какой именно.

Почему отдельная таблица, а не колонка в `entries`: запись сохраняется сразу,
чтобы её нельзя было потерять при падении STT, а текст появляется через
секунды — и править `entries` нельзя (инвариант 3). Перепрогон другой моделью
добавляет строку, а не переписывает старую; текущей считается последняя.
Ровно ради этого ADR-0007 держит аудио тридцать дней.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from brain.storage.models.base import Base


class Transcript(Base):
    """Что услышала модель в голосовой записи."""

    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_entry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("entries.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    text: Mapped[str] = mapped_column(Text, nullable=False)

    # Вычищенная версия того же текста: без «э», самоповторов и оборванных
    # начал, с пунктуацией (ADR-0015, ось 2). Лежит **рядом** с дословной,
    # а не вместо неё, и это не осторожность, а условие обратимости: чистка
    # делается моделью, а всё, что модель потеряет, обязано остаться
    # находимым. Поэтому показ идёт по вычищенной (`display_text()`),
    # а поиск, разметка и отрезки — по дословной (`working_text()`).
    #
    # NULL — законное состояние, а не ошибка: чистка приходит отдельным
    # вызовом через секунды после расшифровки и может не прийти вовсе.
    # Тогда показывается дословный вариант.
    clean_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Чем вычищено — отдельно от `model`: расшифровка и чистка это два разных
    # вызова и, по замыслу, две разные модели. Одна колонка на двоих врала бы
    # ровно в тот момент, когда сравниваешь качество чистки между моделями.
    clean_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Провенанс расшифровки: чем и какой моделью. Без этого нельзя сравнить
    # старые записи с новыми после смены провайдера.
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)

    # Наблюдаемость: расшифровка — единственная статья расходов, растущая
    # с использованием, и первая, где заметна деградация провайдера.
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
