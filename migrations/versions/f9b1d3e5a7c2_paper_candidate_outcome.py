"""Lưu bền lý do candidate không thành lệnh PAPER (16/11/2026).

AI Analyst (AI-2) phát hiện các bot có tín hiệu nhưng không có lệnh mà không rõ vì sao: lý do
từ chối plan/risk/entry chỉ nằm trong log runtime. Bảng ``paper_candidate_outcome`` ghi mỗi cặp
(decision, giai đoạn, mã lý do) một lần.

Quyền: ``crypidian_trading_runtime`` chỉ SELECT/INSERT trong đúng workspace + bot đang đặt ở
``app.bot_id`` (như ledger PAPER); ``crypidian_admin_web`` chỉ SELECT theo workspace. Không ai được
UPDATE/DELETE (append-only).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9b1d3e5a7c2"
down_revision: str | None = "e7a9c1d3f5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "paper_candidate_outcome"
RUNTIME = "crypidian_trading_runtime"
ADMIN_WEB = "crypidian_admin_web"
_WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
_BOT = "bot_id = NULLIF(pg_catalog.current_setting('app.bot_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(120), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "workspace_id", "decision_id", "stage", "reason_code", name="uq_paper_candidate_outcome"
        ),
        sa.CheckConstraint(
            "stage IN ('ENTRY_BLOCK', 'STRATEGY', 'PLAN', 'RISK', 'ERROR')",
            name="paper_candidate_outcome_stage_valid",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "decision_id"],
            ["bot_decision.workspace_id", "bot_decision.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_paper_candidate_outcome_bot", TABLE, ["workspace_id", "bot_id", "created_at"]
    )
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {TABLE}_runtime_select ON public.{TABLE} FOR SELECT TO {RUNTIME} "
        f"USING ({_WORKSPACE} AND {_BOT})"
    )
    op.execute(
        f"CREATE POLICY {TABLE}_runtime_insert ON public.{TABLE} FOR INSERT TO {RUNTIME} "
        f"WITH CHECK ({_WORKSPACE} AND {_BOT})"
    )
    op.execute(
        f"CREATE POLICY {TABLE}_admin_web_select ON public.{TABLE} FOR SELECT TO {ADMIN_WEB} "
        f"USING ({_WORKSPACE})"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{TABLE} TO {RUNTIME}")
    op.execute(f"GRANT SELECT ON TABLE public.{TABLE} TO {ADMIN_WEB}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON TABLE public.{TABLE} FROM {RUNTIME}, {ADMIN_WEB}")
    for policy in ("runtime_select", "runtime_insert", "admin_web_select"):
        op.execute(f"DROP POLICY {TABLE}_{policy} ON public.{TABLE}")
    op.drop_index("ix_paper_candidate_outcome_bot", table_name=TABLE)
    op.drop_table(TABLE)
