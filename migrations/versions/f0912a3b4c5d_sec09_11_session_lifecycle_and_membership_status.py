"""SEC-09 + SEC-11: thêm last_seen_at cho session, GRANT UPDATE cột và enum user_membership.status.

Revision ID: f0912a3b4c5d
Revises: c8d28da699c6
Create Date: 2026-11-19 07:32:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f0912a3b4c5d"
down_revision: str | None = "c8d28da699c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"


def upgrade() -> None:
    """Thêm cột last_seen_at, cấp quyền cập nhật hẹp và ràng buộc trạng thái thành viên."""
    op.add_column(
        "session",
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute(f'GRANT UPDATE (last_seen_at) ON TABLE "session" TO {ADMIN}')
    op.create_check_constraint(
        "user_membership_status_valid",
        "user_membership",
        "status IN ('ACTIVE', 'SUSPENDED', 'REVOKED')",
    )


def downgrade() -> None:
    """Thu hồi ràng buộc trạng thái thành viên, quyền cột last_seen_at và xóa cột."""
    op.drop_constraint("user_membership_status_valid", "user_membership", type_="check")
    op.execute(f'REVOKE UPDATE (last_seen_at) ON TABLE "session" FROM {ADMIN}')
    op.drop_column("session", "last_seen_at")
