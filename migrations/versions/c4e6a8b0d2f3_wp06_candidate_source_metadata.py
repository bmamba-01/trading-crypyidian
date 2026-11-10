"""Cấp quyền cột metadata nguồn cho proof BASE, không cấp OHLCV.

Revision ID: c4e6a8b0d2f3
Revises: b2d4f6a8c0e1
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c4e6a8b0d2f3"
down_revision: str | None = "b2d4f6a8c0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (
    "id,workspace_id,symbol,timeframe,source,logical_key,revision,supersedes_id,"
    "checksum,raw_reference_id,open_time,close_time,available_at,is_closed"
)


def upgrade() -> None:
    """Giữ FORCE RLS hiện hành và chỉ bổ sung chính sách đọc theo workspace."""
    op.execute(
        f"GRANT SELECT ({COLUMNS}) ON TABLE public.market_bar TO crypidian_candidate_builder"
    )


def downgrade() -> None:
    """Gỡ chính xác quyền/chính sách được thêm trong migration này."""
    op.execute(
        f"REVOKE SELECT ({COLUMNS}) ON TABLE public.market_bar FROM crypidian_candidate_builder"
    )
