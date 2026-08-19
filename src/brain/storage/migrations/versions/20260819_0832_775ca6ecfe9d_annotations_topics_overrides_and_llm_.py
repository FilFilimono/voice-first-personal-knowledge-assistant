"""annotations topics overrides and llm calls

Схема Фазы 3: пять таблиц, и каждая закрывает свой пункт ADR-0003.

  annotations      что модель поняла про запись — тип, состояние, сущности
  topics           темы архива, с вектором названия и обоснованием
  entry_topics     какие темы у записи сейчас
  entry_overrides  смена состояния записи, которую нельзя изменить
  llm_calls        учёт стоимости с первого дня (ADR-0004)

**`entry_overrides` — первая таблица, которая делает `entries` живой.** До неё
запись после вставки не менялась вовсе; теперь у неё есть состояние, и оно
меняется единственным законным способом — новой строкой. Права на `entries`
эта миграция не трогает: инвариант 3 остаётся ровно таким, каким был.

**Про вектор темы.** `vector(768)` — тот же срез MRL, что у чанков (ADR-0006),
и по той же причине число живёт в двух местах сразу: колонка меняется больно,
а константа в коде однажды поменяется. Индекса по нему здесь нет намеренно:
пока тем меньше полутора сотен, кандидатов отбирает не близость, а промпт
целиком (ADR-0011), и строить HNSW по трём десяткам строк не за чем.

Revision ID: 775ca6ecfe9d
Revises: e6b58a7567ae
Create Date: 2026-08-19 08:32:44.128571+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "775ca6ecfe9d"
down_revision: str | None = "e6b58a7567ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Дублируется из моделей намеренно: применённую миграцию не правят задним
# числом, а константы в коде однажды поменяются. Совпадение проверяет тест.
DIMENSIONS = 768

ANNOTATION_TYPES = (
    "thought",
    "task",
    "event",
    "loop",
    "decision",
    "error",
    "insight",
    "refusal",
    "noise",
)
ANNOTATION_STATES = ("sure", "two_hypotheses", "unclear")
OVERRIDE_ACTIONS = ("reclassify", "make_private")


def upgrade() -> None:
    # Типы создаются явно и отдельно, а колонки потом ссылаются на готовые
    # (`create_type=False`). Иначе `create_table` попытается создать тип второй
    # раз — а `annotation_type` в `annotations` используется дважды, и миграция
    # падает на собственном же типе.
    for name, values in (
        ("annotation_type", ANNOTATION_TYPES),
        ("annotation_state", ANNOTATION_STATES),
        ("override_action", OVERRIDE_ACTIONS),
    ):
        postgresql.ENUM(*values, name=name).create(op.get_bind())

    annotation_type = postgresql.ENUM(name="annotation_type", create_type=False)
    annotation_state = postgresql.ENUM(name="annotation_state", create_type=False)
    override_action = postgresql.ENUM(name="override_action", create_type=False)

    op.create_table(
        "annotations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_entry_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("type", annotation_type, nullable=False),
        # Пусто у согласия прогонов: у него альтернативы нет по построению.
        sa.Column("alternative_type", annotation_type, nullable=True),
        sa.Column("state", annotation_state, nullable=False),
        sa.Column(
            "entities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["source_entry_id"], ["entries.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_annotations_source_entry_id"), "annotations", ["source_entry_id"])
    # «Текущая разметка записи» — последняя строка по записи. Спрашивают её все.
    op.execute("CREATE INDEX ix_annotations_entry_latest ON annotations (source_entry_id, id DESC)")

    op.create_table(
        "topics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("source_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        # Нормализованное имя: нижний регистр, схлопнутые пробелы, «ё» → «е».
        # Уникальность именно по нему — иначе «учеба» и «учёба» станут двумя
        # темами, и это не выдумка, а результат прогона (ADR-0011).
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["source_entry_id"], ["entries.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_topics_slug"),
    )
    op.create_index(op.f("ix_topics_source_entry_id"), "topics", ["source_entry_id"])
    op.execute(f"ALTER TABLE topics ADD COLUMN embedding vector({DIMENSIONS})")

    op.create_table(
        "entry_topics",
        sa.Column("source_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("topic_id", sa.BigInteger(), nullable=False),
        sa.Column("annotation_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["source_entry_id"], ["entries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["annotation_id"], ["annotations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("source_entry_id", "topic_id"),
    )
    op.create_index(op.f("ix_entry_topics_topic_id"), "entry_topics", ["topic_id"])

    op.create_table(
        "entry_overrides",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_entry_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("action", override_action, nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["source_entry_id"], ["entries.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_entry_overrides_source_entry_id"), "entry_overrides", ["source_entry_id"]
    )
    op.execute(
        "CREATE INDEX ix_entry_overrides_entry_latest ON entry_overrides (source_entry_id, id DESC)"
    )

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("entries_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        # NULL, а не 0: неизвестная цена новой модели — это «не знаем»,
        # а не «бесплатно».
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=8), nullable=True),
        sa.Column("latency_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("succeeded", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_llm_calls_purpose"), "llm_calls", ["purpose"])


def downgrade() -> None:
    op.drop_table("llm_calls")
    op.drop_table("entry_overrides")
    op.drop_table("entry_topics")
    op.drop_table("topics")
    op.drop_table("annotations")
    postgresql.ENUM(name="override_action").drop(op.get_bind())
    postgresql.ENUM(name="annotation_state").drop(op.get_bind())
    postgresql.ENUM(name="annotation_type").drop(op.get_bind())
