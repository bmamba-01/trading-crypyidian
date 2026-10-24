"""Thêm trạng thái MFA chống phát lại và CSRF cho session.

Revision ID: 9f3c2d8a6e41
Revises: 48a80bae456d
Create Date: 2026-10-22 09:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9f3c2d8a6e41"
down_revision: str | None = "48a80bae456d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Mở rộng session và MFA; session cũ cần xác thực lại để có CSRF token."""
    op.add_column("session", sa.Column("csrf_token_hash", sa.String(length=128), nullable=True))
    op.add_column(
        "session", sa.Column("mfa_verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("mfa_factor", sa.Column("last_used_counter", sa.Integer(), nullable=True))
    op.execute("GRANT INSERT, UPDATE ON TABLE workspace TO crypidian_admin_web")


def downgrade() -> None:
    """Thu hồi quyền bootstrap và bỏ các trường được bổ sung trong revision này."""
    op.execute("REVOKE INSERT, UPDATE ON TABLE workspace FROM crypidian_admin_web")
    op.drop_column("mfa_factor", "last_used_counter")
    op.drop_column("session", "mfa_verified_at")
    op.drop_column("session", "csrf_token_hash")
