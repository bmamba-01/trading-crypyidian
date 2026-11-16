"""Kết quả backtest đề xuất AI Analyst: bản gốc vs bản đề xuất (AI-3, 16/11/2026).

Mỗi đề xuất (``analyst_run`` + chỉ số đề xuất) được backtest một lần trên nến đã lưu với quyền
BACKTEST_RESEARCH; ``result`` giữ chỉ số hai bản, coin bị loại kèm lý do và giả định replay.

Quyền: chỉ ``crypidian_admin_web`` SELECT theo workspace (xem trên Admin); ghi bằng job host qua
container migrator. Append-only: không ai được UPDATE/DELETE.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "86afb7c1ba2d"
down_revision: str | None = "f9b1d3e5a7c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "proposal_backtest"
ADMIN_WEB = "crypidian_admin_web"
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
        sa.Column(
            "analyst_run_id",
            sa.Uuid(),
            sa.ForeignKey("analyst_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("proposal_index", sa.Integer(), nullable=False),
        sa.Column("topic_key", sa.String(120), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=True),
        sa.Column("bot_name", sa.String(160), nullable=False),
        sa.Column("proposal", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "workspace_id", "analyst_run_id", "proposal_index", name="uq_proposal_backtest_ref"
        ),
        sa.CheckConstraint(
            "verdict IN ('BETTER', 'WORSE', 'INCONCLUSIVE', 'UNSUPPORTED')",
            name="proposal_backtest_verdict_valid",
        ),
        sa.CheckConstraint("proposal_index >= 0", name="proposal_backtest_index_nonnegative"),
    )
    op.create_index("ix_proposal_backtest_created", TABLE, ["workspace_id", "created_at"])
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {TABLE}_admin_web_select ON public.{TABLE} FOR SELECT TO {ADMIN_WEB} "
        f"USING ({_WORKSPACE})"
    )
    op.execute(f"GRANT SELECT ON TABLE public.{TABLE} TO {ADMIN_WEB}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON TABLE public.{TABLE} FROM {ADMIN_WEB}")
    op.execute(f"DROP POLICY {TABLE}_admin_web_select ON public.{TABLE}")
    op.drop_index("ix_proposal_backtest_created", table_name=TABLE)
    op.drop_table(TABLE)
