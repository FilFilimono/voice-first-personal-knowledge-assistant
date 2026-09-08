"""calendar events created from entries and contexts sent before events

Revision ID: a3f9c1d47e02
Revises: d4a2f7b1c603
Create Date: 2026-09-07 20:30:00.000000+00:00

Две таблицы Фазы 5 (ADR-0016), и обе про одно: **сделать проверяемым то,
что иначе проверялось бы намерением.**

    entry_calendar_events   что ушло в календарь и из какой записи.
                            Инвариант 4 и критерий готовности фазы разом:
                            «ни одно событие не создано без моего нажатия»
                            (`tech_plan.md` §8) проверяется тем, что строка
                            здесь появляется только на пути от кнопки;
    event_contexts          что мы сказали перед встречей и **дошло ли это**.

**Почему миграция одна на две таблицы.** Они пишутся разными кусками фазы,
и раздельные ревизии, сделанные параллельно, дали бы две головы alembic
и `upgrade head`, который не проходит. Цена объединения — ровно та, что
описана ниже в `downgrade`, и она мала.

**Две отметки времени у контекста — это находка 7 сентября 2026, положенная
в схему.** В тот день выяснилось, что `touches.sent_at` пишется **до** вызова
отправки: упавшая отправка выглядела в журнале точно так же, как
состоявшаяся, и сутки сгорали молча — с подтверждением в базе. Здесь
намерение (`decided_at`) и доставка (`delivered_at` + `message_id`) — разные
колонки, и строка с пустой второй означает честное «не дошло», а не тишину.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3f9c1d47e02"
down_revision: str | None = "d4a2f7b1c603"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entry_calendar_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Инвариант 4. Без `ondelete`: записи не удаляются вовсе (инвариант 3),
        # и каскад описывал бы событие, которого не бывает.
        sa.Column("source_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("calendar_id", sa.Text(), nullable=False),
        sa.Column("google_event_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_entry_id"], ["entries.id"]),
        sa.PrimaryKeyConstraint("id"),
        # Повторное нажатие кнопки не должно давать вторую строку на то же
        # событие. Уникальность здесь, а не проверкой в коде: гонка двух
        # нажатий проверку в коде проходит.
        sa.UniqueConstraint("google_event_id", name="uq_entry_calendar_events_google_id"),
    )
    # Единственный вопрос к таблице снизу вверх: «что ушло из этой записи».
    op.create_index(
        "ix_entry_calendar_events_entry",
        "entry_calendar_events",
        ["source_entry_id"],
    )

    op.create_table(
        "event_contexts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        # Идентификатор **вхождения**, а не правила повторения: события
        # приходят развёрнутыми (`singleEvents=true`), и у каждой еженедельной
        # планёрки он свой. Иначе контекст пришёл бы к первой встрече серии
        # и больше никогда.
        sa.Column("google_event_id", sa.Text(), nullable=False),
        sa.Column("calendar_id", sa.Text(), nullable=False),
        sa.Column("event_starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_title", sa.Text(), nullable=False),
        # Намерение: тик дошёл до события и взялся за него.
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Доставка: Telegram принял сообщение. Пишется после отправки.
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        # Одно событие — один контекст. Тик идёт раз в пять минут, окно перед
        # встречей пятнадцать: без уникальности владелец получил бы три
        # одинаковых сообщения на одну встречу.
        sa.UniqueConstraint("google_event_id", name="uq_event_contexts_google_id"),
    )
    # Запрос повтора: «что решили, но не доставили, и окно ещё не закрылось».
    op.create_index(
        "ix_event_contexts_undelivered",
        "event_contexts",
        ["event_starts_at"],
        postgresql_where=sa.text("delivered_at IS NULL"),
    )


def downgrade() -> None:
    """Полный и честный: обе таблицы новые, типов не трогали.

    Это отличие от `d4a2f7b1c603`, где `ALTER TYPE ... ADD VALUE` необратим
    и откат возвращал схему не в точности прежней. Здесь такого нет — сказано
    вслух, чтобы обратимость не приходилось выяснять чтением кода.
    """
    op.drop_index("ix_event_contexts_undelivered", table_name="event_contexts")
    op.drop_table("event_contexts")
    op.drop_index("ix_entry_calendar_events_entry", table_name="entry_calendar_events")
    op.drop_table("entry_calendar_events")
