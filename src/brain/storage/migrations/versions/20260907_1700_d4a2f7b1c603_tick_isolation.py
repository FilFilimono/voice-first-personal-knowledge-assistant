"""tick failures and the failed silence reason

Revision ID: d4a2f7b1c603
Revises: b8e41c7d3a29
Create Date: 2026-09-07 17:00:00.000000+00:00

Изоляция тика касаний — решение владельца 7 сентября 2026, вариант «отказ
строкой в журнал, слот не сгорает».

**Почему это миграция, а не одна правка в коде.** Падение одного механизма
внутри `service.tick` роняло все касания разом — включая **запись молчания
в журнал**, а она и есть то единственное, чем тишина системы отличается от её
смерти. Значит, у отказа обязано быть место в данных, иначе `try/except`
превратил бы громкое падение в тихое и сделал бы хуже, а не лучше.

Мест два, и они отвечают на разные вопросы:

    silence_reason = 'failed'   что случилось **со слотом**: тик до него дошёл
                                и сломался. «Строки нет вовсе» по-прежнему
                                значит другое — воркер не дожил до окна;
    таблица tick_failures       что именно упало. Долг и напоминания привычек
                                слотами не являются (они вне бюджета и вне
                                журнала касаний), и строки в `touches` им
                                не положено — а видеть их отказ владелец
                                обязан. Отсюда отдельная таблица, а не новое
                                значение `touch_slot`: слот участвует
                                в бюджете, а отказ механизма — нет.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4a2f7b1c603"
down_revision: str | None = "b8e41c7d3a29"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `ALTER TYPE ... ADD VALUE` необратим: `DROP VALUE` в PostgreSQL нет вовсе.
    # Цена та же, что у `habit` и у состояний дел, и называется она вслух:
    # значение останется в типе навсегда, откат вернёт схему не в точности
    # прежней.
    #
    # `AFTER 'over_budget'` — в конец осознанно: `failed` не про решение
    # промолчать, а про то, что решить не удалось. Три прежние причины —
    # штатные исходы и читаются вместе.
    op.execute("ALTER TYPE silence_reason ADD VALUE IF NOT EXISTS 'failed' AFTER 'over_budget'")

    op.create_table(
        "tick_failures",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Сутки владельца, а не сервера, — как у `touches` и по той же причине:
        # «что упало сегодня» спрашивается в его часах, а не в UTC.
        sa.Column("local_date", sa.Date(), nullable=False),
        # Имя механизма по-русски: строку читает владелец в `/status`,
        # а не программа. Своего перечисления здесь нет намеренно — механизмы
        # появляются и исчезают вместе с кодом, а новое значение enum стоит
        # миграции и не убирается никогда.
        sa.Column("mechanism", sa.Text(), nullable=False),
        # Текст отказа — одной строкой, для глаз. По нему не ищут и не считают.
        sa.Column("error", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # Единственный запрос к таблице: «что упало за эти сутки».
    op.create_index("ix_tick_failures_day", "tick_failures", ["local_date"])


def downgrade() -> None:
    """Честно: прежнего вида `silence_reason` этот откат не вернёт.

    Таблица уйдёт, а значение `failed` останется в типе навсегда — убрать
    значение из enum PostgreSQL не умеет. Строки журнала, успевшие его
    получить, останутся с ним и перестанут попадать в выборки по трём прежним
    значениям.

    Написано здесь, а не умолчано: откат, выглядящий полным и не являющийся
    им, хуже отсутствующего — по нему принимают решение.
    """
    op.drop_index("ix_tick_failures_day", table_name="tick_failures")
    op.drop_table("tick_failures")
