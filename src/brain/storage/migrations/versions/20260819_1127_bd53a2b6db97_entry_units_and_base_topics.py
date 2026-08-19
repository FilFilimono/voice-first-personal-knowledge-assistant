"""entry units and base topics

Три вещи, и все три из замеров 19 августа 2026.

  entry_units              разметка идёт по смысловым единицам (ADR-0012)
  topics.source_entry_id   становится nullable: у базовой темы записи-источника нет
  базовые темы             разработка · cv чек · учеба, засеваются здесь

**Почему `entry_units`, а не разметка по чанкам.** Замер на живом архиве:
нынешняя резка даёт больше одного чанка ровно у одной записи из 37, а десять
записей содержат по два и более типа. Разметка по чанкам совпала бы с разметкой
по записи на 36 записях из 37 — то есть не изменила бы ничего. Медиана
смысловой единицы — 80 символов против ~900 у чанка (ADR-0012).

**Единица хранится отрезком, а не копией текста.** Чанк и единица становятся
сравнимыми: оба — отрезки одного неизменяемого текста. Отсюда же
`source_transcript_id`: у голосовой записи текст живёт в `transcripts`,
перепрогон другой моделью добавит строку, и отрезок обязан помнить, над каким
именно текстом посчитан. У текстовой записи здесь NULL — отрезок считается
по `entries.raw_text`.

**Провенанс — через `annotation_id`, а не своими колонками.** Чем размечено,
уже записано в `annotations`; вторая копия тех же двух полей однажды разъедется
с первой. Заодно это даёт идемпотентность перепрогона: единицы заменяются
целиком вместе с новой строкой разметки, ровно как `entry_topics`.

**`topics.source_entry_id` становится nullable, и это надо назвать вслух.**
Инвариант 4 требует `source_entry_id` у производной записи. Базовая тема
не производная: она не появилась на записи, а задана решением владельца
до того, как записи вообще были. NULL здесь означает именно это и ничего
другого — тема, у которой нет записи-источника, потому что источник не запись.
Проверить, что тема базовая, можно только так, и отдельного флага не заводится:
два способа сказать одно и то же однажды разойдутся.

**Зачем базовые темы.** Замер прод-режима (по одной записи, как работает
воркер): на пустом списке модель выдаёт россыпь одноразовых тем, на растущем —
одну тему-колодец на весь архив. Обобщать она умеет только увидев пачку,
а пачки в проде не бывает. Базовый список выдаёт готовые обобщения вместо
требования вывести их из одной строки (ADR-0011, блок «Обновление»).

Засев идемпотентен: `ON CONFLICT (slug) DO NOTHING`. В рабочей базе тема
`cv чек` уже заведена разметкой — она остаётся своей строкой, со своей
записью-источником.

Revision ID: bd53a2b6db97
Revises: 775ca6ecfe9d
Create Date: 2026-08-19 11:27:11.582214+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "bd53a2b6db97"
down_revision: str | None = "775ca6ecfe9d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Имена тем и их нормализованный вид. Нормализация повторена здесь строкой,
# а не импортом из `brain.storage.models.topic`: применённую миграцию не правят
# задним числом, а функция в коде однажды поменяется — и тогда миграция начнёт
# заводить не то, что заводила. Совпадение проверяет тест.
BASE_TOPICS = (
    ("разработка", "разработка"),
    ("cv чек", "cv чек"),
    ("учеба", "учеба"),
)


def upgrade() -> None:
    annotation_type = postgresql.ENUM(name="annotation_type", create_type=False)
    annotation_state = postgresql.ENUM(name="annotation_state", create_type=False)

    op.create_table(
        "entry_units",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_entry_id", sa.BigInteger(), nullable=False),
        # Над каким текстом посчитан отрезок. NULL — над `entries.raw_text`.
        sa.Column("source_transcript_id", sa.BigInteger(), nullable=True),
        # Какой разбор произвёл эту единицу. Отсюда же провайдер и модель.
        sa.Column("annotation_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Порядок единиц внутри записи. Единицы обязаны покрывать текст подряд,
        # без дыр и наложений; проверяет это код, а не база — модель возвращает
        # границы, и невалидную нарезку надо не отвергнуть, а увидеть в логе.
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("span_start", sa.Integer(), nullable=False),
        sa.Column("span_end", sa.Integer(), nullable=False),
        sa.Column("type", annotation_type, nullable=False),
        # Пусто у согласия прогонов — как в `annotations`, и по той же причине.
        sa.Column("alternative_type", annotation_type, nullable=True),
        # Состояние считается по единице, а не по записи: одна запись может
        # содержать бесспорную задачу и спорную мысль (ADR-0012).
        sa.Column("state", annotation_state, nullable=False),
        sa.CheckConstraint("span_end > span_start", name="ck_entry_units_span"),
        sa.ForeignKeyConstraint(["source_entry_id"], ["entries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_transcript_id"], ["transcripts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["annotation_id"], ["annotations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("annotation_id", "ordinal", name="uq_entry_units_annotation_ordinal"),
    )
    op.create_index(op.f("ix_entry_units_source_entry_id"), "entry_units", ["source_entry_id"])
    # «Единицы текущей разметки записи» — спрашивается на каждом показе разбора.
    op.execute("CREATE INDEX ix_entry_units_entry_latest ON entry_units (source_entry_id, id DESC)")

    # У базовой темы записи-источника нет: она задана, а не выведена.
    op.alter_column("topics", "source_entry_id", existing_type=sa.BigInteger(), nullable=True)

    for name, slug in BASE_TOPICS:
        op.execute(
            sa.text(
                "INSERT INTO topics (source_entry_id, name, slug, reason) "
                "VALUES (NULL, :name, :slug, :reason) ON CONFLICT (slug) DO NOTHING"
            ).bindparams(
                name=name,
                slug=slug,
                reason="базовая тема: заведена миграцией, чтобы списку тем было с чего начаться",
            )
        )


def downgrade() -> None:
    op.drop_table("entry_units")
    op.execute(
        sa.text("DELETE FROM topics WHERE source_entry_id IS NULL AND slug IN :slugs").bindparams(
            sa.bindparam("slugs", value=tuple(slug for _, slug in BASE_TOPICS), expanding=True)
        )
    )
    op.alter_column("topics", "source_entry_id", existing_type=sa.BigInteger(), nullable=False)
