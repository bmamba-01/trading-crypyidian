"""SEC-25: thêm thời hạn và lý do cấp/thu hồi cho bot_grant, giữ bất biến lịch sử.

Revision ID: e9f1a3b5c7d9
Revises: e1f3a5b7c9d1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9f1a3b5c7d9"
down_revision: str | None = "e1f3a5b7c9d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("bot_grant", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bot_grant", sa.Column("reason", sa.Text(), nullable=True))
    op.add_column("bot_grant", sa.Column("revoke_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        "bot_grant_revoke_reason_valid",
        "bot_grant",
        "revoke_reason IS NULL OR revoked_at IS NOT NULL",
    )
    op.create_index(
        "ix_bot_grant_workspace_expiry",
        "bot_grant",
        ["workspace_id", "revoked_at", "expires_at"],
    )
    # Cập nhật trigger lịch sử: cho phép ghi revoked_at cùng revoke_reason một lần duy nhất
    # khi thu hồi; vẫn bất biến toàn bộ cột còn lại (gồm expires_at, reason) và chặn xóa.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.guard_bot_grant_history() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE'
                OR (to_jsonb(NEW) - 'revoked_at' - 'revoke_reason')
                    <> (to_jsonb(OLD) - 'revoked_at' - 'revoke_reason')
                OR (
                    OLD.revoked_at IS NOT NULL
                    AND (
                        NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
                        OR NEW.revoke_reason IS DISTINCT FROM OLD.revoke_reason
                    )
                ) THEN
                RAISE EXCEPTION 'MB01_BOT_GRANT_HISTORY_IMMUTABLE';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.guard_bot_grant_history() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE'
                OR (to_jsonb(NEW) - 'revoked_at') <> (to_jsonb(OLD) - 'revoked_at')
                OR (OLD.revoked_at IS NOT NULL
                    AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN
                RAISE EXCEPTION 'MB01_BOT_GRANT_HISTORY_IMMUTABLE';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.drop_index("ix_bot_grant_workspace_expiry", table_name="bot_grant")
    op.drop_constraint("bot_grant_revoke_reason_valid", "bot_grant", type_="check")
    op.drop_column("bot_grant", "revoke_reason")
    op.drop_column("bot_grant", "reason")
    op.drop_column("bot_grant", "expires_at")
