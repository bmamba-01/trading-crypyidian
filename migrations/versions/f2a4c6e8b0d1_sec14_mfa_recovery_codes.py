"""SEC-14: bảng mfa_recovery_code cho mã khôi phục MFA một lần (băm có salt, RLS, least privilege).

Revision ID: f2a4c6e8b0d1
Revises: c8d28da699c6
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a4c6e8b0d1"
down_revision: str | None = "c8d28da699c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
_WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    """Tạo bảng mfa_recovery_code, bật FORCE RLS và cấp quyền tối thiểu cho admin-web."""
    op.create_table(
        "mfa_recovery_code",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("membership_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("code_salt", sa.String(length=64), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_mfa_recovery_code_workspace_id"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="CASCADE",
            name="fk_mfa_recovery_code_membership",
        ),
        sa.CheckConstraint(
            "length(code_hash) = 64 AND code_hash = lower(code_hash) AND code_hash !~ '[^0-9a-f]'",
            name="mfa_recovery_code_hash_sha256",
        ),
        sa.CheckConstraint(
            "length(code_salt) >= 16",
            name="mfa_recovery_code_salt_nonempty",
        ),
        sa.CheckConstraint(
            "NOT (used_at IS NOT NULL AND revoked_at IS NOT NULL)",
            name="mfa_recovery_code_terminal_exclusive",
        ),
    )
    op.create_index(
        "ix_mfa_recovery_code_active",
        "mfa_recovery_code",
        ["workspace_id", "membership_id", "used_at", "revoked_at"],
    )
    op.create_index(
        "ix_mfa_recovery_code_user",
        "mfa_recovery_code",
        ["workspace_id", "user_id"],
    )

    op.execute("ALTER TABLE mfa_recovery_code ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mfa_recovery_code FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY mfa_recovery_code_admin_scope ON mfa_recovery_code FOR ALL TO {ADMIN} "
        f"USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute("REVOKE ALL ON TABLE mfa_recovery_code FROM PUBLIC, crypidian_app")
    op.execute(f"GRANT SELECT, INSERT ON TABLE mfa_recovery_code TO {ADMIN}")
    op.execute(f"GRANT UPDATE (used_at, revoked_at) ON TABLE mfa_recovery_code TO {ADMIN}")


def downgrade() -> None:
    """Thu hồi quyền, policy RLS và xóa bảng mfa_recovery_code."""
    op.execute(f"REVOKE ALL ON TABLE mfa_recovery_code FROM {ADMIN}")
    op.execute("DROP POLICY IF EXISTS mfa_recovery_code_admin_scope ON mfa_recovery_code")
    op.drop_index("ix_mfa_recovery_code_user", table_name="mfa_recovery_code")
    op.drop_index("ix_mfa_recovery_code_active", table_name="mfa_recovery_code")
    op.drop_table("mfa_recovery_code")
