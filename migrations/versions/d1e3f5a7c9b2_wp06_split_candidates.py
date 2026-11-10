"""WP06: candidate split private cho builder, metadata-only cho admin."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d1e3f5a7c9b2"
down_revision: str | None = "d0e2f4a6c8b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "learning_split_candidate"
BUILDER = "crypidian_candidate_builder"
ADMIN = "crypidian_admin_web"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
ADMIN_COLUMNS = (
    "id",
    "created_at",
    "workspace_id",
    "trial_record_id",
    "proof_record_id",
    "proof_hash",
    "manifest_hash",
    "split_policy_version",
    "fold_plan_hash",
    "evaluation_cutoff",
    "preview",
)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("trial_record_id", sa.Uuid(), nullable=False),
        sa.Column("proof_record_id", sa.Uuid(), nullable=False),
        sa.Column("proof_hash", sa.String(64), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("split_policy_version", sa.String(128), nullable=False),
        sa.Column("fold_plan_hash", sa.String(64), nullable=False),
        sa.Column("evaluation_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("preview", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("canonical_manifest", sa.Text(), nullable=False),
        sa.Column("canonical_configuration", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND proof_hash ~ '^[0-9a-f]{64}$' AND "
            "fold_plan_hash ~ '^[0-9a-f]{64}$'",
            name="learning_split_candidate_hashes_valid",
        ),
        sa.CheckConstraint(
            "split_policy_version = 'wp06-walk-forward-v1'",
            name="learning_split_candidate_policy_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(preview) = 'object'", name="learning_split_candidate_preview_object"
        ),
        sa.CheckConstraint(
            "length(canonical_manifest) > 0",
            name="learning_split_candidate_manifest_present",
        ),
        sa.CheckConstraint(
            "length(canonical_configuration) > 0",
            name="learning_split_candidate_configuration_present",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "trial_record_id"],
            ["backtest_trial.workspace_id", "backtest_trial.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "proof_record_id"],
            ["candidate_universe_proof.workspace_id", "candidate_universe_proof.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_split_candidate")),
        sa.UniqueConstraint(
            "workspace_id",
            "trial_record_id",
            "manifest_hash",
            "fold_plan_hash",
            name="uq_learning_split_candidate_scope",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_learning_split_candidate_workspace_id"),
    )
    op.create_index(
        "ix_learning_split_candidate_workspace_created",
        TABLE,
        ["workspace_id", "created_at"],
    )
    op.execute(f'ALTER TABLE public."{TABLE}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE public."{TABLE}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY split_candidate_builder_scope ON public."{TABLE}" FOR ALL '
        f"TO {BUILDER} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    op.execute(
        f'CREATE POLICY split_candidate_admin_select ON public."{TABLE}" FOR SELECT '
        f"TO {ADMIN} USING ({WORKSPACE})"
    )
    op.execute(
        "CREATE FUNCTION public.reject_learning_split_candidate_mutation() RETURNS trigger "
        "LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$ BEGIN "
        "RAISE EXCEPTION 'WP06_LEARNING_SPLIT_CANDIDATE_IMMUTABLE'; END $$"
    )
    op.execute(
        f"CREATE TRIGGER learning_split_candidate_immutable BEFORE UPDATE OR DELETE "
        f'ON public."{TABLE}" FOR EACH ROW '
        "EXECUTE FUNCTION public.reject_learning_split_candidate_mutation()"
    )
    op.execute(
        f'REVOKE ALL PRIVILEGES ON TABLE public."{TABLE}" FROM PUBLIC, {ADMIN}, '
        f"{BUILDER}, crypidian_app, crypidian_trading_runtime, crypidian_trainer_worker, "
        "crypidian_backtest_worker"
    )
    op.execute(f'GRANT SELECT, INSERT ON TABLE public."{TABLE}" TO {BUILDER}')
    columns = ", ".join(ADMIN_COLUMNS)
    op.execute(f'GRANT SELECT ({columns}) ON TABLE public."{TABLE}" TO {ADMIN}')
    op.execute(
        "REVOKE ALL ON FUNCTION public.reject_learning_split_candidate_mutation() FROM PUBLIC"
    )

    op.execute("""
        CREATE FUNCTION public.wp06_require_learning_split_candidate() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.learning_split_candidate AS candidate
                WHERE candidate.workspace_id = NEW.workspace_id
                  AND candidate.manifest_hash = NEW.manifest_hash
                  AND candidate.split_policy_version = NEW.split_policy_version
                  AND candidate.fold_plan_hash = NEW.fold_plan_hash
            ) THEN
                RAISE EXCEPTION 'WP06_SPLIT_APPROVAL_CANDIDATE_MISMATCH';
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.dataset_manifest AS manifest
                WHERE manifest.workspace_id = NEW.workspace_id
                  AND manifest.manifest_hash = NEW.manifest_hash
            ) THEN
                RAISE EXCEPTION 'WP06_SPLIT_APPROVAL_AFTER_SEAL';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER learning_split_approval_candidate_guard "
        "BEFORE INSERT ON public.learning_split_approval FOR EACH ROW "
        "EXECUTE FUNCTION public.wp06_require_learning_split_candidate()"
    )
    op.execute("REVOKE ALL ON FUNCTION public.wp06_require_learning_split_candidate() FROM PUBLIC")


def downgrade() -> None:
    op.execute(
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM public.{TABLE}) THEN "
        "RAISE EXCEPTION 'WP06_SPLIT_CANDIDATE_DOWNGRADE_REQUIRES_EMPTY_TABLE'; "
        "END IF; END $$"
    )
    op.execute(
        "DROP TRIGGER learning_split_approval_candidate_guard ON public.learning_split_approval"
    )
    op.execute("DROP FUNCTION public.wp06_require_learning_split_candidate()")
    op.execute(f"DROP TRIGGER learning_split_candidate_immutable ON public.{TABLE}")
    op.execute("DROP FUNCTION public.reject_learning_split_candidate_mutation()")
    op.execute(f'DROP POLICY split_candidate_admin_select ON public."{TABLE}"')
    op.execute(f'DROP POLICY split_candidate_builder_scope ON public."{TABLE}"')
    op.execute(f'ALTER TABLE public."{TABLE}" DISABLE ROW LEVEL SECURITY')
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE public."{TABLE}" FROM {ADMIN}, {BUILDER}')
    op.drop_index("ix_learning_split_candidate_workspace_created", table_name=TABLE)
    op.drop_table(TABLE)
