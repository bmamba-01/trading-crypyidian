"""WP06.14: giới hạn đọc PAPER checkpoint cho reader theo membership/bot."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f0a2c4e6b8d6"
down_revision: str | None = "f0a2c4e6b8d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "paper_execution_checkpoint"
READER = "crypidian_admin_report_reader"
POLICY = "wp06_admin_report_reader_paper_checkpoint_select"


def upgrade() -> None:
    op.execute(f"GRANT SELECT ON TABLE public.{TABLE} TO {READER}")
    op.execute(
        f"CREATE POLICY {POLICY} ON public.{TABLE} "
        f"FOR SELECT TO {READER} USING ("
        "public.wp06_admin_report_reader_can_access(workspace_id, bot_id))"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY {POLICY} ON public.{TABLE}")
    op.execute(f"REVOKE SELECT ON TABLE public.{TABLE} FROM {READER}")
