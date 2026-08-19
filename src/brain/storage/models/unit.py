"""Смысловая единица записи — то, что получает свой тип.

Одно голосовое содержит несколько разных вещей: подумал вслух, решил, надо
не забыть. До ADR-0012 запись получала одну метку на всё, и замер показал цену:
из 37 записей архива десять содержат два и более типа, а шесть теряли тип,
который до владельца не доходил вовсе.

**Единица — не чанк.** Чанк режется для поиска (~900 символов, ADR-0010),
единица — для понимания (медиана 80). Нынешняя резка даёт больше одного чанка
ровно у одной записи из 37, так что разметка по чанкам совпала бы с разметкой
по записи почти везде. Две нарезки не встречаются нигде: у единицы **нет
эмбеддинга никогда** — как только он появится, это будет четвёртый канал
поиска, заведённый по недосмотру.

**Единица — отрезок, а не копия текста.** Поэтому её можно сопоставить с чанком:
оба отрезки одного неизменяемого текста. Отсюда `source_transcript_id` —
у голосовой записи текст живёт в `transcripts`, и отрезок обязан помнить, над
каким именно текстом посчитан; перепрогон расшифровки влечёт переразметку.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from brain.storage.models.annotation import AnnotationState, AnnotationType
from brain.storage.models.base import Base


class EntryUnit(Base):
    """Одно дело внутри записи.

    Именно дело, а не отрезок, на котором тип не меняется: три подряд идущие
    задачи остаются тремя единицами. Фаза 4 работает с делами поштучно —
    «что осталось висеть» и «что закрыл за неделю» требуют знать, что задач
    было три (`vision.md` §4, ADR-0012, блок «Обновление»).
    """

    __tablename__ = "entry_units"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    source_entry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("entries.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Над каким текстом посчитан отрезок. NULL — над `entries.raw_text`.
    source_transcript_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("transcripts.id", ondelete="RESTRICT"), nullable=True
    )
    # Какой разбор произвёл единицу. Провенанс берётся отсюда, своих колонок
    # `provider`/`model` здесь нет: вторая копия однажды разъедется с первой.
    # CASCADE, а не RESTRICT, как везде: единица — часть разметки и без неё
    # не значит ничего. Удаление разметки бывает в двух местах (кнопка
    # «сделать приватной» и перепрогон), и явный `delete` в каждом означал бы,
    # что третье место через полгода про единицы забудет.
    annotation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("annotations.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    span_start: Mapped[int] = mapped_column(Integer, nullable=False)
    span_end: Mapped[int] = mapped_column(Integer, nullable=False)

    type: Mapped[AnnotationType] = mapped_column(
        Enum(
            AnnotationType,
            name="annotation_type",
            values_callable=lambda e: [m.value for m in e],
            create_type=False,
        ),
        nullable=False,
    )
    alternative_type: Mapped[AnnotationType | None] = mapped_column(
        Enum(
            AnnotationType,
            name="annotation_type",
            values_callable=lambda e: [m.value for m in e],
            create_type=False,
        ),
        nullable=True,
    )
    # Состояние по единице, а не по записи: одна запись может содержать
    # бесспорную задачу и спорную мысль.
    state: Mapped[AnnotationState] = mapped_column(
        Enum(
            AnnotationState,
            name="annotation_state",
            values_callable=lambda e: [m.value for m in e],
            create_type=False,
        ),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint("span_end > span_start", name="ck_entry_units_span"),
        UniqueConstraint("annotation_id", "ordinal", name="uq_entry_units_annotation_ordinal"),
        Index("ix_entry_units_entry_latest", "source_entry_id", text("id DESC")),
    )
