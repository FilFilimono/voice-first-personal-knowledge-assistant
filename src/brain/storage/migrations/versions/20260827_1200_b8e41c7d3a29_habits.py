"""habits and the habit annotation type

Revision ID: b8e41c7d3a29
Revises: a71c9d0b4e26
Create Date: 2026-08-27 12:00:00.000000+00:00

Привычка — отдельный тип записи и отдельная таблица (`vision.md` §5.11,
блок B плана Фазы 4). Причина в устройстве, а не в оттенке: у привычки нет
состояния «сделано», и делом она либо висит вечным упрёком, либо исчезает
после первого выполнения.

**Миграция данных не нужна:** существующие записи остаются при своих типах,
привычек до этой миграции не было ни одной.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b8e41c7d3a29"
down_revision: str | None = "a71c9d0b4e26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RHYTHM = "habit_rhythm"
STATUS = "habit_status"


def upgrade() -> None:
    # `ALTER TYPE ... ADD VALUE` необратим: `DROP VALUE` в PostgreSQL нет вовсе.
    # Цена та же, что у состояний дел (`a71c9d0b4e26`) и у ADR-0013/ADR-0015:
    # значение остаётся в типе навсегда, и откат вернёт схему не в точности
    # прежней. Названо здесь, а не умолчано, потому что решение принимает
    # владелец, а не миграция.
    #
    # `AFTER 'task'` — не косметика. Значение, добавленное в конец, встало бы
    # в базе девятым, а в коде оно стоит третьим (`AnnotationType`), и порядок
    # разъехался бы молча: сверяет их только `test_annotation_schema`, а живой
    # разметке порядок безразличен. Место выбрано смыслом — привычка отличается
    # от задачи повтором, и читаются они рядом.
    op.execute("ALTER TYPE annotation_type ADD VALUE IF NOT EXISTS 'habit' AFTER 'task'")

    rhythm = postgresql.ENUM(
        "daily",
        "every_other_day",
        "weekdays",
        "weekends",
        "weekly_days",
        "times_per_week",
        name=RHYTHM,
        create_type=False,
    )
    status = postgresql.ENUM("active", "stopped", name=STATUS, create_type=False)
    rhythm.create(op.get_bind(), checkfirst=True)
    status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "habits",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("source_unit_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("rhythm", rhythm, nullable=False),
        # Дни недели, 0 — понедельник. Пусто у «каждый день» и «через день»:
        # там ритм не про дни недели вовсе.
        sa.Column("days", postgresql.ARRAY(sa.SmallInteger()), nullable=True),
        sa.Column("times_per_week", sa.SmallInteger(), nullable=True),
        sa.Column("hour", sa.SmallInteger(), nullable=False),
        sa.Column("minute", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("status", status, nullable=False, server_default="active"),
        # Не счётчик пропусков и не серия — их в этой таблице нет намеренно
        # (`vision.md` §5.11). Отвечает на один вопрос: «сегодня уже напоминали?»
        sa.Column("last_reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["source_entry_id"], ["entries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_unit_id"], ["entry_units.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        # Одна привычка на запись: кнопка подтверждения живёт в переписке вечно,
        # и второе нажатие не должно завести второе ежедневное сообщение.
        sa.UniqueConstraint("source_entry_id", name="uq_habits_entry"),
    )
    op.create_index("ix_habits_source_entry_id", "habits", ["source_entry_id"])
    op.create_index("ix_habits_active", "habits", ["status", "id"])


def downgrade() -> None:
    """Честно: прежнего вида `annotation_type` этот откат не вернёт.

    Таблица и оба своих типа уйдут, а значение `habit` останется
    в `annotation_type` навсегда — убрать значение из enum PostgreSQL не умеет.
    Единицы, успевшие получить этот тип, останутся с ним и перестанут попадать
    в любую выборку по девяти прежним значениям.

    Написано здесь, а не умолчано, потому что откат, выглядящий полным
    и не являющийся им, хуже отсутствующего: по нему принимают решение.
    """
    op.drop_index("ix_habits_active", table_name="habits")
    op.drop_index("ix_habits_source_entry_id", table_name="habits")
    op.drop_table("habits")
    postgresql.ENUM(name=STATUS).drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name=RHYTHM).drop(op.get_bind(), checkfirst=True)
