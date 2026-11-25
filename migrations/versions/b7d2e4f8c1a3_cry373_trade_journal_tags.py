"""CRY-373: bảng riêng cho nhật ký giao dịch, checklist, tag setup, ảnh đính kèm và audit."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7d2e4f8c1a3"
down_revision: str | None = "0095c0fdba8f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ADMIN_ROLE = "crypidian_admin_web"


def _resolve_admin_role() -> str:
    has_safe: bool = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'crypidian_admin_web_safe'"
                ")"
            )
        )
        .scalar_one()
    )
    return "crypidian_admin_web_safe" if has_safe else _ADMIN_ROLE


def _apply_workspace_rls(table: str) -> None:
    policy = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY workspace_isolation ON "{table}" USING ({policy}) WITH CHECK ({policy})'
    )


def upgrade() -> None:
    op.create_table(
        "trade_journal_entry",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("trade_id", sa.Uuid(), nullable=False),
        sa.Column("author_user_id", sa.Uuid(), nullable=False),
        sa.Column("note_vi", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "checklist_items",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "setup_tags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "mistake_tags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "privacy_mode",
            sa.String(length=32),
            server_default="workspace_team",
            nullable=False,
        ),
        sa.Column("retention_days", sa.Integer(), server_default="365", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "privacy_mode IN ('workspace_team', 'author_only')",
            name="trade_journal_privacy_valid",
        ),
        sa.CheckConstraint(
            "retention_days >= 7 AND retention_days <= 3650",
            name="trade_journal_retention_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(checklist_items) = 'array'",
            name="trade_journal_checklist_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(setup_tags) = 'array'",
            name="trade_journal_setup_tags_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(mistake_tags) = 'array'",
            name="trade_journal_mistake_tags_array",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_trade_journal_entry"),
    )
    op.create_index(
        "ux_trade_journal_active_trade",
        "trade_journal_entry",
        ["workspace_id", "trade_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_trade_journal_workspace_bot",
        "trade_journal_entry",
        ["workspace_id", "bot_id", "updated_at"],
    )
    op.create_index(
        "ix_trade_journal_expires_at",
        "trade_journal_entry",
        ["workspace_id", "expires_at"],
    )
    admin_role = _resolve_admin_role()
    _apply_workspace_rls("trade_journal_entry")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE trade_journal_entry TO {admin_role}")

    op.create_table(
        "trade_journal_attachment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("entry_id", sa.Uuid(), nullable=False),
        sa.Column("file_name", sa.String(length=180), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_ref", sa.String(length=255), nullable=False),
        sa.Column("exif_stripped", sa.Boolean(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "content_type IN ('image/png', 'image/jpeg', 'image/webp')",
            name="trade_journal_att_type_valid",
        ),
        sa.CheckConstraint(
            "byte_size > 0 AND byte_size <= 5242880",
            name="trade_journal_att_size_valid",
        ),
        sa.CheckConstraint("exif_stripped = true", name="trade_journal_att_exif_stripped"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entry_id"], ["trade_journal_entry.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_trade_journal_attachment"),
    )
    op.create_index(
        "ix_trade_journal_att_entry",
        "trade_journal_attachment",
        ["workspace_id", "entry_id", "created_at"],
    )
    _apply_workspace_rls("trade_journal_attachment")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE trade_journal_attachment TO {admin_role}"
    )

    op.create_table(
        "trade_journal_audit",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("entry_id", sa.Uuid(), nullable=False),
        sa.Column("trade_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column(
            "diff_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('CREATED', 'UPDATED', 'ATTACHMENT_ADDED', "
            "'ATTACHMENT_REMOVED', 'SOFT_DELETED', 'RETENTION_PURGED')",
            name="trade_journal_audit_action_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(diff_summary) = 'object'",
            name="trade_journal_audit_diff_object",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_trade_journal_audit"),
    )
    op.create_index(
        "ix_trade_journal_audit_trade",
        "trade_journal_audit",
        ["workspace_id", "trade_id", "created_at"],
    )
    _apply_workspace_rls("trade_journal_audit")
    op.execute(f"GRANT SELECT, INSERT ON TABLE trade_journal_audit TO {admin_role}")


def downgrade() -> None:
    op.drop_index("ix_trade_journal_audit_trade", table_name="trade_journal_audit")
    op.drop_table("trade_journal_audit")
    op.drop_index("ix_trade_journal_att_entry", table_name="trade_journal_attachment")
    op.drop_table("trade_journal_attachment")
    op.drop_index("ix_trade_journal_expires_at", table_name="trade_journal_entry")
    op.drop_index("ix_trade_journal_workspace_bot", table_name="trade_journal_entry")
    op.drop_index("ux_trade_journal_active_trade", table_name="trade_journal_entry")
    op.drop_table("trade_journal_entry")
