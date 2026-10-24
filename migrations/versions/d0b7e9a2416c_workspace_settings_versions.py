"""Lưu cấu hình workspace theo phiên bản bất biến và có RLS.

Revision ID: d0b7e9a2416c
Revises: a63d41e7b205
Create Date: 2026-10-22 15:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d0b7e9a2416c"
down_revision: str | None = "a63d41e7b205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_settings_version",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_version_id", sa.Uuid(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("settings_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("settings_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="DRAFT", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name="workspace_settings_version_positive"),
        sa.CheckConstraint("schema_version >= 1", name="workspace_settings_schema_positive"),
        sa.CheckConstraint("revision >= 1", name="workspace_settings_revision_positive"),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED')", name="workspace_settings_status_valid"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "parent_version_id"],
            ["workspace_settings_version.workspace_id", "workspace_settings_version.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_workspace_settings_created_by",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "approved_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_workspace_settings_approved_by",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_settings_version")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_workspace_settings_version_id"),
        sa.UniqueConstraint("workspace_id", "version", name="uq_workspace_settings_version_number"),
    )
    op.create_index(
        "ix_workspace_settings_effective",
        "workspace_settings_version",
        ["workspace_id", "status", "effective_at"],
        unique=False,
    )
    op.execute("ALTER TABLE workspace_settings_version ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workspace_settings_version FORCE ROW LEVEL SECURITY")
    policy = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
    op.execute(
        "CREATE POLICY workspace_isolation ON workspace_settings_version "
        f"USING ({policy}) WITH CHECK ({policy})"
    )
    op.execute(
        """
        CREATE FUNCTION protect_workspace_settings_history() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'workspace settings versions are append-only';
            END IF;
            IF OLD.status = 'PUBLISHED' THEN
                RAISE EXCEPTION 'published workspace settings are immutable';
            END IF;
            IF OLD.status <> NEW.status
               AND NOT (OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED') THEN
                RAISE EXCEPTION 'invalid workspace settings state transition';
            END IF;
            IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED'
               AND (NEW.workspace_id, NEW.version, NEW.parent_version_id,
                    NEW.schema_version, NEW.settings_json, NEW.settings_hash,
                    NEW.created_by, NEW.created_at)
                   IS DISTINCT FROM
                   (OLD.workspace_id, OLD.version, OLD.parent_version_id,
                    OLD.schema_version, OLD.settings_json, OLD.settings_hash,
                    OLD.created_by, OLD.created_at) THEN
                RAISE EXCEPTION 'publishing cannot change draft contents';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER workspace_settings_history_immutable "
        "BEFORE UPDATE OR DELETE ON workspace_settings_version "
        "FOR EACH ROW EXECUTE FUNCTION protect_workspace_settings_history()"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE workspace_settings_version TO crypidian_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON TABLE workspace_settings_version TO crypidian_admin_web"
    )
    op.execute("GRANT SELECT ON TABLE workspace_settings_version TO crypidian_trading_runtime")


def downgrade() -> None:
    op.execute("DROP TRIGGER workspace_settings_history_immutable ON workspace_settings_version")
    op.execute("DROP FUNCTION protect_workspace_settings_history()")
    op.execute("DROP POLICY workspace_isolation ON workspace_settings_version")
    op.execute("ALTER TABLE workspace_settings_version DISABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL PRIVILEGES ON TABLE workspace_settings_version FROM crypidian_app")
    op.execute("REVOKE ALL PRIVILEGES ON TABLE workspace_settings_version FROM crypidian_admin_web")
    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE workspace_settings_version FROM crypidian_trading_runtime"
    )
    op.drop_index("ix_workspace_settings_effective", table_name="workspace_settings_version")
    op.drop_table("workspace_settings_version")
