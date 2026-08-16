"""forbid truncating entries

Дыра в инварианте 3, найденная сразу после предыдущей ревизии: строковый
триггер `BEFORE UPDATE OR DELETE ... FOR EACH ROW` на TRUNCATE не срабатывает
вовсе, а `REVOKE TRUNCATE` не касается суперпользователя. То есть
`TRUNCATE entries CASCADE` сносил архив целиком, не встретив ни одной проверки.

Закрывается операторным триггером: TRUNCATE — операция уровня таблицы,
и ловить её нужно на том же уровне.

Отдельной ревизией, а не правкой предыдущей: применённая миграция задним числом
не правится, откат запрещён. Дешевле объяснить лишнюю ревизию в истории, чем
завести привычку переписывать применённое.

Revision ID: ec31c86ef599
Revises: db2e48543b3e
Create Date: 2026-08-15 21:43:26.108455+00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "ec31c86ef599"
down_revision: str | None = "db2e48543b3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION fn_entries_forbid_truncate() RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'entries только на вставку: TRUNCATE запрещён (инвариант 3).'
                USING ERRCODE = 'raise_exception';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER tg_entries_forbid_truncate
        BEFORE TRUNCATE ON entries
        FOR EACH STATEMENT EXECUTE FUNCTION fn_entries_forbid_truncate()
        """
    )


def downgrade() -> None:
    # Формальный, как и у всех наших миграций: выполнение заблокировано хуком.
    op.execute("DROP TRIGGER IF EXISTS tg_entries_forbid_truncate ON entries")
    op.execute("DROP FUNCTION IF EXISTS fn_entries_forbid_truncate()")
