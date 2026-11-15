"""CK-1: bảng giá trực tiếp (bid/ask tốt nhất) và quyền đọc của admin-web.

Revision ID: a7c9e1b3d5f6
Revises: f1b3d5e7a9c2

``market_live_quote`` giữ **một dòng mỗi (workspace, nguồn, instrument)** và được cập nhật tại
chỗ: đây là bảng hiển thị "giá hiện tại" cho buồng lái biểu đồ, không phải dữ liệu point-in-time
cho chiến lược hay backtest, nên không dùng trigger append-only. Ingestion worker chỉ được
SELECT/INSERT/UPDATE (không DELETE); admin-web, runtime và market-signal chỉ đọc.

Admin-web được thêm quyền đọc ``market_mark_price`` (mẫu mark/index/funding đã có từ
``b4d6f8a0c2e5``) để dải thông số hiện mark price và đếm ngược funding.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c9e1b3d5f6"
down_revision: str | None = "f1b3d5e7a9c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "market_live_quote"
_WRITER = "crypidian_ingestion_worker"
_READERS = ("crypidian_admin_web", "crypidian_trading_runtime", "crypidian_market_signal_worker")


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("bid_price", sa.Numeric(38, 18), nullable=False),
        sa.Column("bid_quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("ask_price", sa.Numeric(38, 18), nullable=False),
        sa.Column("ask_quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "bid_price > 0 AND ask_price >= bid_price AND bid_quantity >= 0 "
            "AND ask_quantity >= 0 AND update_id >= 0",
            name="market_live_quote_values_valid",
        ),
        sa.CheckConstraint("event_at <= received_at", name="market_live_quote_timestamps_causal"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "instrument_id"],
            ["market_instrument.workspace_id", "market_instrument.id"],
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id", "source", "instrument_id", name=op.f("pk_market_live_quote")
        ),
    )
    op.create_index("ix_market_live_quote_symbol", _TABLE, ["workspace_id", "symbol"])
    policy = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
    op.execute(f'ALTER TABLE "{_TABLE}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{_TABLE}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY workspace_isolation ON "{_TABLE}" USING ({policy}) WITH CHECK ({policy})'
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON TABLE market_live_quote TO crypidian_ingestion_worker"
    )
    op.execute("GRANT SELECT ON TABLE market_live_quote TO crypidian_admin_web")
    op.execute("GRANT SELECT ON TABLE market_live_quote TO crypidian_trading_runtime")
    op.execute("GRANT SELECT ON TABLE market_live_quote TO crypidian_market_signal_worker")
    op.execute("GRANT SELECT ON TABLE market_mark_price TO crypidian_admin_web")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON TABLE market_mark_price FROM crypidian_admin_web")
    for role in (_WRITER, *_READERS):
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {_TABLE} FROM {role}")
    op.execute(f'DROP POLICY workspace_isolation ON "{_TABLE}"')
    op.drop_index("ix_market_live_quote_symbol", table_name=_TABLE)
    op.drop_table(_TABLE)
