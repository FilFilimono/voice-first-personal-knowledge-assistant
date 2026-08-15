"""extensions vector and pg_trgm

Первая миграция проекта. Таблиц не создаёт: схема данных приходит в Фазе 1.
Её работа — включить расширения, на которых стоит весь ретривал:

  vector   — эмбеддинги, `vector(768)` (ADR-0006)
  pg_trgm  — триграммный канал гибридного поиска. Не украшение: русский
             snowball без лемматизации плохо берёт словоформы, а смесь
             с английскими терминами ломает статистику BM25

Расширения ставятся один раз и переживают все последующие миграции, поэтому
им место в самом начале истории.

Revision ID: 1b63f8f1c92f
Revises:
Create Date: 2026-08-15 19:22:16.245880+00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "1b63f8f1c92f"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")


def downgrade() -> None:
    # Обратимость честная, но применять её на базе с данными нельзя:
    # DROP EXTENSION vector снесёт все колонки типа vector вместе с содержимым.
    # Поэтому downgrade заблокирован хуком и существует ради проверки
    # на пустой базе, а не ради отката в проде.
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("DROP EXTENSION IF EXISTS vector")
