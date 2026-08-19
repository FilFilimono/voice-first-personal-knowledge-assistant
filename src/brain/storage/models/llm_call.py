"""Журнал обращений к модели: что, зачем, почём.

ADR-0004 требует учёт **с первого дня**, а не «потом добавлю», и причина
записана там же: расход растёт незаметно. Незаметно — потому что каждый
отдельный вызов стоит доли цента, и заметить рост можно только в сумме,
которой без этой таблицы нигде нет.

**`source_entry_id` здесь нет, и это не нарушение инварианта 4.** Инвариант
говорит про производные записи — про то, что выведено *из* записи. Вызов
покрывает пачку записей сразу (ADR-0011), и привязать его к одной значило бы
соврать в трёх случаях из четырёх. Сколько записей ушло в вызов, говорит
`entries_count`.

Строка пишется и на упавший вызов тоже: провайдер берёт деньги за попытку,
а ретраи очереди умеют превратить одну разметку в пять.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, Numeric, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from brain.storage.models.base import Base


class LlmCall(Base):
    """Один вызов модели."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    # Тир из ADR-0004: `cheap` или `expensive`. Строкой, а не enum'ом:
    # маршрутизация живёт в коде, а здесь это метка для отчёта.
    tier: Mapped[str] = mapped_column(Text, nullable=False)
    # Назначение вызова: `enrich`, позже `digest`, `contradiction`. Ради него
    # таблица и заведена — «сколько стоит разметка» без этой колонки не спросить.
    purpose: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    entries_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Пусто, если цена модели неизвестна. Пустая цена честнее нуля: ноль
    # означал бы «бесплатно», а мы всего лишь не знаем прайс новой модели.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 8), nullable=True)

    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
