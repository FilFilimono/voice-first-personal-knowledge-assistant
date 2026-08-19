"""Дело — то, что владелец пообещал себе и что можно закрыть.

Появляется из единицы разметки типа «задача» (ADR-0012) и живёт дальше своей
жизнью. Утренняя сводка перечисляет незакрытые, недельное ревью считает
закрытые за неделю.

**Почему это своя таблица, а не колонка у `entry_units`.** Перепрогон разметки
добавляет новую строку `annotations` и новые единицы, старые остаются лежать.
Состояние, привязанное к `entry_units.id`, обнулялось бы при каждом
`just reenrich` — закрытые дела воскресали бы молча, и заметить это можно было
бы только по тому, что сводка вдруг стала длиннее.

**Почему `state` изменяемый, а не журнал строк.** Append-only в проекте
существует ради инварианта 3: `entries` не правится, поэтому её состояние живёт
в оверрайдах. Дело — не запись, а рабочая сущность того же рода, что `loops`
и `reminders`; `tech_plan.md` §3 описывает их изменяемыми с самого начала.
Единственное, что от истории нужно недельному ревью, — «что закрыл за неделю»,
и на это хватает `state_changed_at`.

**Идемпотентность по отрезку.** Ключ — `(source_entry_id, span_start, span_end)`:
перепрогон, давший ту же нарезку, дубля не заведёт; давший другую — заведёт
новое дело и оставит старое. Второе честнее, чем попытка угадать, какое старое
дело соответствует новому отрезку.

**Текст хранится снимком, и это осознанное третье место, где лежат слова.**
Отрезок без своего текста не значит ничего (та же логика, что у `entry_units`),
но сводка обязана уметь показать дело даже после перепрогона расшифровки,
сдвинувшего границы. Снимок — двадцать символов на дело, а не копия архива.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from brain.storage.models.base import Base


class CommitmentState(enum.StrEnum):
    """Что с делом.

    Три значения, а не два: «не актуально» и «сделал» — разные ответы,
    и недельное ревью, которое считает закрытое, обязано их различать.
    Слить их значило бы записать в достижения то, от чего отказались.
    """

    OPEN = "open"
    DONE = "done"
    DROPPED = "dropped"


class Commitment(Base):
    """Одно дело."""

    __tablename__ = "commitments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_entry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("entries.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Единица, из которой дело выведено. SET NULL, а не CASCADE: единицы уходят
    # вместе с разметкой (кнопка «сделать приватной», перепрогон), а дело
    # переживает разметку по построению — в этом весь смысл отдельной таблицы.
    source_unit_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("entry_units.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    text: Mapped[str] = mapped_column(Text, nullable=False)
    span_start: Mapped[int] = mapped_column(Integer, nullable=False)
    span_end: Mapped[int] = mapped_column(Integer, nullable=False)

    state: Mapped[CommitmentState] = mapped_column(
        Enum(
            CommitmentState,
            name="commitment_state",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=CommitmentState.OPEN,
    )
    # Когда закрыли. Пусто у открытого. Нужно недельному ревью: «что закрыл
    # за неделю» — это вопрос про время, а не про состояние.
    state_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("span_end > span_start", name="ck_commitments_span"),
        UniqueConstraint(
            "source_entry_id", "span_start", "span_end", name="uq_commitments_entry_span"
        ),
        # «Что висит» — запрос утренней сводки, и он идёт по состоянию.
        Index("ix_commitments_open", "state", "created_at"),
    )
