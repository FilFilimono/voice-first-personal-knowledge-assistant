"""llm call status code

Revision ID: ab49d34f7d94
Revises: e3fec9bf6f8b
Create Date: 2026-08-24 18:44:47.408530+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ab49d34f7d94"
down_revision: str | None = "e3fec9bf6f8b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Код ответа провайдера. Зачем он колонкой, а не подстрокой в `error` —
    # в `storage/models/llm_call.py`. Nullable: ответа могло не быть вовсе.
    op.add_column("llm_calls", sa.Column("status_code", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_calls", "status_code")
