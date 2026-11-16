"""AI-2: bảng kiểm toán AI Analyst (session theo chủ đề + từng lượt phân tích).

``analyst_session`` lưu session CLI agent (Claude/Codex) của mỗi chủ đề để tiếp tục tích lũy ngữ
cảnh; ``analyst_run`` lưu đầu vào đã băm, đầu ra thô, model/usage và kết quả validator số liệu.
Ghi bằng role admin (script AI Analyst chạy qua container migrator); admin-web chỉ ``SELECT`` theo
workspace để hiển thị. RLS bắt buộc theo ``app.workspace_id``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "e7a9c1d3f5b8"
down_revision: str | None = "d5f7b9c1e3a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN_WEB = "crypidian_admin_web"
_WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
TABLES = ("analyst_session", "analyst_run")


def upgrade() -> None:
    op.create_table(
        "analyst_session",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("topic_key", sa.String(120), nullable=False),
        sa.Column("engine", sa.String(40), nullable=False),
        sa.Column("session_id", sa.String(120), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("run_count", sa.Integer(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "workspace_id", "topic_key", "engine", "session_id", name="uq_analyst_session_ref"
        ),
        sa.CheckConstraint("status IN ('ACTIVE', 'RETIRED')", name="analyst_session_status_valid"),
        sa.CheckConstraint("run_count >= 0", name="analyst_session_run_count_nonnegative"),
    )
    op.create_index(
        "ix_analyst_session_topic",
        "analyst_session",
        ["workspace_id", "topic_key", "engine", "status"],
    )
    op.create_table(
        "analyst_run",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("topic_key", sa.String(120), nullable=False),
        sa.Column("engine", sa.String(40), nullable=False),
        sa.Column("session_id", sa.String(120), nullable=True),
        sa.Column("resumed", sa.Boolean(), nullable=False),
        sa.Column("run_report_hash", sa.String(64), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(40), nullable=False),
        sa.Column("output_version", sa.String(40), nullable=False),
        sa.Column("cli_version", sa.String(120), nullable=True),
        sa.Column("model", sa.String(120), nullable=True),
        sa.Column("usage", JSONB(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(200), nullable=True),
        sa.Column("raw_stdout", sa.Text(), nullable=False),
        sa.Column("structured_output", JSONB(), nullable=True),
        sa.Column("validation_errors", JSONB(), nullable=False),
        sa.Column("valid", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_analyst_run_topic_created", "analyst_run", ["workspace_id", "topic_key", "created_at"]
    )
    for table in TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_admin_web_select ON public.{table} "
            f"FOR SELECT TO {ADMIN_WEB} USING ({_WORKSPACE})"
        )
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {ADMIN_WEB}")


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM {ADMIN_WEB}")
        op.execute(f"DROP POLICY {table}_admin_web_select ON public.{table}")
    op.drop_index("ix_analyst_run_topic_created", table_name="analyst_run")
    op.drop_table("analyst_run")
    op.drop_index("ix_analyst_session_topic", table_name="analyst_session")
    op.drop_table("analyst_session")
