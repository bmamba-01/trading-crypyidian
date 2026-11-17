"""Ảnh chụp phái sinh công khai Binance (AI-4 lát 2, 16/11/2026): funding, OI, long/short, taker.

Mỗi lượt thu (≈ mỗi giờ) một hàng bất biến mỗi symbol; ``collected_at`` là thời điểm dữ liệu khả
dụng cho hệ thống, mọi mốc của Binance phải ≤ mốc này (point-in-time, không nhìn trước).

Quyền: crypidian_ingestion_worker SELECT/INSERT; producer, runtime, backtest và admin-web chỉ
SELECT; RLS bắt buộc theo app.workspace_id. Không ai UPDATE/DELETE (append-only).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "00912d59476e"
down_revision: str | None = "62d0ea00fae5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "derivatives_snapshot"
WRITER = "crypidian_ingestion_worker"
READERS = (
    "crypidian_market_signal_worker",
    "crypidian_trading_runtime",
    "crypidian_backtest_worker",
    "crypidian_admin_web",
)
_WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
_STAT = sa.Numeric(38, 18)


def _ts(name: str, *, nullable: bool) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(40), nullable=False),
        _ts("collected_at", nullable=False),
        sa.Column("policy_fingerprint", sa.String(64), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("mark_price", _STAT, nullable=False),
        sa.Column("funding_rate", _STAT, nullable=False),
        _ts("next_funding_time", nullable=False),
        _ts("premium_time", nullable=False),
        sa.Column("oi_contracts", _STAT, nullable=True),
        sa.Column("oi_value_usdt", _STAT, nullable=True),
        _ts("oi_at", nullable=True),
        sa.Column("oi_value_24h_ago", _STAT, nullable=True),
        sa.Column("long_short_account_ratio", _STAT, nullable=True),
        _ts("ls_at", nullable=True),
        sa.Column("top_position_ratio", _STAT, nullable=True),
        _ts("top_at", nullable=True),
        sa.Column("taker_buy_sell_ratio", _STAT, nullable=True),
        _ts("taker_at", nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "workspace_id", "symbol", "collected_at", name="uq_derivatives_snapshot_point"
        ),
        sa.CheckConstraint(
            "payload_sha256 ~ '^[0-9a-f]{64}$' AND policy_fingerprint ~ '^[0-9a-f]{64}$'",
            name="derivatives_snapshot_hash_sha256",
        ),
        sa.CheckConstraint(
            "premium_time <= collected_at"
            " AND (oi_at IS NULL OR oi_at <= collected_at)"
            " AND (ls_at IS NULL OR ls_at <= collected_at)"
            " AND (top_at IS NULL OR top_at <= collected_at)"
            " AND (taker_at IS NULL OR taker_at <= collected_at)",
            name="derivatives_snapshot_timestamps_causal",
        ),
        sa.CheckConstraint(
            "mark_price > 0 AND (oi_value_usdt IS NULL OR oi_value_usdt >= 0)",
            name="derivatives_snapshot_values_valid",
        ),
    )
    op.create_index(
        "ix_derivatives_snapshot_asof", TABLE, ["workspace_id", "collected_at", "symbol"]
    )
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {TABLE}_writer_select ON public.{TABLE} FOR SELECT TO {WRITER} "
        f"USING ({_WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY {TABLE}_writer_insert ON public.{TABLE} FOR INSERT TO {WRITER} "
        f"WITH CHECK ({_WORKSPACE})"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{TABLE} TO {WRITER}")
    for role in READERS:
        short = role.removeprefix("crypidian_")
        op.execute(
            f"CREATE POLICY {TABLE}_{short}_select ON public.{TABLE} FOR SELECT TO {role} "
            f"USING ({_WORKSPACE})"
        )
        op.execute(f"GRANT SELECT ON TABLE public.{TABLE} TO {role}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON TABLE public.{TABLE} FROM {WRITER}, {', '.join(READERS)}")
    op.execute(f"DROP POLICY {TABLE}_writer_select ON public.{TABLE}")
    op.execute(f"DROP POLICY {TABLE}_writer_insert ON public.{TABLE}")
    for role in READERS:
        short = role.removeprefix("crypidian_")
        op.execute(f"DROP POLICY {TABLE}_{short}_select ON public.{TABLE}")
    op.drop_index("ix_derivatives_snapshot_asof", table_name=TABLE)
    op.drop_table(TABLE)
