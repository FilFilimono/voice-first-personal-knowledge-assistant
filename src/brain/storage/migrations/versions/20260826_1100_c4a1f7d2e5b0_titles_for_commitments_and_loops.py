"""titles for commitments and loops

Revision ID: c4a1f7d2e5b0
Revises: ab49d34f7d94
Create Date: 2026-08-26 11:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4a1f7d2e5b0"
down_revision: str | None = "ab49d34f7d94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Название дела и петли — то, что владелец читает в сводке вместо куска
    # своей речи (ADR-0015, п. 4). Nullable у обоих намеренно: пусто означает
    # «названия нет», и тогда показывается прежний отрезок. Деградация обязана
    # быть мягкой — отказ модели не имеет права прятать дело из сводки.
    op.add_column("commitments", sa.Column("title", sa.Text(), nullable=True))
    op.add_column("loops", sa.Column("title", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("loops", "title")
    op.drop_column("commitments", "title")
