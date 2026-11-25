"""CRY-452: chính sách retention 90 ngày cho market_funding, market_liquidation, market_trade_flow.

Hợp nhất hai head ``ab26dde001df`` và ``e7f8a9b0c1d2`` thành một head duy nhất.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: str | tuple[str, ...] | None = ("ab26dde001df", "e7f8a9b0c1d2")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES: tuple[tuple[str, str], ...] = (
    ("market_funding", "funding_time"),
    ("market_liquidation", "event_at"),
    ("market_trade_flow", "event_at"),
)
_WRITER = "crypidian_ingestion_worker"


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION enforce_live_market_retention_immutability() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.expires_at <= clock_timestamp() THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'market source records cannot be deleted before expires_at';
            END IF;
            RAISE EXCEPTION 'market source records are append-only';
        END;
        $$
        """
    )

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

    op.execute("DROP FUNCTION enforce_live_market_retention_immutability()")
