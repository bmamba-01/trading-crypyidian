"""Thêm bảng user_invitation và cột maker-checker cho workspace_settings_version.

Revision ID: e1f3a5b7c9d1
Revises: d8e2f4a6b8c0
Create Date: 2026-11-18 11:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1f3a5b7c9d1"
down_revision: str | Sequence[str] | None = "e1a2b3c4d5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_invitation",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=40), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("invited_by", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING', 'ACCEPTED', 'REVOKED', 'EXPIRED')",
            name="user_invitation_status_valid",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "invited_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_user_invitation_invited_by",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_user_invitation_token_hash"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_user_invitation_workspace_id"),
    )
    op.create_index(
        "ix_user_invitation_workspace_status",
        "user_invitation",
        ["workspace_id", "status"],
        unique=False,
    )

    op.add_column(
        "workspace_settings_version",
        sa.Column("proposed_by", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.add_column(
        "workspace_settings_version",
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workspace_settings_version",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workspace_settings_version",
        sa.Column("approval_reason", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_workspace_settings_proposed_by",
        "workspace_settings_version",
        "user_membership",
        ["workspace_id", "proposed_by"],
        ["workspace_id", "id"],
    )
    op.drop_constraint(
        "workspace_settings_status_valid",
        "workspace_settings_version",
        type_="check",
    )
    op.create_check_constraint(
        "workspace_settings_status_valid",
        "workspace_settings_version",
        "status IN ('DRAFT', 'PROPOSED', 'APPROVED', 'PUBLISHED', 'RETIRED')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "workspace_settings_status_valid",
        "workspace_settings_version",
        type_="check",
    )
    op.create_check_constraint(
        "workspace_settings_status_valid",
        "workspace_settings_version",
        "status IN ('DRAFT', 'PUBLISHED')",
    )
    op.drop_constraint(
        "fk_workspace_settings_proposed_by",
        "workspace_settings_version",
        type_="foreignkey",
    )
    op.drop_column("workspace_settings_version", "approval_reason")
    op.drop_column("workspace_settings_version", "approved_at")
    op.drop_column("workspace_settings_version", "proposed_at")
    op.drop_column("workspace_settings_version", "proposed_by")
    op.drop_index("ix_user_invitation_workspace_status", table_name="user_invitation")
    op.drop_table("user_invitation")
