"""CRY-453: retention 90 ngày cho market_mark_price và market_open_interest."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES: tuple[tuple[str, str], ...] = (
    ("market_mark_price", "event_at"),
    ("market_open_interest", "observed_at"),
)
_WRITER = "crypidian_ingestion_worker"


def upgrade() -> None:
    for table, event_col in _TABLES:
        op.execute(f'ALTER TABLE "{table}" DISABLE TRIGGER {table}_immutable')
        op.add_column(table, sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
        op.execute(
            f"UPDATE {table} SET expires_at = {event_col} + interval '90 days' "
            "WHERE expires_at IS NULL"
        )
        op.alter_column(table, "expires_at", nullable=False)
        op.create_index(f"ix_{table}_expires", table, ["workspace_id", "expires_at"])
        op.execute(f'DROP TRIGGER {table}_immutable ON "{table}"')
        op.execute(
            f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION enforce_live_market_retention_immutability()"
        )
        op.execute(f'ALTER TABLE "{table}" ENABLE TRIGGER {table}_immutable')

    tables_sql = ", ".join(table for table, _ in _TABLES)
    op.execute(f"GRANT DELETE ON TABLE {tables_sql} TO {_WRITER}")


def downgrade() -> None:
    tables_sql = ", ".join(table for table, _ in _TABLES)
    op.execute(f"REVOKE DELETE ON TABLE {tables_sql} FROM {_WRITER}")

    for table, _ in reversed(_TABLES):
        op.execute(f'DROP TRIGGER {table}_immutable ON "{table}"')
        op.execute(
            f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION reject_market_data_mutation()"
        )
        op.drop_index(f"ix_{table}_expires", table_name=table)
        op.drop_column(table, "expires_at")
