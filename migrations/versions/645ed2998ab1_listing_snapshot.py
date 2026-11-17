"""Lần kiểm exchangeInfo Binance (AI-4 lát 3, 16/11/2026): phát hiện niêm yết/gỡ hợp đồng.

Mỗi lần kiểm một hàng bất biến; danh sách hợp đồng chuẩn hóa (``entries``) chỉ lưu khi hash khác
lần trước, để so sánh hai lần tải có nội dung mà không lưu trùng ~950 hợp đồng mỗi giờ.

Quyền: crypidian_ingestion_worker SELECT/INSERT; producer, runtime, backtest và admin-web chỉ
SELECT; RLS bắt buộc theo app.workspace_id. Không ai UPDATE/DELETE (append-only).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "645ed2998ab1"
down_revision: str | None = "00912d59476e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "listing_snapshot"
WRITER = "crypidian_ingestion_worker"
READERS = (
    "crypidian_market_signal_worker",
    "crypidian_trading_runtime",
    "crypidian_backtest_worker",
    "crypidian_admin_web",
)
_WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"


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
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_fingerprint", sa.String(64), nullable=False),
        sa.Column("entries_sha256", sa.String(64), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column("changed", sa.Boolean(), nullable=False),
        sa.Column("entries", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "entries_sha256 ~ '^[0-9a-f]{64}$' AND policy_fingerprint ~ '^[0-9a-f]{64}$'",
            name="listing_snapshot_hash_sha256",
        ),
        sa.CheckConstraint(
            "changed = (entries IS NOT NULL) AND entry_count > 0",
            name="listing_snapshot_entries_iff_changed",
        ),
    )
    op.create_index("ix_listing_snapshot_asof", TABLE, ["workspace_id", "fetched_at"])
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
    op.drop_index("ix_listing_snapshot_asof", table_name=TABLE)
    op.drop_table(TABLE)
