"""SEC-08: bảng auth_rate_limit_state bền vững và chia sẻ giữa các replica.

Revision ID: d9e1f3a5b7c8
Revises: c8d28da699c6
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9e1f3a5b7c8"
down_revision: str | None = "12e554aace5d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
TABLE = "auth_rate_limit_state"


def upgrade() -> None:
    """Tạo bảng auth_rate_limit_state theo khóa băm SHA-256 (không lưu PII, không RLS workspace)."""
    op.create_table(
        TABLE,
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "window_started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("scope_key", name=op.f("pk_auth_rate_limit_state")),
        sa.CheckConstraint(
            "failures >= 0",
            name=op.f("ck_auth_rate_limit_state_failures_nonnegative"),
        ),
        sa.CheckConstraint(
            "scope_type IN ("
            "'ip', 'email', 'pair', 'account', 'familiar', 'step_up', 'telegram_link'"
            ")",
            name=op.f("ck_auth_rate_limit_state_scope_type_valid"),
        ),
        sa.CheckConstraint(
            "scope_key ~ '^[a-z][a-z0-9_]*:[0-9a-f]{64}$'",
            name=op.f("ck_auth_rate_limit_state_scope_key_hashed"),
        ),
    )
    op.create_index(
        "ix_auth_rate_limit_state_updated_at",
        TABLE,
        ["updated_at"],
        unique=False,
    )
    op.execute(f"REVOKE ALL ON TABLE {TABLE} FROM PUBLIC, crypidian_app")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {TABLE} TO {ADMIN}")


def downgrade() -> None:
    """Thu hồi quyền và xóa bảng auth_rate_limit_state."""
    op.execute(f"REVOKE ALL ON TABLE {TABLE} FROM {ADMIN}")
    op.drop_index("ix_auth_rate_limit_state_updated_at", table_name=TABLE)
    op.drop_table(TABLE)
