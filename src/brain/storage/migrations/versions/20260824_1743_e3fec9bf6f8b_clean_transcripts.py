"""clean transcripts

Revision ID: e3fec9bf6f8b
Revises: 3368bd233e92
Create Date: 2026-08-24 17:43:21.756890+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3fec9bf6f8b"
down_revision: str | None = "3368bd233e92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Вычищенная версия лежит рядом с дословной, а не вместо неё: инвариант
    # «сырое неизменяемо» про то, чтобы ничего не потерять, — не про то,
    # чтобы показывать сырое (ADR-0015, ось 2).
    op.add_column("transcripts", sa.Column("clean_text", sa.Text(), nullable=True))
    # Чем вычищено — отдельно от того, чем расшифровано: это разные вызовы
    # и, по замыслу, разные модели.
    op.add_column("transcripts", sa.Column("clean_model", sa.Text(), nullable=True))
    op.add_column("transcripts", sa.Column("cleaned_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("transcripts", "cleaned_at")
    op.drop_column("transcripts", "clean_model")
    op.drop_column("transcripts", "clean_text")
