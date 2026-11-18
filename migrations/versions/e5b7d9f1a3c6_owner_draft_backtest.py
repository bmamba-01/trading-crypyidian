"""CRY-428: lưu backtest bản nháp cấu hình bot của owner vào `proposal_backtest`.

Revision ID: e5b7d9f1a3c6
Revises: 8f8ec8240b11

Owner quyết định 17/11/2026: cho chỉnh thông số thoát lệnh theo bot, nhưng publish thay đổi
khối `exit` bắt buộc có backtest bản gốc (overlay đang publish) vs bản mới (overlay nháp). Kết
quả dùng chung bảng `proposal_backtest` của AI-3 (cùng replay, cùng chỉ số, cùng quyền):

- Cột `source`: `AI_PROPOSAL` (hàng cũ, có `analyst_run_id`) hoặc `OWNER_DRAFT` (không có
  `analyst_run`; `proposal` ghi `draft_id`, `draft_overlay_hash`, `head_overlay_hash`).
- `analyst_run_id`/`topic_key` chỉ bắt buộc với `AI_PROPOSAL` (CHECK ràng buộc theo nguồn).
- Quyền giữ nguyên: chỉ `crypidian_admin_web` SELECT theo workspace (RLS); ghi bằng job host qua
  container migrator. Không ai UPDATE/DELETE. Admin không tự ghi được kết quả để vượt gate.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5b7d9f1a3c6"
down_revision: str | None = "8f8ec8240b11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "proposal_backtest"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("source", sa.String(16), nullable=False, server_default="AI_PROPOSAL"),
    )
    op.alter_column(TABLE, "analyst_run_id", existing_type=sa.Uuid(), nullable=True)
    op.create_check_constraint(
        "proposal_backtest_source_valid", TABLE, "source IN ('AI_PROPOSAL', 'OWNER_DRAFT')"
    )
    op.create_check_constraint(
        "proposal_backtest_source_ref",
        TABLE,
        "(source = 'AI_PROPOSAL') = (analyst_run_id IS NOT NULL)",
    )
    op.create_index(
        "ix_proposal_backtest_owner_draft",
        TABLE,
        ["workspace_id", "bot_id", "created_at"],
        postgresql_where=sa.text("source = 'OWNER_DRAFT'"),
    )


def downgrade() -> None:
    # Hạ cấp sẽ làm mất bằng chứng backtest của owner: chặn khi đã có hàng OWNER_DRAFT.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.{TABLE} WHERE source = 'OWNER_DRAFT') THEN
                RAISE EXCEPTION 'CRY428_OWNER_DRAFT_BACKTEST_DOWNGRADE_BLOCKED';
            END IF;
        END $$;
        """
    )
    op.drop_index("ix_proposal_backtest_owner_draft", table_name=TABLE)
    op.drop_constraint("proposal_backtest_source_ref", TABLE, type_="check")
    op.drop_constraint("proposal_backtest_source_valid", TABLE, type_="check")
    op.alter_column(TABLE, "analyst_run_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_column(TABLE, "source")
