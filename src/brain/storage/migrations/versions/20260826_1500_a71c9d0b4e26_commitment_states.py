"""commitment states in progress and snoozed

Revision ID: a71c9d0b4e26
Revises: c4a1f7d2e5b0
Create Date: 2026-08-26 15:00:00.000000+00:00

Между «висит» и «закрыто» у живого дела есть середина, и без неё владелец жал
«сделал» на том, что только начал. Добавляются два состояния и срок, до которого
отложенное дело не показывается.

**Миграция данных не нужна:** существующие дела остаются `open`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a71c9d0b4e26"
down_revision: str | None = "c4a1f7d2e5b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `ALTER TYPE ... ADD VALUE` необратим: `DROP VALUE` в PostgreSQL нет вовсе.
    # Цена названа заранее и принята владельцем — то же свойство базы, что
    # записано в ADR-0013 и ADR-0015: значение остаётся в типе навсегда.
    op.execute("ALTER TYPE commitment_state ADD VALUE IF NOT EXISTS 'in_progress'")
    op.execute("ALTER TYPE commitment_state ADD VALUE IF NOT EXISTS 'snoozed'")
    # Срок, до которого отложенное дело не показывается. Nullable, потому что
    # у остальных четырёх состояний срока нет; заполненность у `snoozed`
    # держится не схемой, а единственной дверью перехода
    # (`digest.commitments.set_state`) и путевым тестом на неё.
    op.add_column(
        "commitments", sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Честно: прежнего вида типа этот откат не вернёт.

    Колонка уйдёт, а `in_progress` и `snoozed` останутся в `commitment_state`
    навсегда — убрать значение из enum PostgreSQL не умеет. Дела, успевшие
    оказаться в одном из новых состояний, останутся в нём и перестанут
    попадать в любую выборку по трём прежним значениям.

    Написано это здесь, а не умолчано, потому что откат, выглядящий полным
    и не являющийся им, хуже отсутствующего: по нему принимают решение.
    """
    op.drop_column("commitments", "snoozed_until")
