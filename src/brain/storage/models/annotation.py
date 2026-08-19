"""Разметка записи: чем она является и насколько устойчиво это определено.

Производная запись, поэтому хранит `source_entry_id` (инвариант 4) и то, чем
размечена: через полгода надо уметь отличить разбор старой модели от нового.

**Перепрогон добавляет строку, а не правит старую** — как у расшифровки.
Текущей разметкой считается последняя. Это не формальность: разметка второй
моделью нужна затем, чтобы сравнить её с первой, а не затем, чтобы стереть.

**Почему колонки, а не `payload jsonb`, как в наброске ADR-0003.** Тип
и состояние читаются в запросах постоянно — `/status` считает неразобранное,
eval считает матрицу ошибок, вечерний пакет выбирает по состоянию. Внутри
jsonb каждое такое чтение стало бы выражением без индекса. В jsonb осталось
то, у чего нет ни схемы, ни запросов, — сущности.
"""

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.storage.models.base import Base


class AnnotationType(enum.StrEnum):
    """Чем запись является. Девять значений, обоснование — ADR-0011.

    Значения английские, потому что это значения колонки; определения, которые
    видит модель, — русские и живут в `core/enrich/prompt.py`. Таблицы
    соответствий между ними нет намеренно: модель возвращает ровно то, что
    ложится в колонку, а таблица перевода однажды разъедется с одной из сторон.
    """

    THOUGHT = "thought"
    TASK = "task"
    # Событие со временем: то, что произойдёт независимо от того, сделает ли
    # владелец что-нибудь. Без этого типа «во вторник в 9:00 экзамен» обе
    # модели называли мыслью (ADR-0011).
    EVENT = "event"
    # Открытый вопрос — «открытая петля» из vision: думал и не додумал.
    LOOP = "loop"
    DECISION = "decision"
    ERROR = "error"
    INSIGHT = "insight"
    REFUSAL = "refusal"
    # Служебное: проверка бота, обрывок, ответ на вопрос бота. Смысла записи
    # нет вовсе, и без этого типа шум становился «мыслью» и утекал в темы.
    NOISE = "noise"


class AnnotationState(enum.StrEnum):
    """Насколько устойчиво определён тип.

    Состояние, а не число: числовая самооценка у моделей плохо калибрована
    (ADR-0003). Но и enum'у самооценки верить нельзя — замер 19.08.2026 дал
    «уверен» на 29 записях из 29. Поэтому состояние здесь считается
    по расхождению двух прогонов, а не приходит от модели (ADR-0011).
    """

    SURE = "sure"
    TWO_HYPOTHESES = "two_hypotheses"
    UNCLEAR = "unclear"


class Annotation(Base):
    """Что дешёвая модель поняла про запись."""

    __tablename__ = "annotations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_entry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("entries.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    type: Mapped[AnnotationType] = mapped_column(
        Enum(
            AnnotationType,
            name="annotation_type",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )

    # Вторая версия — только когда прогоны разошлись. У согласия альтернативы
    # нет по построению, и пустое значение здесь означает именно это.
    alternative_type: Mapped[AnnotationType | None] = mapped_column(
        Enum(
            AnnotationType,
            name="annotation_type",
            values_callable=lambda e: [m.value for m in e],
            create_type=False,
        ),
        nullable=True,
    )

    state: Mapped[AnnotationState] = mapped_column(
        Enum(
            AnnotationState,
            name="annotation_state",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )

    # Имена собственные, продукты, люди — как они названы в тексте. Здесь jsonb
    # законен: схемы у списка строк нет, и запросов по нему тоже нет.
    entities: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    # Провенанс: чем размечено. Без этого нельзя сравнить разбор двух моделей,
    # а ради этого сравнения перепрогон и существует.
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        # «Текущая разметка записи» — это последняя строка по записи, и такой
        # запрос делают все: бот, `/status`, eval. Без индекса он читал бы
        # таблицу целиком уже на первой тысяче записей.
        Index("ix_annotations_entry_latest", "source_entry_id", text("id DESC")),
    )
