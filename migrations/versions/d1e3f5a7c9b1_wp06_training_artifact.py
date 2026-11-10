"""Persist artifact trainer bất biến và bắt cặp attempt succeeded."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d1e3f5a7c9b1"
down_revision: str | None = "c3d5e7f9a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
TRAINER = "crypidian_trainer_worker"
TABLE = "learning_training_artifact"
EVENT = "learning_attempt_event"
ATTEMPT = "learning_training_attempt"
PROFILE = "learning_fold_resource_profile"
WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"


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
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("normalized_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_hash", sa.String(64), nullable=False),
        sa.Column("artifact_schema_version", sa.String(128), nullable=False),
        sa.Column("trainer_version", sa.String(64), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("fold_plan_hash", sa.String(64), nullable=False),
        sa.Column("fold_id", sa.String(64), nullable=False),
        sa.Column("approval_hash", sa.String(64), nullable=False),
        sa.Column("batch_hash", sa.String(64), nullable=False),
        sa.Column("feature_schema_hash", sa.String(64), nullable=False),
        sa.Column("attempt_content_hash", sa.String(64), nullable=False),
        sa.Column("canonical_artifact", sa.Text(), nullable=False),
        sa.Column(
            "artifact",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("elapsed_milliseconds", sa.Integer(), nullable=False),
        sa.Column("cpu_time_milliseconds", sa.Integer(), nullable=False),
        sa.Column("peak_memory_bytes", sa.Integer(), nullable=False),
        sa.Column("actual_operation_units", sa.Integer(), nullable=False),
        sa.Column("process_id", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "artifact_hash ~ '^[0-9a-f]{64}$' AND manifest_hash ~ '^[0-9a-f]{64}$' "
            "AND fold_plan_hash ~ '^[0-9a-f]{64}$' AND fold_id ~ '^[0-9a-f]{64}$' "
            "AND approval_hash ~ '^[0-9a-f]{64}$' AND batch_hash ~ '^[0-9a-f]{64}$' "
            "AND feature_schema_hash ~ '^[0-9a-f]{64}$' "
            "AND attempt_content_hash ~ '^[0-9a-f]{64}$'",
            name="learning_training_artifact_hashes_valid",
        ),
        sa.CheckConstraint(
            "elapsed_milliseconds >= 0 AND cpu_time_milliseconds >= 0 "
            "AND peak_memory_bytes > 0 AND actual_operation_units > 0 AND process_id > 0",
            name="learning_training_artifact_measurements_valid",
        ),
        sa.CheckConstraint(
            "length(artifact_schema_version) > 0 AND length(trainer_version) > 0 "
            "AND jsonb_typeof(artifact) = 'object' AND length(canonical_artifact) > 0",
            name="learning_training_artifact_payload_valid",
        ),
        sa.CheckConstraint(
            "COALESCE(artifact ->> 'scorer_state', '') = 'abstain' "
            "AND COALESCE(artifact ->> 'calibrated', '') = 'false' "
            "AND NOT (artifact ? 'samples') AND NOT (artifact ? 'artifact_hash')",
            name="learning_training_artifact_diagnostic_only",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            [f"{ATTEMPT}.workspace_id", f"{ATTEMPT}.id"],
            name="fk_learning_training_artifact_attempt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "normalized_dataset_id", "fold_id"],
            [
                f"{PROFILE}.workspace_id",
                f"{PROFILE}.normalized_dataset_id",
                f"{PROFILE}.fold_id",
            ],
            name="fk_learning_training_artifact_profile",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_training_artifact")),
        sa.UniqueConstraint(
            "workspace_id",
            "attempt_id",
            name="uq_learning_training_artifact_attempt",
        ),
    )
    op.create_index(
        "ix_learning_training_artifact_workspace_created",
        TABLE,
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_learning_training_artifact_manifest", TABLE, ["workspace_id", "manifest_hash"]
    )

    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY learning_training_artifact_admin_select ON public.{TABLE} "
        f"FOR SELECT TO {ADMIN} USING ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY learning_training_artifact_trainer_scope ON public.{TABLE} "
        f"FOR SELECT TO {TRAINER} USING ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY learning_training_artifact_trainer_insert ON public.{TABLE} "
        f"FOR INSERT TO {TRAINER} WITH CHECK ({WORKSPACE})"
    )
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.{TABLE} FROM PUBLIC, {ADMIN}, {TRAINER}")
    op.execute(f"GRANT SELECT ON TABLE public.{TABLE} TO {ADMIN}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{TABLE} TO {TRAINER}")

    op.execute(f"""
        CREATE FUNCTION public.wp06_validate_learning_training_artifact()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            v_attempt public.{ATTEMPT}%ROWTYPE;
            v_latest_state text;
            v_profile_schema text;
            v_payload jsonb;
        BEGIN
            IF current_user <> '{TRAINER}' THEN
                RAISE EXCEPTION 'WP06_TRAINING_ARTIFACT_CALLER_DENIED';
            END IF;
            v_payload := NEW.canonical_artifact::jsonb;
            IF v_payload <> NEW.artifact
                OR pg_catalog.encode(pg_catalog.sha256(
                    pg_catalog.convert_to(NEW.canonical_artifact, 'UTF8')
                ), 'hex') <> NEW.artifact_hash
                OR v_payload ? 'samples'
                OR v_payload ? 'artifact_hash'
                OR v_payload->>'scorer_state' IS DISTINCT FROM 'abstain'
                OR v_payload->>'calibrated' IS DISTINCT FROM 'false'
                OR v_payload->>'schema_version' IS DISTINCT FROM
                    'crypidian.learning.cpu-logistic.v1'
                OR NEW.artifact_schema_version IS DISTINCT FROM
                    'crypidian.learning.cpu-logistic.v1'
                OR v_payload->>'trainer_version' IS DISTINCT FROM 'cpu-logistic-v1'
                OR NEW.trainer_version IS DISTINCT FROM 'cpu-logistic-v1'
                OR v_payload->>'workspace_id' IS DISTINCT FROM NEW.workspace_id::text
                OR v_payload->>'attempt_id' IS DISTINCT FROM NEW.attempt_id::text
                OR v_payload->>'attempt_content_hash' IS DISTINCT FROM NEW.attempt_content_hash
                OR v_payload->>'manifest_hash' IS DISTINCT FROM NEW.manifest_hash
                OR v_payload->>'fold_plan_hash' IS DISTINCT FROM NEW.fold_plan_hash
                OR v_payload->>'fold_id' IS DISTINCT FROM NEW.fold_id
                OR v_payload->>'approval_hash' IS DISTINCT FROM NEW.approval_hash
                OR v_payload->>'batch_hash' IS DISTINCT FROM NEW.batch_hash
                OR v_payload->>'feature_schema_hash' IS DISTINCT FROM NEW.feature_schema_hash
                OR v_payload->>'actual_operation_units' IS DISTINCT FROM
                    NEW.actual_operation_units::text THEN
                RAISE EXCEPTION 'WP06_TRAINING_ARTIFACT_CANONICAL_INVALID';
            END IF;

            SELECT * INTO v_attempt FROM public.{ATTEMPT}
             WHERE workspace_id = NEW.workspace_id AND id = NEW.attempt_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_TRAINING_ARTIFACT_ATTEMPT_MISSING';
            END IF;
            IF v_attempt.content_hash IS DISTINCT FROM NEW.attempt_content_hash
                OR v_attempt.normalized_dataset_id IS DISTINCT FROM
                    (v_payload->>'normalized_dataset_id')::uuid
                OR v_attempt.normalized_dataset_id IS DISTINCT FROM NEW.normalized_dataset_id
                OR v_attempt.manifest_hash IS DISTINCT FROM NEW.manifest_hash
                OR v_attempt.fold_plan_hash IS DISTINCT FROM NEW.fold_plan_hash
                OR v_attempt.fold_id IS DISTINCT FROM NEW.fold_id
                OR v_attempt.approval_hash IS DISTINCT FROM NEW.approval_hash
                OR v_attempt.trainer_version IS DISTINCT FROM NEW.trainer_version
                OR v_attempt.dependency_fingerprint IS DISTINCT FROM
                    v_payload->>'dependency_fingerprint'
                OR v_attempt.resource_plan->>'estimated_operation_units' IS DISTINCT FROM
                    NEW.actual_operation_units::text THEN
                RAISE EXCEPTION 'WP06_TRAINING_ARTIFACT_ATTEMPT_MISMATCH';
            END IF;
            SELECT state INTO v_latest_state FROM public.{EVENT}
             WHERE workspace_id = NEW.workspace_id AND attempt_id = NEW.attempt_id
             ORDER BY sequence DESC LIMIT 1;
            IF v_latest_state IS DISTINCT FROM 'running' THEN
                RAISE EXCEPTION 'WP06_TRAINING_ARTIFACT_ATTEMPT_NOT_RUNNING';
            END IF;
            SELECT feature_schema_hash INTO v_profile_schema FROM public.{PROFILE}
             WHERE workspace_id = NEW.workspace_id
               AND normalized_dataset_id = v_attempt.normalized_dataset_id
               AND fold_id = NEW.fold_id;
            IF v_profile_schema IS DISTINCT FROM NEW.feature_schema_hash
                OR v_payload->>'feature_schema_hash' IS DISTINCT FROM v_profile_schema THEN
                RAISE EXCEPTION 'WP06_TRAINING_ARTIFACT_SCHEMA_MISMATCH';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.wp06_training_artifact_requires_success_event()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.{EVENT} AS event
                 WHERE event.workspace_id = NEW.workspace_id
                   AND event.attempt_id = NEW.attempt_id
                   AND event.state = 'succeeded'
                   AND event.elapsed_milliseconds = NEW.elapsed_milliseconds
                   AND event.peak_memory_bytes = NEW.peak_memory_bytes
                   AND event.actual_operation_units = NEW.actual_operation_units
            ) THEN
                RAISE EXCEPTION 'WP06_TRAINING_ARTIFACT_SUCCESS_EVENT_REQUIRED';
            END IF;
            RETURN NULL;
        END
        $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.wp06_training_success_requires_artifact()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF NEW.state = 'succeeded' AND NOT EXISTS (
                SELECT 1 FROM public.{TABLE} AS artifact
                 WHERE artifact.workspace_id = NEW.workspace_id
                   AND artifact.attempt_id = NEW.attempt_id
                   AND artifact.elapsed_milliseconds = NEW.elapsed_milliseconds
                   AND artifact.peak_memory_bytes = NEW.peak_memory_bytes
                   AND artifact.actual_operation_units = NEW.actual_operation_units
            ) THEN
                RAISE EXCEPTION 'WP06_TRAINING_SUCCESS_ARTIFACT_REQUIRED';
            END IF;
            RETURN NULL;
        END
        $$
    """)
    op.execute(
        f"CREATE TRIGGER learning_training_artifact_validate BEFORE INSERT ON public.{TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_validate_learning_training_artifact()"
    )
    op.execute(
        f"CREATE TRIGGER learning_training_artifact_immutable BEFORE UPDATE OR DELETE "
        f"ON public.{TABLE} FOR EACH ROW EXECUTE FUNCTION "
        "public.wp06_learning_attempt_immutable()"
    )
    op.execute(f"""
        CREATE CONSTRAINT TRIGGER learning_training_artifact_success_event
        AFTER INSERT ON public.{TABLE} DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.wp06_training_artifact_requires_success_event()
    """)
    op.execute(f"""
        CREATE CONSTRAINT TRIGGER learning_training_success_artifact
        AFTER INSERT ON public.{EVENT} DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.wp06_training_success_requires_artifact()
    """)
    for function in (
        "wp06_validate_learning_training_artifact",
        "wp06_training_artifact_requires_success_event",
        "wp06_training_success_requires_artifact",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{function}() FROM PUBLIC")


def downgrade() -> None:
    op.execute(f"LOCK TABLE public.{TABLE}, public.{EVENT} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.{TABLE}) THEN
            RAISE EXCEPTION 'WP06_TRAINING_ARTIFACT_DOWNGRADE_REQUIRES_EMPTY_TABLE';
        END IF;
    END $$""")
    op.execute(f"DROP TRIGGER learning_training_success_artifact ON public.{EVENT}")
    op.execute(f"DROP TRIGGER learning_training_artifact_success_event ON public.{TABLE}")
    op.execute(f"DROP TRIGGER learning_training_artifact_immutable ON public.{TABLE}")
    op.execute(f"DROP TRIGGER learning_training_artifact_validate ON public.{TABLE}")
    op.execute("DROP FUNCTION public.wp06_training_success_requires_artifact()")
    op.execute("DROP FUNCTION public.wp06_training_artifact_requires_success_event()")
    op.execute("DROP FUNCTION public.wp06_validate_learning_training_artifact()")
    op.execute(f"DROP POLICY learning_training_artifact_trainer_insert ON public.{TABLE}")
    op.execute(f"DROP POLICY learning_training_artifact_trainer_scope ON public.{TABLE}")
    op.execute(f"DROP POLICY learning_training_artifact_admin_select ON public.{TABLE}")
    op.drop_index("ix_learning_training_artifact_manifest", table_name=TABLE)
    op.drop_index("ix_learning_training_artifact_workspace_created", table_name=TABLE)
    op.drop_table(TABLE)
