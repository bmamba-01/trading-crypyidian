"""Live ingest: bảng mark price, thanh lý và dòng lệnh taker từ stream công khai."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4d6f8a0c2e5"
down_revision: str | None = "c7e9a1b3d5f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("market_mark_price", "market_liquidation", "market_trade_flow")
_WRITER = "crypidian_ingestion_worker"
_READERS = ("crypidian_trading_runtime", "crypidian_market_signal_worker")


def _common_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key", sa.String(length=240), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _tail_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def _common_constraints(table: str) -> list[sa.Constraint]:
    return [
        sa.CheckConstraint(
            "event_at <= received_at AND received_at <= available_at",
            name=f"{table}_timestamps_causal",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "instrument_id"],
            ["market_instrument.workspace_id", "market_instrument.id"],
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
        sa.UniqueConstraint("workspace_id", "id", name=f"uq_{table}_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "source", "idempotency_key", name=f"uq_{table}_idempotency"
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "market_mark_price",
        *_common_columns(),
        sa.Column("mark_price", sa.Numeric(38, 18), nullable=False),
        sa.Column("index_price", sa.Numeric(38, 18), nullable=False),
        sa.Column("funding_rate", sa.Numeric(38, 18), nullable=True),
        sa.Column("next_funding_time", sa.DateTime(timezone=True), nullable=True),
        *_tail_columns(),
        sa.CheckConstraint(
            "mark_price > 0 AND index_price > 0", name="market_mark_price_prices_positive"
        ),
        *_common_constraints("market_mark_price"),
    )
    op.create_table(
        "market_liquidation",
        *_common_columns(),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("order_type", sa.String(length=40), nullable=False),
        sa.Column("time_in_force", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("price", sa.Numeric(38, 18), nullable=False),
        sa.Column("average_price", sa.Numeric(38, 18), nullable=False),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(38, 18), nullable=False),
        *_tail_columns(),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="market_liquidation_side_valid"),
        sa.CheckConstraint(
            "quantity > 0 AND filled_quantity >= 0 AND filled_quantity <= quantity "
            "AND price >= 0 AND average_price >= 0",
            name="market_liquidation_values_valid",
        ),
        *_common_constraints("market_liquidation"),
    )
    op.create_table(
        "market_trade_flow",
        *_common_columns(),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bucket_seconds", sa.Integer(), nullable=False),
        sa.Column("buy_quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("sell_quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("buy_quote", sa.Numeric(38, 18), nullable=False),
        sa.Column("sell_quote", sa.Numeric(38, 18), nullable=False),
        sa.Column("trade_count", sa.Integer(), nullable=False),
        sa.Column("first_aggregate_id", sa.BigInteger(), nullable=False),
        sa.Column("last_aggregate_id", sa.BigInteger(), nullable=False),
        sa.Column("is_complete", sa.Boolean(), nullable=False),
        *_tail_columns(),
        sa.CheckConstraint(
            "bucket_seconds > 0 AND trade_count > 0 AND first_aggregate_id <= last_aggregate_id",
            name="market_trade_flow_shape_valid",
        ),
        sa.CheckConstraint(
            "buy_quantity >= 0 AND sell_quantity >= 0 AND buy_quote >= 0 AND sell_quote >= 0",
            name="market_trade_flow_values_nonnegative",
        ),
        *_common_constraints("market_trade_flow"),
    )
    op.create_index(
        "ix_market_mark_price_asof",
        "market_mark_price",
        ["workspace_id", "instrument_id", "event_at", "available_at"],
    )
    op.create_index(
        "ix_market_liquidation_asof",
        "market_liquidation",
        ["workspace_id", "instrument_id", "event_at", "available_at"],
    )
    op.create_index(
        "ix_market_trade_flow_asof",
        "market_trade_flow",
        ["workspace_id", "instrument_id", "bucket_start", "available_at"],
    )

    policy = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
    for table in _TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY workspace_isolation ON "{table}" USING ({policy}) WITH CHECK ({policy})'
        )
        # Dùng lại trigger append-only của kho market: không UPDATE/DELETE bản ghi nguồn.
        op.execute(
            f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION reject_market_data_mutation()"
        )
    tables = ", ".join(_TABLES)
    op.execute(f"GRANT SELECT, INSERT ON TABLE {tables} TO {_WRITER}")
    for reader in _READERS:
        op.execute(f"GRANT SELECT ON TABLE {tables} TO {reader}")


def downgrade() -> None:
    tables = ", ".join(_TABLES)
    for role in (_WRITER, *_READERS):
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {tables} FROM {role}")
    for table in reversed(_TABLES):
        op.execute(f'DROP TRIGGER {table}_immutable ON "{table}"')
        op.execute(f'DROP POLICY workspace_isolation ON "{table}"')
        op.drop_index(f"ix_{table}_asof", table_name=table)
        op.drop_table(table)
