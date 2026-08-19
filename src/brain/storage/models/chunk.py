"""Кусок записи, по которому идёт поиск.

Производная запись, поэтому хранит `source_entry_id` (инвариант 4).

**Почему чанки, в отличие от `entries` и `transcripts`, можно переписывать.**
Инвариант 3 назван для `entries`, и только для неё: там лежит сказанное
владельцем, и второго такого нет. Расшифровка неизменяема по другой причине —
в ней есть собственное знание, «что именно услышала модель», и перепрогон
добавляет строку, а не правит старую.

У чанка своего знания нет вовсе: это детерминированная функция от текста
и правила резки. Перегенерация — обычное дело (сменилась модель эмбеддингов,
поправилось правило), и хранить историю чанков значит копить мусор, который
невозможно осмысленно прочитать. Поэтому пересчёт удаляет чанки записи
и вставляет новые одной транзакцией.

Три колонки под три канала поиска: `tsv` считает база, `text` ищется
триграммами как есть, `embedding` пустой у приватных записей (инвариант 5).
"""

from datetime import datetime

from sqlalchemy import BigInteger, Computed, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from brain.storage.models.base import Base
from brain.storage.types import Vector

# Размерность вектора. Живёт в схеме, а не в настройке: поменять её значит
# перегенерировать архив, мигрировать колонку и пересобрать индекс (ADR-0006).
# Настройку можно поправить в `.env` и узнать об этом от базы уже в проде.
DIMENSIONS = 768

# Конфигурация полнотекста. Русский snowball берёт словоформы («напомни» →
# «напомн»), английские термины внутри русской речи оставляет как есть —
# ровно поэтому третьим каналом стоят триграммы (ADR-0006).
FTS_CONFIG = "russian"


class Chunk(Base):
    """Кусок текста записи со всем, что нужно трём каналам поиска."""

    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_entry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("entries.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Порядковый номер внутри записи. Нужен, чтобы показать найденное в том
    # порядке, в каком оно было сказано, и чтобы пересчёт был идемпотентным.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    text: Mapped[str] = mapped_column(Text, nullable=False)

    # Считает база, а не приложение: колонка, которую заполняет код, однажды
    # разъедется с текстом — и молча, потому что поиск продолжит что-то находить.
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(f"to_tsvector('{FTS_CONFIG}', text)", persisted=True),
        nullable=False,
    )

    # NULL у приватных записей (инвариант 5) и у всего, что ещё не посчитано.
    # Различать эти два случая по колонке нельзя и не нужно: приватность
    # видна в `entries.is_private`, а несчитанное находит `just reindex`.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(DIMENSIONS), nullable=True)

    # Чем посчитан вектор. Без этого нельзя найти чанки, оставшиеся от прошлой
    # модели, — а именно они и есть цена любой смены модели эмбеддингов.
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
