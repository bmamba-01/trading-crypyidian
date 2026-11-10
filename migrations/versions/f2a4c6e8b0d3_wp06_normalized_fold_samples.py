"""WP06: lưu sample chuẩn hóa theo manifest và assignment fold bất biến."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f2a4c6e8b0d3"
down_revision: str | None = "f9b1d3e5a7c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BUILDER = "crypidian_candidate_builder"
ADMIN = "crypidian_admin_web"
APP = "crypidian_app"
TRAINER = "crypidian_trainer_worker"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
TABLES = (
    "learning_normalized_dataset",
    "learning_normalized_sample",
    "learning_fold_assignment",
)


def _assert_builder_role() -> None:
    op.execute(f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE rolname='{BUILDER}'
            AND rolcanlogin AND NOT (rolsuper OR rolbypassrls OR rolinherit OR rolcreatedb
            OR rolcreaterole OR rolreplication)
            AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m
                WHERE m.roleid=r.oid OR m.member=r.oid)) THEN
            RAISE EXCEPTION 'WP06_NORMALIZED_BUILDER_ROLE_UNSAFE';
        END IF;
    END $$""")


def _create_immutable_trigger(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON public.{table} "
        "FOR EACH ROW EXECUTE FUNCTION public.reject_learning_normalized_mutation()"
    )


def upgrade() -> None:
    _assert_builder_role()
    op.create_table(
        "learning_normalized_dataset",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("split_policy_version", sa.String(128), nullable=False),
        sa.Column("fold_plan_hash", sa.String(64), nullable=False),
        sa.Column("approval_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("assignment_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND approval_hash ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'",
            name="learning_normalized_dataset_hashes_valid",
        ),
        sa.CheckConstraint(
            "length(split_policy_version) BETWEEN 1 AND 128",
            name="learning_normalized_dataset_policy_present",
        ),
        sa.CheckConstraint(
            "sample_count > 0 AND assignment_count > 0",
            name="learning_normalized_dataset_counts_positive",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "manifest_hash"],
            ["dataset_manifest.workspace_id", "dataset_manifest.manifest_hash"],
            name="fk_learning_normalized_dataset_manifest",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_normalized_dataset")),
        sa.UniqueConstraint(
            "workspace_id",
            "manifest_hash",
            "fold_plan_hash",
            name="uq_learning_normalized_dataset_plan",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "manifest_hash",
            "fold_plan_hash",
            "id",
            name="uq_learning_normalized_dataset_identity",
        ),
    )
    op.create_index(
        "ix_learning_normalized_dataset_workspace_created",
        "learning_normalized_dataset",
        ["workspace_id", "created_at"],
    )
    op.create_table(
        "learning_normalized_sample",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("fold_plan_hash", sa.String(64), nullable=False),
        sa.Column("sample_id", sa.Text(), nullable=False),
        sa.Column("outcome_id", sa.Text(), nullable=False),
        sa.Column("outcome_hash", sa.String(64), nullable=False),
        sa.Column("feature_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("feature_schema_hash", sa.String(64), nullable=False),
        sa.Column("label_hash", sa.String(64), nullable=False),
        sa.Column("label_value", sa.Integer(), nullable=False),
        sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sample_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "outcome_hash ~ '^[0-9a-f]{64}$' AND feature_snapshot_hash ~ '^[0-9a-f]{64}$' "
            "AND feature_schema_hash ~ '^[0-9a-f]{64}$' AND label_hash ~ '^[0-9a-f]{64}$' "
            "AND sample_hash ~ '^[0-9a-f]{64}$'",
            name="learning_normalized_sample_hashes_valid",
        ),
        sa.CheckConstraint(
            "label_value IN (-1, 0, 1)", name="learning_normalized_sample_label_valid"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(features) = 'object' AND features <> '{}'::jsonb",
            name="learning_normalized_sample_features_valid",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "manifest_hash", "fold_plan_hash"],
            [
                "learning_normalized_dataset.workspace_id",
                "learning_normalized_dataset.manifest_hash",
                "learning_normalized_dataset.fold_plan_hash",
            ],
            name="fk_learning_normalized_sample_dataset",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_normalized_sample")),
        sa.UniqueConstraint(
            "workspace_id",
            "manifest_hash",
            "fold_plan_hash",
            "sample_id",
            name="uq_learning_normalized_sample_identity",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "manifest_hash",
            "fold_plan_hash",
            "sample_id",
            "id",
            name="uq_learning_normalized_sample_row_identity",
        ),
    )
    op.create_index(
        "ix_learning_normalized_sample_outcome",
        "learning_normalized_sample",
        ["workspace_id", "outcome_id", "outcome_hash"],
    )
    op.create_table(
        "learning_fold_assignment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("fold_plan_hash", sa.String(64), nullable=False),
        sa.Column("fold_id", sa.String(64), nullable=False),
        sa.Column("sample_id", sa.Text(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.CheckConstraint(
            "fold_id ~ '^[0-9a-f]{64}$'", name="learning_fold_assignment_fold_hash_valid"
        ),
        sa.CheckConstraint(
            "role IN ('train', 'validation', 'test')", name="learning_fold_assignment_role_valid"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "manifest_hash", "fold_plan_hash"],
            [
                "learning_normalized_dataset.workspace_id",
                "learning_normalized_dataset.manifest_hash",
                "learning_normalized_dataset.fold_plan_hash",
            ],
            name="fk_learning_fold_assignment_dataset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "manifest_hash", "fold_plan_hash", "sample_id"],
            [
                "learning_normalized_sample.workspace_id",
                "learning_normalized_sample.manifest_hash",
                "learning_normalized_sample.fold_plan_hash",
                "learning_normalized_sample.sample_id",
            ],
            name="fk_learning_fold_assignment_sample",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_fold_assignment")),
        sa.UniqueConstraint(
            "workspace_id",
            "manifest_hash",
            "fold_plan_hash",
            "fold_id",
            "sample_id",
            name="uq_learning_fold_assignment_role",
        ),
    )
    op.create_index(
        "ix_learning_fold_assignment_lookup",
        "learning_fold_assignment",
        ["workspace_id", "manifest_hash", "fold_plan_hash", "fold_id", "role"],
    )

    op.execute("""
        CREATE FUNCTION public.reject_learning_normalized_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$ BEGIN
            RAISE EXCEPTION 'WP06_NORMALIZED_SAMPLE_IMMUTABLE';
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.reject_learning_normalized_mutation() FROM PUBLIC")
    for table in TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY normalized_builder_scope ON public.{table} FOR ALL TO {BUILDER} "
            f"USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
        )
        _create_immutable_trigger(table)
        op.execute(
            f"REVOKE ALL PRIVILEGES ON TABLE public.{table} FROM PUBLIC, {APP}, {ADMIN}, "
            f"{TRAINER}, {BUILDER}"
        )
        op.execute(f"GRANT SELECT, INSERT ON TABLE public.{table} TO {BUILDER}")

    op.execute(
        f"CREATE POLICY normalized_builder_manifest_select ON public.dataset_manifest "
        f"FOR SELECT TO {BUILDER} USING ({WORKSPACE})"
    )
    op.execute(f"GRANT SELECT ON TABLE public.dataset_manifest TO {BUILDER}")
    op.execute(
        f"CREATE POLICY normalized_builder_approval_select ON public.learning_split_approval "
        f"FOR SELECT TO {BUILDER} USING ({WORKSPACE})"
    )
    op.execute(f"GRANT SELECT ON TABLE public.learning_split_approval TO {BUILDER}")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.learning_fold_assignment)
            OR EXISTS (SELECT 1 FROM public.learning_normalized_sample)
            OR EXISTS (SELECT 1 FROM public.learning_normalized_dataset) THEN
            RAISE EXCEPTION 'WP06_NORMALIZED_SAMPLE_DOWNGRADE_REQUIRES_EMPTY_TABLES';
        END IF;
    END $$""")
    for table in reversed(TABLES):
        op.execute(f"DROP TRIGGER {table}_immutable ON public.{table}")
        op.execute(f"DROP POLICY normalized_builder_scope ON public.{table}")
        op.execute(f"ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.{table} FROM {BUILDER}")
    op.execute("DROP FUNCTION public.reject_learning_normalized_mutation()")
    op.execute("DROP POLICY normalized_builder_manifest_select ON public.dataset_manifest")
    op.execute("REVOKE SELECT ON TABLE public.dataset_manifest FROM " + BUILDER)
    op.execute("DROP POLICY normalized_builder_approval_select ON public.learning_split_approval")
    op.execute("REVOKE SELECT ON TABLE public.learning_split_approval FROM " + BUILDER)
    op.drop_index("ix_learning_fold_assignment_lookup", table_name="learning_fold_assignment")
    op.drop_table("learning_fold_assignment")
    op.drop_index("ix_learning_normalized_sample_outcome", table_name="learning_normalized_sample")
    op.drop_table("learning_normalized_sample")
    op.drop_index(
        "ix_learning_normalized_dataset_workspace_created",
        table_name="learning_normalized_dataset",
    )
    op.drop_table("learning_normalized_dataset")
