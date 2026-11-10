"""WP-06: ghép source-rights lineage và chỉ mục truy ngược manifest."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6b8d0e2a4c6"
down_revision: str | None = "f4a6c8d0e2b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
TRAINER = "crypidian_trainer_worker"
_WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "dataset_manifest_source_ref",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_id", sa.Uuid(), nullable=False),
        sa.Column("sample_id", sa.Text(), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("item_id", sa.String(128), nullable=False),
        sa.Column("revision_id", sa.String(128), nullable=False),
        sa.Column("policy_id", sa.String(160), nullable=False),
        sa.Column("policy_sequence", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("rights_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "source_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' AND "
            "item_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' AND "
            "revision_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' AND "
            "policy_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$'",
            name="dataset_manifest_source_ref_identifiers_valid",
        ),
        sa.CheckConstraint(
            "policy_sequence > 0", name="dataset_manifest_source_ref_sequence_valid"
        ),
        sa.CheckConstraint(
            "purpose = 'training'", name="dataset_manifest_source_ref_purpose_valid"
        ),
        sa.CheckConstraint(
            "rights_hash ~ '^[0-9a-f]{64}$'", name="dataset_manifest_source_ref_hash_valid"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "manifest_id"],
            ["dataset_manifest.workspace_id", "dataset_manifest.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_manifest_source_ref")),
        sa.UniqueConstraint(
            "workspace_id",
            "manifest_id",
            "sample_id",
            "source_id",
            "item_id",
            "revision_id",
            "policy_id",
            "policy_sequence",
            "purpose",
            name="uq_dataset_manifest_source_ref",
        ),
    )
    op.create_index(
        "ix_dataset_manifest_source_ref_reverse",
        "dataset_manifest_source_ref",
        [
            "workspace_id",
            "source_id",
            "item_id",
            "revision_id",
            "policy_id",
            "policy_sequence",
            "purpose",
        ],
    )
    op.execute('ALTER TABLE public."dataset_manifest_source_ref" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE public."dataset_manifest_source_ref" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY workspace_isolation ON public."dataset_manifest_source_ref" '
        f"TO {ADMIN}, {TRAINER} USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute(
        """
        CREATE FUNCTION public.enforce_dataset_manifest_source_ref() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            canonical_manifest jsonb;
        BEGIN
            IF NEW.workspace_id IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_SOURCE_REF_SCOPE_INVALID';
            END IF;
            SELECT manifest INTO canonical_manifest
              FROM public.dataset_manifest
             WHERE workspace_id = NEW.workspace_id AND id = NEW.manifest_id;
            IF canonical_manifest IS NULL
               OR canonical_manifest->>'manifest_version' <> 'dataset-manifest-v3' THEN
                RAISE EXCEPTION 'WP06_SOURCE_REF_MANIFEST_MISSING_OR_UNSUPPORTED';
            END IF;
            IF NOT EXISTS (
                SELECT 1
                  FROM pg_catalog.jsonb_array_elements(
                      COALESCE(canonical_manifest->'samples', '[]'::jsonb)
                  ) AS sample(value)
                  CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(
                      COALESCE(sample.value->'source_references', '[]'::jsonb)
                  ) AS source_ref(value)
                 WHERE sample.value->>'sample_id' = NEW.sample_id
                   AND source_ref.value->>'source_id' = NEW.source_id
                   AND source_ref.value->>'item_id' = NEW.item_id
                   AND source_ref.value->>'revision_id' = NEW.revision_id
                   AND source_ref.value->>'policy_id' = NEW.policy_id
                   AND (source_ref.value->>'policy_sequence')::integer = NEW.policy_sequence
                   AND source_ref.value->>'purpose' = NEW.purpose
                   AND source_ref.value->>'rights_hash' = NEW.rights_hash
            ) THEN
                RAISE EXCEPTION 'WP06_SOURCE_REF_NOT_IN_MANIFEST';
            END IF;
            RETURN NEW;
        END; $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.enforce_dataset_manifest_source_ref() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER dataset_manifest_source_ref_immutable "
        'BEFORE UPDATE OR DELETE ON public."dataset_manifest_source_ref" '
        "FOR EACH ROW EXECUTE FUNCTION public.reject_dataset_manifest_mutation()"
    )
    op.execute(
        "CREATE TRIGGER dataset_manifest_source_ref_bound "
        'BEFORE INSERT ON public."dataset_manifest_source_ref" '
        "FOR EACH ROW EXECUTE FUNCTION public.enforce_dataset_manifest_source_ref()"
    )
    op.execute(
        'REVOKE ALL PRIVILEGES ON TABLE public."dataset_manifest_source_ref" FROM '
        "PUBLIC, crypidian_app, crypidian_admin_web, crypidian_trading_runtime, "
        "crypidian_ingestion_worker, crypidian_trainer_worker, crypidian_execution_gateway"
    )
    op.execute(f'GRANT SELECT, INSERT ON TABLE public."dataset_manifest_source_ref" TO {ADMIN}')
    op.execute(f'GRANT SELECT ON TABLE public."dataset_manifest_source_ref" TO {TRAINER}')


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM public.dataset_manifest_source_ref) THEN "
        "RAISE EXCEPTION 'WP06_TYPED_SOURCE_REF_DOWNGRADE_REQUIRES_EMPTY_TABLE'; "
        "END IF; END $$"
    )
    op.execute(
        'DROP TRIGGER dataset_manifest_source_ref_immutable ON public."dataset_manifest_source_ref"'
    )
    op.execute(
        'DROP TRIGGER dataset_manifest_source_ref_bound ON public."dataset_manifest_source_ref"'
    )
    op.execute("DROP FUNCTION public.enforce_dataset_manifest_source_ref()")
    op.execute('DROP POLICY workspace_isolation ON public."dataset_manifest_source_ref"')
    op.execute('ALTER TABLE public."dataset_manifest_source_ref" DISABLE ROW LEVEL SECURITY')
    op.execute(
        'REVOKE ALL PRIVILEGES ON TABLE public."dataset_manifest_source_ref" FROM '
        f"PUBLIC, {ADMIN}, {TRAINER}"
    )
    op.drop_index(
        "ix_dataset_manifest_source_ref_reverse", table_name="dataset_manifest_source_ref"
    )
    op.drop_table("dataset_manifest_source_ref")
