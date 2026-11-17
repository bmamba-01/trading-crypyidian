"""CRY-384: lưu bối cảnh ngoài Run Report đã đưa cho AI Analyst (16/11/2026).

Cột ``analyst_run.context`` (JSONB, NULL với lượt cũ): khối ``macro_calendar``, ``derivatives``,
``listings`` của chủ đề. Run Report đã có ``run_report_hash``; bối cảnh này nằm trong prompt nên
``input_sha256`` phủ, nhưng trước đây không lưu nên không dựng lại được nguyên văn đầu vào.
Quyền bảng giữ nguyên (grant cấp bảng).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a42edbd211e5"
down_revision: str | None = "7707d82de246"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analyst_run", sa.Column("context", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("analyst_run", "context")
