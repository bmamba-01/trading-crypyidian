"""WP06.4: lưu report đánh giá chẩn đoán bất biến theo workspace."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f3a5c7d9e1b2"
down_revision: str | None = "a8c0e2f4b6d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
TRAINER = "crypidian_trainer_worker"
_WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
TABLE = "learning_evaluation_report"
IMMUTABLE_FUNCTION = "public.reject_learning_evaluation_report_mutation"


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
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("report_hash", sa.String(64), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("fold_plan_hash", sa.String(64), nullable=False),
        sa.Column("split_policy_version", sa.String(128), nullable=False),
        sa.Column("evaluator_version", sa.String(64), nullable=False),
        sa.Column("evaluation_mode", sa.String(24), nullable=False),
        sa.Column("candidate_completeness_verified", sa.Boolean(), nullable=False),
        sa.Column("evaluation_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("canonical_report", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "report_hash ~ '^[0-9a-f]{64}$' AND manifest_hash ~ '^[0-9a-f]{64}$' "
            "AND fold_plan_hash ~ '^[0-9a-f]{64}$'",
            name="learning_evaluation_report_hashes_valid",
        ),
        sa.CheckConstraint("seed >= 0", name="learning_evaluation_report_seed_nonnegative"),
        sa.CheckConstraint(
            "evaluation_mode = 'diagnostic' AND candidate_completeness_verified IS FALSE",
            name="learning_evaluation_report_diagnostic_only",
        ),
        sa.CheckConstraint(
            "length(split_policy_version) > 0 AND length(evaluator_version) > 0",
            name="learning_evaluation_report_versions_present",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(report) = 'object' AND length(canonical_report) > 0",
            name="learning_evaluation_report_payload_valid",
        ),
        sa.CheckConstraint(
            "COALESCE(report ->> 'evaluation_mode', '') = 'diagnostic' "
            "AND COALESCE(report ->> 'candidate_completeness_verified', '') = 'false' "
            "AND COALESCE(report ->> 'manifest_hash', '') = manifest_hash "
            "AND COALESCE(report ->> 'fold_plan_hash', '') = fold_plan_hash",
            name="learning_evaluation_report_payload_diagnostic",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "manifest_hash"],
            ["dataset_manifest.workspace_id", "dataset_manifest.manifest_hash"],
            ondelete="RESTRICT",
            name="fk_learning_evaluation_report_manifest",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_evaluation_report")),
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_evaluation_report_idempotency"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_learning_evaluation_report_workspace"),
    )
    op.create_index(
        "ix_learning_evaluation_report_workspace_created",
        TABLE,
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_learning_evaluation_report_manifest",
        TABLE,
        ["workspace_id", "manifest_hash"],
    )
    op.execute(f'ALTER TABLE public."{TABLE}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE public."{TABLE}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY learning_evaluation_report_admin_select ON public."{TABLE}" '
        f"FOR SELECT TO {ADMIN} USING ({_WORKSPACE})"
    )
    op.execute(
        f'CREATE POLICY learning_evaluation_report_trainer_select ON public."{TABLE}" '
        f"FOR SELECT TO {TRAINER} USING ({_WORKSPACE})"
    )
    op.execute(
        f'CREATE POLICY learning_evaluation_report_trainer_insert ON public."{TABLE}" '
        f"FOR INSERT TO {TRAINER} WITH CHECK ({_WORKSPACE})"
    )
    op.execute(
        f"CREATE FUNCTION {IMMUTABLE_FUNCTION}() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "RAISE EXCEPTION 'WP06_LEARNING_EVALUATION_REPORT_IMMUTABLE'; "
        "END; $$"
    )
    op.execute(
        f"CREATE TRIGGER learning_evaluation_report_immutable BEFORE UPDATE OR DELETE "
        f'ON public."{TABLE}" FOR EACH ROW EXECUTE FUNCTION {IMMUTABLE_FUNCTION}()'
    )
    op.execute(
        f"REVOKE ALL PRIVILEGES ON TABLE public.{TABLE} FROM PUBLIC, crypidian_app, "
        "crypidian_trading_runtime, crypidian_ingestion_worker, crypidian_backtest_worker, "
        "crypidian_execution_gateway, crypidian_admin_web, crypidian_trainer_worker"
    )
    op.execute(f"GRANT SELECT ON TABLE public.{TABLE} TO {ADMIN}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{TABLE} TO {TRAINER}")


def downgrade() -> None:
    op.execute(
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM public.{TABLE}) THEN "
        "RAISE EXCEPTION 'WP06_LEARNING_EVALUATION_REPORT_DOWNGRADE_REQUIRES_EMPTY_TABLE'; "
        "END IF; END $$"
    )
    op.execute(f'DROP TRIGGER learning_evaluation_report_immutable ON public."{TABLE}"')
    op.execute(f"DROP FUNCTION {IMMUTABLE_FUNCTION}()")
    op.execute(f'DROP POLICY learning_evaluation_report_trainer_insert ON public."{TABLE}"')
    op.execute(f'DROP POLICY learning_evaluation_report_trainer_select ON public."{TABLE}"')
    op.execute(f'DROP POLICY learning_evaluation_report_admin_select ON public."{TABLE}"')
    op.drop_index("ix_learning_evaluation_report_manifest", table_name=TABLE)
    op.drop_index("ix_learning_evaluation_report_workspace_created", table_name=TABLE)
    op.drop_table(TABLE)
