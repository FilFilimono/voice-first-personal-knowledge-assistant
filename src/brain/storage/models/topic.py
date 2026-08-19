"""Темы архива и привязка тем к записям.

Тема — производная запись (инвариант 4): она появилась на конкретной записи,
и `source_entry_id` говорит, на какой именно. Через полгода это единственный
способ понять, откуда взялась тема, которой пользуются сорок записей.

**Почему у темы два имени.** `name` — то, как её показывают; `slug` — то,
по чему её узнают. Замер 19.08.2026: два прогона одной модели на одной записи
выдали «учеба» и «учёба», то есть две темы там, где тема одна. Нормализация
поэтому не украшение, а условие того, что список тем вообще сходится.

**Почему у темы есть вектор.** Пока тем меньше полутора сотен, модель видит
весь список и вектор не нужен. Когда список перерастёт промпт, кандидатов
будет отбирать близость — и вектор должен быть посчитан заранее, а не в тот
день (ADR-0011).
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from brain.storage.models.base import Base
from brain.storage.models.chunk import DIMENSIONS
from brain.storage.types import Vector


def normalize(name: str) -> str:
    """Имя темы в том виде, по которому темы считаются одинаковыми.

    Нижний регистр, схлопнутые пробелы и «ё» → «е». Последнее выглядит
    придиркой ровно до первого прогона: «учеба» и «учёба» — это одна тема,
    названная двумя строками, и без этой замены список тем удваивается сам
    по себе (ADR-0011).
    """
    return " ".join(name.lower().replace("ё", "е").split())


class Topic(Base):
    """Тема архива."""

    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # На какой записи тема появилась. NULL — базовая тема: она не появилась
    # на записи, а задана решением владельца до того, как записи были вообще.
    # Инвариант 4 говорит про производные записи, а базовая тема не производная;
    # отдельного флага «базовая» нет намеренно — два способа сказать одно и то же
    # однажды разойдутся (ADR-0011, блок «Обновление»).
    source_entry_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("entries.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    # Почему модель не смогла обойтись существующими темами. Обязательное поле
    # ответа: новая тема заводится только с объяснением (ADR-0011). Заодно
    # по нему видно обратную беду — когда модель хотела новую тему и не рискнула.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Вектор названия — для отбора кандидатов, когда список перерастёт промпт.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(DIMENSIONS), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)


class EntryTopic(Base):
    """Какие темы у записи сейчас.

    Проекция последней разметки, а не журнал: перепрогон заменяет строки
    записи целиком — ровно так же, как пересчёт заменяет чанки. История
    при этом не теряется, она лежит в `annotations`, а `annotation_id` здесь
    говорит, какой именно разбор приписал эту тему.
    """

    __tablename__ = "entry_topics"

    source_entry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("entries.id", ondelete="RESTRICT"), primary_key=True
    )
    topic_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("topics.id", ondelete="RESTRICT"), primary_key=True, index=True
    )
    annotation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("annotations.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
