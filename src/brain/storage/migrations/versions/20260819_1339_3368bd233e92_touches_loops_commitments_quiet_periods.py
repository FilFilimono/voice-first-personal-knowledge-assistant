"""touches loops commitments quiet periods

Схема Фазы 4 — касания (ADR-0013). Четыре таблицы и два новых значения
у `override_action`.

  touches           журнал касаний, где **молчание — такая же строка**
  commitments       дела, выведенные из единиц разметки типа «задача»
  loops             открытые петли — ядро системы по `vision.md` §5.2
  quiet_periods     «замолчи на неделю» и «сегодня выходной»

**Про `touches` и уникальность.** Тик просыпается каждые пять минут, и без
`UNIQUE (slot, local_date)` перезапуск воркера в 09:00 отправил бы сводку
дважды. Проверка «а не отправляли ли уже» в коде это бы не закрыла: два
процесса успели бы прочитать пусто. Дедупликация здесь того же рода, что
`entries.dedup_key`, и по той же причине живёт в базе, а не в проверке.

`local_date` — сутки **владельца**, отдельной колонкой. Приведение `created_at`
к таймзоне в запросе означало бы приведение всей таблицы на каждом тике,
то есть 288 раз в сутки.

Молчание отличается от отправки тем, что `sent_at` пуст, а `silence_reason`
заполнен. Constraint требует ровно одного из двух: строка, у которой пусто
и то и другое, означала бы «касание было или не было, неизвестно», а это
ровно то состояние, ради устранения которого таблица и заведена.

**Про два новых значения `override_action`.** `confirm` — владелец посмотрел
спорную разметку и согласился; `auto_resolve` — прошло семь дней и её
зафиксировало само (`vision.md` §7). Разделены намеренно: «я согласился»
и «за меня решили» — разные данные для eval'а классификации, и слитые
в одно значение они сделали бы его точность завышенной.

`ALTER TYPE ... ADD VALUE` идёт здесь же, а не отдельной ревизией: PostgreSQL
12 и новее разрешает его внутри транзакции, и запрет касается только
**использования** нового значения в той же транзакции — эта миграция им
не пользуется. Проверено накатом на живой базе, а не выведено из документации.

**Про `source_unit_id` с `ON DELETE SET NULL`.** Дело и петля переживают
перепрогон разметки, а единица — нет: она уходит вместе со строкой `annotations`
(кнопка «сделать приватной», переразметка). CASCADE здесь удалил бы закрытые
дела молча, RESTRICT — сломал бы кнопку «сделать приватной». SET NULL оставляет
дело живым и честно теряет ссылку на исчезнувший отрезок.

Revision ID: 3368bd233e92
Revises: 2830d825b6f7
Create Date: 2026-08-19 13:39:57.350991+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3368bd233e92"
down_revision: str | None = "2830d825b6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    touch_slot = sa.Enum("morning", "day", "evening", "weekly", name="touch_slot")
    touch_category = sa.Enum("ordered", "morning", "initiative", name="touch_category")
    silence_reason = sa.Enum("nothing_to_say", "quiet", "over_budget", name="silence_reason")
    commitment_state = sa.Enum("open", "done", "dropped", name="commitment_state")
    loop_status = sa.Enum("open", "closed", "irrelevant", name="loop_status")
    quiet_kind = sa.Enum("pause", "day_off", "resume", name="quiet_kind")

    op.create_table(
        "touches",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("slot", touch_slot, nullable=False),
        sa.Column("category", touch_category, nullable=False),
        # Сутки владельца, а не сервера.
        sa.Column("local_date", sa.Date(), nullable=False),
        # Пусто — значит промолчали.
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("silence_reason", silence_reason, nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.CheckConstraint(
            "(sent_at IS NULL) <> (silence_reason IS NULL)",
            name="ck_touches_sent_xor_silent",
        ),
        sa.PrimaryKeyConstraint("id"),
        # Строка на слот в сутки. Это и защита от двойной отправки при
        # перезапуске воркера, и то, что не даёт тику писать 288 строк в день.
        sa.UniqueConstraint("slot", "local_date", name="uq_touches_slot_date"),
    )

    op.create_table(
        "commitments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("source_unit_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Снимок текста дела: сводка обязана уметь его показать даже после
        # перепрогона расшифровки, сдвинувшего границы отрезка.
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("span_start", sa.Integer(), nullable=False),
        sa.Column("span_end", sa.Integer(), nullable=False),
        sa.Column("state", commitment_state, nullable=False, server_default="open"),
        sa.Column("state_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("span_end > span_start", name="ck_commitments_span"),
        sa.ForeignKeyConstraint(["source_entry_id"], ["entries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_unit_id"], ["entry_units.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        # Идемпотентность перепрогона: та же нарезка дубля не заведёт.
        sa.UniqueConstraint(
            "source_entry_id", "span_start", "span_end", name="uq_commitments_entry_span"
        ),
    )
    op.create_index(op.f("ix_commitments_source_entry_id"), "commitments", ["source_entry_id"])
    op.create_index("ix_commitments_open", "commitments", ["state", "created_at"])

    op.create_table(
        "loops",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("source_unit_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", loop_status, nullable=False, server_default="open"),
        sa.Column("last_asked_at", sa.DateTime(timezone=True), nullable=True),
        # Не счётчик ради счётчика: после третьего «ещё думаю» меняется вопрос.
        sa.Column("times_asked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("snooze_until", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["source_entry_id"], ["entries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_unit_id"], ["entry_units.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_loops_source_entry_id"), "loops", ["source_entry_id"])
    op.create_index("ix_loops_open_oldest", "loops", ["status", "created_at"])

    op.create_table(
        "quiet_periods",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("kind", quiet_kind, nullable=False),
        # У `resume` момент в прошлом — снятие паузы это тоже строка журнала.
        sa.Column("until", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_quiet_periods_created_at"), "quiet_periods", ["created_at"])

    op.execute("ALTER TYPE override_action ADD VALUE IF NOT EXISTS 'confirm'")
    op.execute("ALTER TYPE override_action ADD VALUE IF NOT EXISTS 'auto_resolve'")


def downgrade() -> None:
    # Значения enum обратно не убираются: `ALTER TYPE ... DROP VALUE`
    # в PostgreSQL нет вовсе. Это не недосмотр downgrade'а, а свойство базы,
    # и знать о нём стоит до того, как понадобится откат.
    op.drop_table("quiet_periods")
    op.drop_table("loops")
    op.drop_table("commitments")
    op.drop_table("touches")

    for name in (
        "touch_slot",
        "touch_category",
        "silence_reason",
        "commitment_state",
        "loop_status",
        "quiet_kind",
    ):
        op.execute(f"DROP TYPE IF EXISTS {name}")
