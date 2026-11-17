"""CRY-380: Run Report bất biến (17/11/2026).

Mỗi Run Report tất định (AI-1) lưu một hàng theo `report_hash` (SHA-256 nội dung chuẩn hóa); lượt
AI (`analyst_run.run_report_hash`) trỏ tới đây nên dựng lại được đúng dữ liệu AI đã đọc.

- Trigger chặn UPDATE/DELETE với mọi role (kể cả chủ bảng): report đã lưu là bằng chứng.
- `payload->>'report_hash'` phải bằng cột `report_hash`; trùng hash trong workspace bị chặn (lưu
  idempotent bằng ON CONFLICT DO NOTHING ở tầng ứng dụng).
- RLS bắt buộc: admin-web chỉ SELECT. Hiện script AI ghi bằng role admin (migrator); role đọc báo
  cáo riêng là việc tách riêng (CRY-380 phần 2).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ad99c4f520d0"
down_revision: str | None = "a42edbd211e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "run_report"
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
        sa.Column("report_hash", sa.String(64), nullable=False),
        sa.Column("report_version", sa.String(40), nullable=False),
        sa.Column("run_label", sa.String(120), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bot_count", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("workspace_id", "report_hash", name="uq_run_report_hash"),
        sa.CheckConstraint("report_hash ~ '^[0-9a-f]{64}$'", name="run_report_hash_sha256"),
        sa.CheckConstraint(
            "payload->>'report_hash' = report_hash", name="run_report_payload_hash_matches"
        ),
        sa.CheckConstraint("window_start < window_end", name="run_report_window_ordered"),
        sa.CheckConstraint("bot_count >= 0", name="run_report_bot_count_nonnegative"),
    )
    op.create_index("ix_run_report_created", TABLE, ["workspace_id", "created_at"])
    op.execute(
        """
        CREATE FUNCTION public.run_report_reject_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'RUN_REPORT_IMMUTABLE';
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.run_report_reject_mutation() FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER run_report_immutable BEFORE UPDATE OR DELETE ON public.{TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION public.run_report_reject_mutation()"
    )
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {TABLE}_admin_web_select ON public.{TABLE} "
        f"FOR SELECT TO {ADMIN_WEB} USING ({_WORKSPACE})"
    )
    op.execute(f"GRANT SELECT ON TABLE public.{TABLE} TO {ADMIN_WEB}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON TABLE public.{TABLE} FROM {ADMIN_WEB}")
    op.execute(f"DROP POLICY {TABLE}_admin_web_select ON public.{TABLE}")
    op.execute(f"DROP TRIGGER run_report_immutable ON public.{TABLE}")
    op.execute("DROP FUNCTION public.run_report_reject_mutation()")
    op.drop_index("ix_run_report_created", table_name=TABLE)
    op.drop_table(TABLE)
