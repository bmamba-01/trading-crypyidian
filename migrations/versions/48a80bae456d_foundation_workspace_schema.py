"""Tạo schema nền và chính sách tách biệt workspace.

Revision ID: 48a80bae456d
Revises:
Create Date: 2026-10-21 15:32:48.516245
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "48a80bae456d"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Alembic đã sinh các thao tác tạo bảng từ metadata và đã được rà soát.
    op.create_table(
        "user_account",
        sa.Column("email_normalized", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_account")),
        sa.UniqueConstraint("email_normalized", name="uq_user_account_email"),
    )
    op.create_table(
        "workspace",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace")),
        sa.UniqueConstraint("slug", name="uq_workspace_slug"),
    )
    op.create_table(
        "risk_profile_version",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("max_risk_fraction", sa.Numeric(precision=12, scale=10), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "max_risk_fraction >= 0 AND max_risk_fraction <= 1",
            name=op.f("ck_risk_profile_version_risk_fraction_range"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_risk_profile_version_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_profile_version")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_risk_profile_version_workspace_id"),
        sa.UniqueConstraint("workspace_id", "name", "version", name="uq_risk_profile_version_name"),
    )
    op.create_table(
        "secret_reference",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("reference_key", sa.String(length=240), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_secret_reference_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_secret_reference")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_secret_reference_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "provider", "reference_key", name="uq_secret_reference_key"
        ),
    )
    op.create_table(
        "strategy_release",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("recipe", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_strategy_release_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy_release")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_strategy_release_workspace_id"),
        sa.UniqueConstraint("workspace_id", "name", "version", name="uq_strategy_release_version"),
    )
    op.create_index(
        "ix_strategy_release_workspace_status",
        "strategy_release",
        ["workspace_id", "status"],
        unique=False,
    )
    op.create_table(
        "trading_account",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("venue", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("environment", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("max_notional", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "max_notional >= 0", name=op.f("ck_trading_account_max_notional_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_trading_account_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trading_account")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_trading_account_workspace_id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_trading_account_workspace_name"),
    )
    op.create_index(
        "ix_trading_account_workspace_mode_status",
        "trading_account",
        ["workspace_id", "mode", "status"],
        unique=False,
    )
    op.create_table(
        "user_membership",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.id"],
            name=op.f("fk_user_membership_user_id_user_account"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_user_membership_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_membership")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_user_membership_workspace_id"),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_user_membership_workspace_user"),
    )
    op.create_table(
        "watchlist_version",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_watchlist_version_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watchlist_version")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_watchlist_version_workspace_id"),
        sa.UniqueConstraint("workspace_id", "name", "version", name="uq_watchlist_version_name"),
    )
    op.create_table(
        "approval_request",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("trading_account_id", sa.Uuid(), nullable=True),
        sa.Column("subject_type", sa.String(length=48), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "trading_account_id"],
            ["trading_account.workspace_id", "trading_account.id"],
            name=op.f("fk_approval_request_workspace_id_trading_account"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_approval_request_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "requested_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_approval_request_requested_by_membership",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "decided_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_approval_request_decided_by_membership",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_request")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_approval_request_workspace_id"),
    )
    op.create_index(
        "ix_approval_request_workspace_status",
        "approval_request",
        ["workspace_id", "status"],
        unique=False,
    )
    op.create_table(
        "config_version",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("trading_account_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_version_id", sa.Uuid(), nullable=True),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_hash", sa.String(length=128), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "parent_version_id"],
            ["config_version.workspace_id", "config_version.id"],
            name=op.f("fk_config_version_workspace_id_config_version"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "trading_account_id"],
            ["trading_account.workspace_id", "trading_account.id"],
            name=op.f("fk_config_version_workspace_id_trading_account"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_config_version_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_config_version")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_config_version_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "trading_account_id", "version", name="uq_config_version_number"
        ),
    )
    op.create_table(
        "mfa_factor",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("membership_id", sa.Uuid(), nullable=False),
        sa.Column("factor_type", sa.String(length=24), nullable=False),
        sa.Column("secret_reference", sa.String(length=240), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            name=op.f("fk_mfa_factor_workspace_id_user_membership"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mfa_factor")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_mfa_factor_workspace_id"),
    )
    op.create_table(
        "role_binding",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("membership_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=40), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            name=op.f("fk_role_binding_workspace_id_user_membership"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role_binding")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_role_binding_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "membership_id", "role", name="uq_role_binding_membership_role"
        ),
    )
    op.create_table(
        "session",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("membership_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            name=op.f("fk_session_workspace_id_user_membership"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_session_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session")),
        sa.UniqueConstraint("token_hash", name="uq_session_token_hash"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_session_workspace_id"),
    )
    op.create_index(
        "ix_session_workspace_expires", "session", ["workspace_id", "expires_at"], unique=False
    )
    op.create_table(
        "telegram_binding",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("membership_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_user_id", sa.String(length=64), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            name=op.f("fk_telegram_binding_workspace_id_user_membership"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_binding")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_telegram_binding_workspace_id"),
        sa.UniqueConstraint("workspace_id", "telegram_user_id", name="uq_telegram_binding_user"),
    )
    op.create_table(
        "watchlist_item",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("watchlist_version_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("weight", sa.Numeric(precision=12, scale=10), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "watchlist_version_id"],
            ["watchlist_version.workspace_id", "watchlist_version.id"],
            name=op.f("fk_watchlist_item_workspace_id_watchlist_version"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watchlist_item")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_watchlist_item_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "watchlist_version_id", "symbol", name="uq_watchlist_item_symbol"
        ),
    )
    tenant_tables = (
        "user_membership",
        "role_binding",
        "session",
        "mfa_factor",
        "telegram_binding",
        "trading_account",
        "secret_reference",
        "config_version",
        "risk_profile_version",
        "approval_request",
        "watchlist_version",
        "watchlist_item",
        "strategy_release",
    )
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crypidian_app') THEN
                CREATE ROLE crypidian_app NOLOGIN NOSUPERUSER NOBYPASSRLS;
            END IF;
        END $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO crypidian_app")
    for table_name in tenant_tables:
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "{table_name}" TO crypidian_app')
    op.execute("GRANT SELECT ON TABLE workspace TO crypidian_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE user_account TO crypidian_admin_web")
    for role_name in (
        "crypidian_admin_web",
        "crypidian_trading_runtime",
        "crypidian_execution_gateway",
        "crypidian_ingestion_worker",
        "crypidian_trainer_worker",
    ):
        op.execute(f"GRANT crypidian_app TO {role_name}")
    for table_name in tenant_tables:
        op.execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY')
        workspace_policy = (
            "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
        )
        op.execute(
            f'CREATE POLICY workspace_isolation ON "{table_name}" '
            f"USING ({workspace_policy}) WITH CHECK ({workspace_policy})"
        )


def downgrade() -> None:
    # Tắt policy trước khi xóa bảng để migration hoàn nguyên được an toàn.
    for table_name in (
        "user_membership",
        "role_binding",
        "session",
        "mfa_factor",
        "telegram_binding",
        "trading_account",
        "secret_reference",
        "config_version",
        "risk_profile_version",
        "approval_request",
        "watchlist_version",
        "watchlist_item",
        "strategy_release",
    ):
        op.execute(f'DROP POLICY workspace_isolation ON "{table_name}"')
        op.execute(f'ALTER TABLE "{table_name}" DISABLE ROW LEVEL SECURITY')
    for role_name in (
        "crypidian_admin_web",
        "crypidian_trading_runtime",
        "crypidian_execution_gateway",
        "crypidian_ingestion_worker",
        "crypidian_trainer_worker",
    ):
        op.execute(f"REVOKE crypidian_app FROM {role_name}")
    op.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM crypidian_app")
    op.execute("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM crypidian_app")
    op.execute("REVOKE USAGE ON SCHEMA public FROM crypidian_app")
    op.execute("REVOKE SELECT, INSERT, UPDATE ON TABLE user_account FROM crypidian_admin_web")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE crypidian_admin IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM crypidian_app"
    )
    op.execute("DROP ROLE crypidian_app")
    op.drop_table("watchlist_item")
    op.drop_table("telegram_binding")
    op.drop_index("ix_session_workspace_expires", table_name="session")
    op.drop_table("session")
    op.drop_table("role_binding")
    op.drop_table("mfa_factor")
    op.drop_table("config_version")
    op.drop_index("ix_approval_request_workspace_status", table_name="approval_request")
    op.drop_table("approval_request")
    op.drop_table("watchlist_version")
    op.drop_table("user_membership")
    op.drop_index("ix_trading_account_workspace_mode_status", table_name="trading_account")
    op.drop_table("trading_account")
    op.drop_index("ix_strategy_release_workspace_status", table_name="strategy_release")
    op.drop_table("strategy_release")
    op.drop_table("secret_reference")
    op.drop_table("risk_profile_version")
    op.drop_table("workspace")
    op.drop_table("user_account")
