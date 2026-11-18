"""DATA-1: bổ sung cột basis và 24h ticker cho derivatives_snapshot.

Revision ID: d4f6a8b0c2e1
Revises: c9e1a3b5d7f9
Create Date: 2026-11-18 09:40:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4f6a8b0c2e1"
down_revision = "c9e1a3b5d7f9"
branch_labels = None
depends_on = None

TABLE = "derivatives_snapshot"
_STAT = sa.Numeric(38, 18)


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("index_price", _STAT, nullable=True))
    op.add_column(TABLE, sa.Column("basis", _STAT, nullable=True))
    op.add_column(TABLE, sa.Column("basis_rate", _STAT, nullable=True))
    op.add_column(TABLE, sa.Column("basis_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("ticker_last_price", _STAT, nullable=True))
    op.add_column(TABLE, sa.Column("ticker_quote_volume_24h", _STAT, nullable=True))
    op.add_column(TABLE, sa.Column("ticker_price_change_pct_24h", _STAT, nullable=True))
    op.add_column(TABLE, sa.Column("ticker_at", sa.DateTime(timezone=True), nullable=True))

    op.drop_constraint("derivatives_snapshot_timestamps_causal", TABLE, type_="check")
    op.create_check_constraint(
        "derivatives_snapshot_timestamps_causal",
        TABLE,
        "premium_time <= collected_at"
        " AND (oi_at IS NULL OR oi_at <= collected_at)"
        " AND (ls_at IS NULL OR ls_at <= collected_at)"
        " AND (top_at IS NULL OR top_at <= collected_at)"
        " AND (taker_at IS NULL OR taker_at <= collected_at)"
        " AND (basis_at IS NULL OR basis_at <= collected_at)"
        " AND (ticker_at IS NULL OR ticker_at <= collected_at)",
    )

    op.drop_constraint("derivatives_snapshot_values_valid", TABLE, type_="check")
    op.create_check_constraint(
        "derivatives_snapshot_values_valid",
        TABLE,
        "mark_price > 0"
        " AND (oi_value_usdt IS NULL OR oi_value_usdt >= 0)"
        " AND (index_price IS NULL OR index_price > 0)"
        " AND (ticker_last_price IS NULL OR ticker_last_price > 0)"
        " AND (ticker_quote_volume_24h IS NULL OR ticker_quote_volume_24h >= 0)",
    )


def downgrade() -> None:
    op.drop_constraint("derivatives_snapshot_values_valid", TABLE, type_="check")
    op.create_check_constraint(
        "derivatives_snapshot_values_valid",
        TABLE,
        "mark_price > 0 AND (oi_value_usdt IS NULL OR oi_value_usdt >= 0)",
    )

    op.drop_constraint("derivatives_snapshot_timestamps_causal", TABLE, type_="check")
    op.create_check_constraint(
        "derivatives_snapshot_timestamps_causal",
        TABLE,
        "premium_time <= collected_at"
        " AND (oi_at IS NULL OR oi_at <= collected_at)"
        " AND (ls_at IS NULL OR ls_at <= collected_at)"
        " AND (top_at IS NULL OR top_at <= collected_at)"
        " AND (taker_at IS NULL OR taker_at <= collected_at)",
    )

    for col in (
        "ticker_at",
        "ticker_price_change_pct_24h",
        "ticker_quote_volume_24h",
        "ticker_last_price",
        "basis_at",
        "basis_rate",
        "basis",
        "index_price",
    ):
        op.drop_column(TABLE, col)
