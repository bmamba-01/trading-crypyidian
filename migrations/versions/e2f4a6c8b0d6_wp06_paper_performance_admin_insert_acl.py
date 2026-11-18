"""Thu hồi quyền INSERT trực tiếp của admin trên report aggregate PAPER."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e2f4a6c8b0d6"
down_revision: str | Sequence[str] | None = "e2f4a6c8b0d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "REVOKE INSERT ON TABLE public.learning_paper_performance_report "
        "FROM PUBLIC, crypidian_admin_web"
    )


def downgrade() -> None:
    op.execute(
        "GRANT INSERT ON TABLE public.learning_paper_performance_report TO crypidian_admin_web"
    )
