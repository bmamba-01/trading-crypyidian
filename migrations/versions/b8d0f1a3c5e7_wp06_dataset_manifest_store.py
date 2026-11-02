"""WP-06: lưu manifest dataset bất biến, tenant-scoped, cho verifier/trainer."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b8d0f1a3c5e7"
down_revision: str | None = "a7c9e2f4b6d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
TRAINER = "crypidian_trainer_worker"
_WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "dataset_manifest",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("query_hash", sa.String(64), nullable=False),
        sa.Column("manifest_version", sa.String(64), nullable=False),
        sa.Column("data_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("feature_schema_version", sa.String(128), nullable=False),
        sa.Column("feature_version", sa.String(128), nullable=False),
        sa.Column("label_version", sa.String(128), nullable=False),
        sa.Column("sample_schema_version", sa.String(128), nullable=False),
        sa.Column("split_policy_version", sa.String(128), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("exclusion_count", sa.Integer(), nullable=False),
        sa.Column("holdout_sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retention_policy_ref", sa.String(240), nullable=False),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("canonical_manifest", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND query_hash ~ '^[0-9a-f]{64}$'",
            name="dataset_manifest_hashes_valid",
        ),
        sa.CheckConstraint("data_start < data_end", name="dataset_manifest_range_valid"),
        sa.CheckConstraint(
            "sample_count >= 0 AND exclusion_count >= 0",
            name="dataset_manifest_counts_nonnegative",
        ),
        sa.CheckConstraint("jsonb_typeof(manifest) = 'object'", name="dataset_manifest_object"),
        sa.CheckConstraint(
            "length(feature_schema_version) > 0 AND length(feature_version) > 0 "
            "AND length(label_version) > 0 AND length(sample_schema_version) > 0 "
            "AND length(split_policy_version) > 0 AND length(retention_policy_ref) > 0",
            name="dataset_manifest_versions_present",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_manifest")),
        sa.UniqueConstraint("workspace_id", "manifest_hash", name="uq_dataset_manifest_hash"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_dataset_manifest_workspace_id"),
    )
    op.create_index(
        "ix_dataset_manifest_workspace_created", "dataset_manifest", ["workspace_id", "created_at"]
    )
    op.create_index(
        "ix_dataset_manifest_workspace_asof", "dataset_manifest", ["workspace_id", "as_of"]
    )
    op.create_index(
        "ix_dataset_manifest_split_policy",
        "dataset_manifest",
        ["workspace_id", "split_policy_version"],
    )

    op.execute('ALTER TABLE "dataset_manifest" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "dataset_manifest" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY workspace_isolation ON "dataset_manifest" '
        f"TO {ADMIN}, {TRAINER} USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute(
        """
        CREATE FUNCTION public.reject_dataset_manifest_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'WP06_DATASET_MANIFEST_IMMUTABLE';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER dataset_manifest_immutable BEFORE UPDATE OR DELETE ON dataset_manifest "
        "FOR EACH ROW EXECUTE FUNCTION public.reject_dataset_manifest_mutation()"
    )
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE dataset_manifest FROM PUBLIC, {ADMIN}, {TRAINER}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE dataset_manifest TO {ADMIN}")
    op.execute(f"GRANT SELECT ON TABLE dataset_manifest TO {TRAINER}")


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM dataset_manifest) THEN "
        "RAISE EXCEPTION 'WP06_DATASET_MANIFEST_DOWNGRADE_REQUIRES_EMPTY_TABLE'; "
        "END IF; END $$"
    )
    op.execute("DROP TRIGGER dataset_manifest_immutable ON dataset_manifest")
    op.execute("DROP FUNCTION public.reject_dataset_manifest_mutation()")
    op.execute('DROP POLICY workspace_isolation ON "dataset_manifest"')
    op.execute('ALTER TABLE "dataset_manifest" DISABLE ROW LEVEL SECURITY')
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE dataset_manifest FROM PUBLIC, {ADMIN}, {TRAINER}")
    op.drop_index("ix_dataset_manifest_split_policy", table_name="dataset_manifest")
    op.drop_index("ix_dataset_manifest_workspace_asof", table_name="dataset_manifest")
    op.drop_index("ix_dataset_manifest_workspace_created", table_name="dataset_manifest")
    op.drop_table("dataset_manifest")
