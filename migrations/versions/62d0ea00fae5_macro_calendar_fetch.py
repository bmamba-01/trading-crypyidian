"""Lịch công bố vĩ mô chính thức (AI-4 lát 1, 16/11/2026): mỗi lần tải là một hàng bất biến.

Hàng chứa nguyên danh sách sự kiện đã chuẩn hóa (events) + hash, để trạng thái macro tại một
thời điểm chỉ dùng lần tải gần nhất **trước** thời điểm đó (point-in-time, không nhìn trước).

Quyền: crypidian_ingestion_worker SELECT/INSERT; producer, runtime, backtest và admin-web chỉ
SELECT; RLS bắt buộc theo app.workspace_id. Không ai UPDATE/DELETE (append-only).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "62d0ea00fae5"
down_revision: str | None = "86afb7c1ba2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "macro_calendar_fetch"
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
        sa.Column("source_id", sa.String(80), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_url", sa.String(300), nullable=False),
        sa.Column("policy_fingerprint", sa.String(64), nullable=False),
        sa.Column("events_sha256", sa.String(64), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("coverage_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("events", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("event_count >= 0", name="macro_calendar_fetch_count_nonnegative"),
        sa.CheckConstraint(
            "events_sha256 ~ '^[0-9a-f]{64}$' AND policy_fingerprint ~ '^[0-9a-f]{64}$'",
            name="macro_calendar_fetch_hash_sha256",
        ),
    )
    op.create_index(
        "ix_macro_calendar_fetch_asof", TABLE, ["workspace_id", "source_id", "fetched_at"]
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
    op.drop_index("ix_macro_calendar_fetch_asof", table_name=TABLE)
    op.drop_table(TABLE)
