"""entry units cascade with annotation

`entry_units.annotation_id` переводится с `RESTRICT` на `CASCADE`.

**Почему отдельной миграцией, а не правкой предыдущей.** Предыдущая уже
применена к рабочей базе; применённую миграцию не правят задним числом
(`CLAUDE.md`), поэтому исправление едет вперёд, а не назад.

**Почему `CASCADE`, хотя во всём проекте `RESTRICT`.** `RESTRICT` стоит там,
где удаление означало бы потерю: запись, расшифровку, тему удалять нельзя,
и база обязана этому мешать. Единица — другое: она часть разметки и без неё
не значит ничего, ровно как строка `entry_topics`.

Поймано тестом сразу после первой миграции: кнопка «сделать приватной»
удаляет разметку записи (инвариант 5 — у приватной записи аннотаций
не бывает), и `RESTRICT` на единицах ронял её на любой уже размеченной
записи. Удаление живёт **в двух местах** — `core/memory/overrides.py`
и `core/enrich/repository.py`, — и починка явным `delete` в каждом означала бы,
что третье место, написанное через полгода, про единицы просто забудет.
Каскад забыть невозможно, и это ровно тот довод, по которому в этом проекте
проверки живут на границе, а не у вызывающего.

Revision ID: 2830d825b6f7
Revises: bd53a2b6db97
Create Date: 2026-08-19 11:41:03.117420+00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "2830d825b6f7"
down_revision: str | None = "bd53a2b6db97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "entry_units_annotation_id_fkey"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, "entry_units", type_="foreignkey")
    op.create_foreign_key(
        CONSTRAINT, "entry_units", "annotations", ["annotation_id"], ["id"], ondelete="CASCADE"
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, "entry_units", type_="foreignkey")
    op.create_foreign_key(
        CONSTRAINT, "entry_units", "annotations", ["annotation_id"], ["id"], ondelete="RESTRICT"
    )
