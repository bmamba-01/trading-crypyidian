"""Admin-web đọc vốn ban đầu PAPER để dashboard hiển thị tổng vốn.

Chỉ cấp SELECT theo cột không nhạy cảm. Việc lọc dòng do policy RLS `bot_member_isolation`
đã có trên `paper_account_seed` đảm nhiệm (workspace hiện tại; owner/strategy_admin/risk_admin
hoặc thành viên có `bot_grant` còn hiệu lực). Không cấp ghi, không mở `idempotency_key`,
`source_settings_hash`, `trading_account_id`.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d2e4f6a8b0c1"
down_revision: str | None = "c7e9a1b3d5f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
COLUMNS = "workspace_id, bot_id, quote_asset, initial_equity, created_at"


def upgrade() -> None:
    op.execute(f"GRANT SELECT ({COLUMNS}) ON TABLE public.paper_account_seed TO {ADMIN}")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT ({COLUMNS}) ON TABLE public.paper_account_seed FROM {ADMIN}")
