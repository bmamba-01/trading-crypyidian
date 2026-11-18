"""SEC-20: bảng action_challenge cho step-up theo hành động (TTL ≤ 5p, atomic consume).

Revision ID: e9a1c3f5b7d2
Revises: e9f1a3b5c7d9
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9a1c3f5b7d2"
down_revision: str | None = "e9f1a3b5c7d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
_WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
_MEMBERSHIP = "membership_id = NULLIF(current_setting('app.membership_id', true), '')::uuid"


def upgrade() -> None:
    """Tạo bảng action_challenge, RLS theo workspace + membership và quyền cột hẹp."""
    op.create_table(
        "action_challenge",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("membership_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="PENDING", nullable=False),
        sa.Column("failed_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False),
        sa.Column("verification_token_hash", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_action_challenge_workspace_id"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "session_id"],
            ["session.workspace_id", "session.id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'VERIFIED', 'CONSUMED', 'CANCELLED')",
            name="action_challenge_status_valid",
        ),
        sa.CheckConstraint("length(trim(action)) > 0", name="action_challenge_action_nonempty"),
        sa.CheckConstraint(
            "length(payload_hash) = 64 "
            "AND payload_hash = lower(payload_hash) "
            "AND payload_hash !~ '[^0-9a-f]'",
            name="action_challenge_payload_hash_sha256",
        ),
        sa.CheckConstraint(
            "verification_token_hash IS NULL OR ("
            "length(verification_token_hash) = 64 "
            "AND verification_token_hash = lower(verification_token_hash) "
            "AND verification_token_hash !~ '[^0-9a-f]')",
            name="action_challenge_token_hash_sha256",
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + interval '300 seconds'",
            name="action_challenge_ttl_valid",
        ),
        sa.CheckConstraint(
            "max_attempts = 5 AND failed_attempts >= 0 AND failed_attempts <= max_attempts",
            name="action_challenge_attempts_valid",
        ),
        sa.CheckConstraint(
            "(status = 'CONSUMED') = (consumed_at IS NOT NULL)",
            name="action_challenge_consumed_consistent",
        ),
        sa.CheckConstraint(
            "(status = 'CANCELLED') = (cancelled_at IS NOT NULL)",
            name="action_challenge_cancelled_consistent",
        ),
        sa.CheckConstraint(
            "(status NOT IN ('VERIFIED', 'CONSUMED')) OR (verified_at IS NOT NULL)",
            name="action_challenge_verified_consistent",
        ),
    )
    op.create_index(
        "ix_action_challenge_session_lookup",
        "action_challenge",
        ["workspace_id", "session_id", "action", "status"],
    )
    op.create_index(
        "ix_action_challenge_expiry",
        "action_challenge",
        ["workspace_id", "expires_at"],
    )

    op.execute("ALTER TABLE action_challenge ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE action_challenge FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY action_challenge_admin_scope ON action_challenge FOR ALL TO {ADMIN} "
        f"USING (({_WORKSPACE}) AND ({_MEMBERSHIP})) "
        f"WITH CHECK (({_WORKSPACE}) AND ({_MEMBERSHIP}))"
    )
    op.execute("REVOKE ALL ON TABLE action_challenge FROM PUBLIC, crypidian_app")
    op.execute(f"GRANT SELECT, INSERT ON TABLE action_challenge TO {ADMIN}")
    op.execute(
        "GRANT UPDATE (status, failed_attempts, verification_token_hash, verified_at, "
        f"consumed_at, cancelled_at) ON TABLE action_challenge TO {ADMIN}"
    )


def downgrade() -> None:
    """Thu hồi quyền, policy và xóa bảng action_challenge."""
    op.execute(f"REVOKE ALL ON TABLE action_challenge FROM {ADMIN}")
    op.execute("DROP POLICY IF EXISTS action_challenge_admin_scope ON action_challenge")
    op.drop_index("ix_action_challenge_expiry", table_name="action_challenge")
    op.drop_index("ix_action_challenge_session_lookup", table_name="action_challenge")
    op.drop_table("action_challenge")
