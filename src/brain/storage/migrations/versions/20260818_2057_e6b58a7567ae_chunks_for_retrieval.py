"""chunks for retrieval

Схема Фазы 2: одна таблица и три индекса — по одному на канал поиска.

  GIN по tsv               полнотекст, русский snowball
  GIN по text с триграммами  словоформы и опечатки расшифровки
  HNSW по embedding        векторная близость, косинус

Три индекса — это цена решения из ADR-0006: каждый занимает память и замедляет
вставку. Принято сознательно, потому что каналы ошибаются по-разному.

**Про размерность.** `vector(768)` — срез MRL (ADR-0006). Замер на живом
архиве 18.08.2026: срез до 768 воспроизводит соседство полной размерности
в 98% случаев, 1536 не даёт ничего сверх. Менять размерность потом дорого —
перегенерация архива, миграция колонки, пересборка индекса, — поэтому число
живёт и здесь, и в `models/chunk.py`, и совпадение проверяется тестом.

**Про HNSW против IVFFlat.** Выбран HNSW: он не требует обучения на данных,
а таблица на момент миграции пуста, и IVFFlat пришлось бы пересобирать после
первой же загрузки архива. На десятках тысяч записей разница в качестве
незаметна (ADR-0006), так что решает именно это.

Revision ID: e6b58a7567ae
Revises: ec31c86ef599
Create Date: 2026-08-18 20:57:11.402931+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e6b58a7567ae"
down_revision: str | None = "ec31c86ef599"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Дублируется из `models/chunk.py` намеренно: миграция обязана быть
# самодостаточной. Применённую миграцию не правят задним числом, а константа
# в коде однажды поменяется — и тогда старая ревизия должна остаться такой,
# какой её накатили. Совпадение двух чисел проверяет тест.
DIMENSIONS = 768
FTS_CONFIG = "russian"


def upgrade() -> None:
    op.create_table(
        "chunks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_entry_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed(f"to_tsvector('{FTS_CONFIG}', text)", persisted=True),
            nullable=False,
        ),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["source_entry_id"], ["entries.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        # Порядковый номер уникален внутри записи: пересчёт чанков обязан быть
        # идемпотентным, а без этого повторный проход тихо удвоил бы архив.
        sa.UniqueConstraint("source_entry_id", "ordinal", name="uq_chunks_entry_ordinal"),
    )
    op.create_index(op.f("ix_chunks_source_entry_id"), "chunks", ["source_entry_id"])

    # Колонка вектора добавляется сырым SQL: `vector` живёт в расширении,
    # и типа под него у SQLAlchemy нет. Расширение включено первой ревизией.
    # NULL допустим и означает две разные вещи: приватную запись, которой
    # эмбеддинг не положен (инвариант 5), и ещё не посчитанный вектор.
    op.execute(f"ALTER TABLE chunks ADD COLUMN embedding vector({DIMENSIONS})")

    op.execute("CREATE INDEX ix_chunks_tsv ON chunks USING gin (tsv)")
    op.execute("CREATE INDEX ix_chunks_text_trgm ON chunks USING gin (text gin_trgm_ops)")
    op.execute(
        "CREATE INDEX ix_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding")
    op.execute("DROP INDEX IF EXISTS ix_chunks_text_trgm")
    op.execute("DROP INDEX IF EXISTS ix_chunks_tsv")
    op.drop_index(op.f("ix_chunks_source_entry_id"), table_name="chunks")
    op.drop_table("chunks")
