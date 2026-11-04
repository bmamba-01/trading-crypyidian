"""WP-05: Lưu trữ revision và lệnh takedown của item nguồn, tenant-scoped và append-only.

Revision ID: d4e5f6a7b8c9
Revises: b8d0f1a3c5e7
Create Date: 2026-11-02 22:50:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "b8d0f1a3c5e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
INGESTION = "crypidian_ingestion_worker"
_WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
_TABLES = ("research_source_item_revisions", "research_source_takedowns")


def upgrade() -> None:
    # 1. Bảng research_source_item_revisions: lưu revision metadata append-only
    op.create_table(
        "research_source_item_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("item_id", sa.String(length=128), nullable=False),
        sa.Column("revision_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("policy_id", sa.String(length=160), nullable=False),
        sa.Column("predecessor_revision_id", sa.String(length=128), nullable=True),
        sa.Column("predecessor_hash", sa.String(length=64), nullable=True),
        sa.Column("revision_hash", sa.String(length=64), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column(
            "canonical_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "is_tombstone",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_source_item_revisions")),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "workspace_id",
            "source_id",
            "item_id",
            "revision_id",
            name="uq_research_source_item_revisions_workspace_item_revision",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "source_id",
            "item_id",
            "sequence",
            name="uq_research_source_item_revisions_workspace_item_sequence",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "id",
            name="uq_research_source_item_revisions_workspace_id",
        ),
        sa.CheckConstraint(
            "sequence >= 1",
            name="source_item_revision_sequence_positive",
        ),
        sa.CheckConstraint(
            "source_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'",
            name="source_item_revision_source_id_valid",
        ),
        sa.CheckConstraint(
            "item_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'",
            name="source_item_revision_item_id_valid",
        ),
        sa.CheckConstraint(
            "revision_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'",
            name="source_item_revision_revision_id_valid",
        ),
        sa.CheckConstraint(
            "length(policy_id) BETWEEN 1 AND 160",
            name="source_item_revision_policy_id_valid",
        ),
        sa.CheckConstraint(
            "revision_hash ~ '^[0-9a-f]{64}$'",
            name="source_item_revision_hash_valid",
        ),
        sa.CheckConstraint(
            "(predecessor_revision_id IS NULL AND predecessor_hash IS NULL) OR "
            "(predecessor_revision_id IS NOT NULL AND predecessor_hash IS NOT NULL "
            "AND predecessor_hash ~ '^[0-9a-f]{64}$' "
            "AND predecessor_revision_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')",
            name="source_item_revision_predecessor_valid",
        ),
        sa.CheckConstraint(
            "available_at IS NULL OR available_at >= observed_at",
            name="source_item_revision_available_not_before_observed",
        ),
        sa.CheckConstraint(
            "published_at IS NULL OR published_at <= observed_at",
            name="source_item_revision_published_not_after_observed",
        ),
        sa.CheckConstraint(
            "edited_at IS NULL OR edited_at <= observed_at",
            name="source_item_revision_edited_not_after_observed",
        ),
        sa.CheckConstraint(
            "published_at IS NULL OR edited_at IS NULL OR edited_at >= published_at",
            name="source_item_revision_edited_not_before_published",
        ),
        sa.CheckConstraint(
            "canonical_url LIKE 'https://%' AND position('#' in canonical_url) = 0",
            name="source_item_revision_canonical_url_valid",
        ),
    )
    op.create_index(
        "ix_research_source_item_revisions_lookup",
        "research_source_item_revisions",
        ["workspace_id", "source_id", "item_id", "sequence"],
    )
    op.create_index(
        "ix_research_source_item_revisions_available",
        "research_source_item_revisions",
        ["workspace_id", "source_id", "item_id", "available_at"],
    )

    # 2. Bảng research_source_takedowns: lưu lệnh gỡ bỏ / vô hiệu hóa item
    op.create_table(
        "research_source_takedowns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("item_id", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_source_takedowns")),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "workspace_id",
            "id",
            name="uq_research_source_takedowns_workspace_id",
        ),
        sa.CheckConstraint(
            "source_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'",
            name="source_takedown_source_id_valid",
        ),
        sa.CheckConstraint(
            "item_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'",
            name="source_takedown_item_id_valid",
        ),
        sa.CheckConstraint(
            "length(reason) > 0",
            name="source_takedown_reason_present",
        ),
        sa.CheckConstraint(
            "length(requested_by) > 0",
            name="source_takedown_requested_by_present",
        ),
        sa.CheckConstraint(
            "effective_at >= requested_at",
            name="source_takedown_effective_not_before_requested",
        ),
    )
    op.create_index(
        "uq_research_source_takedowns_active",
        "research_source_takedowns",
        ["workspace_id", "source_id", "item_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_index(
        "ix_research_source_takedowns_lookup",
        "research_source_takedowns",
        ["workspace_id", "source_id", "item_id", "is_active", "effective_at"],
    )

    # 3. Kích hoạt Row Level Security và Workspace Isolation Policy
    for table in _TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY workspace_isolation ON "{table}" '
            f"TO {ADMIN}, {INGESTION} USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
        )

    # 4. Trigger immutable cho bảng research_source_item_revisions (append-only)
    op.execute(
        """
        CREATE FUNCTION public.reject_source_item_revision_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'WP05_SOURCE_ITEM_REVISION_IMMUTABLE';
        END;
        $$
        """
    )
    op.execute(
        'CREATE TRIGGER "research_source_item_revisions_append_only" '
        'BEFORE UPDATE OR DELETE ON "research_source_item_revisions" '
        "FOR EACH ROW EXECUTE FUNCTION public.reject_source_item_revision_mutation()"
    )
    op.execute("REVOKE ALL ON FUNCTION public.reject_source_item_revision_mutation() FROM PUBLIC")

    # 5. Phân quyền tối thiểu (Least-Privilege Role Hardening)
    op.execute(
        f'REVOKE ALL PRIVILEGES ON TABLE "research_source_item_revisions", '
        f'"research_source_takedowns" FROM PUBLIC, crypidian_app, {ADMIN}, {INGESTION}'
    )
    op.execute(
        f'GRANT SELECT, INSERT ON TABLE "research_source_item_revisions" TO {ADMIN}, {INGESTION}'
    )
    op.execute(f'GRANT SELECT, INSERT, UPDATE ON TABLE "research_source_takedowns" TO {ADMIN}')
    op.execute(f'GRANT SELECT, INSERT ON TABLE "research_source_takedowns" TO {INGESTION}')


def downgrade() -> None:
    op.execute(
        'LOCK TABLE "research_source_item_revisions", "research_source_takedowns" '
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM research_source_item_revisions)
               OR EXISTS (SELECT 1 FROM research_source_takedowns) THEN
                RAISE EXCEPTION 'WP05_SOURCE_ITEM_STORAGE_DOWNGRADE_BLOCKED';
            END IF;
        END;
        $$
        """
    )
    for table in _TABLES:
        op.execute(
            f'REVOKE ALL PRIVILEGES ON TABLE "{table}" '
            f"FROM PUBLIC, crypidian_app, {ADMIN}, {INGESTION}"
        )
        if table == "research_source_item_revisions":
            op.execute(f'DROP TRIGGER "{table}_append_only" ON "{table}"')
        op.execute(f'DROP POLICY workspace_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')

    op.execute("DROP FUNCTION public.reject_source_item_revision_mutation()")

    op.drop_index(
        "ix_research_source_takedowns_lookup",
        table_name="research_source_takedowns",
    )
    op.drop_index(
        "uq_research_source_takedowns_active",
        table_name="research_source_takedowns",
    )
    op.drop_table("research_source_takedowns")

    op.drop_index(
        "ix_research_source_item_revisions_available",
        table_name="research_source_item_revisions",
    )
    op.drop_index(
        "ix_research_source_item_revisions_lookup",
        table_name="research_source_item_revisions",
    )
    op.drop_table("research_source_item_revisions")
