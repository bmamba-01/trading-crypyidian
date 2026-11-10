"""Queue bền vững, lease generation và cancellation request cho trainer."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3f5a7c9e1b2"
down_revision: str | None = "d1e3f5a7c9b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
TRAINER = "crypidian_trainer_worker"
TABLE = "learning_training_job"
ATTEMPT = "learning_training_attempt"
EVENT = "learning_attempt_event"
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), server_default="queued", nullable=False),
        sa.Column("stage", sa.String(16), server_default="queued", nullable=False),
        sa.Column("progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.String(120), nullable=True),
        sa.Column("lease_generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_by", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="learning_training_job_status_valid",
        ),
        sa.CheckConstraint(
            "stage IN ('queued', 'training', 'complete', 'failed', 'cancelled')",
            name="learning_training_job_stage_valid",
        ),
        sa.CheckConstraint(
            "progress BETWEEN 0 AND 100", name="learning_training_job_progress_valid"
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 3",
            name="learning_training_job_attempt_count_valid",
        ),
        sa.CheckConstraint("lease_generation >= 0", name="learning_training_job_generation_valid"),
        sa.CheckConstraint(
            "(cancel_requested AND cancel_requested_at IS NOT NULL "
            "AND cancel_requested_by IS NOT NULL) "
            "OR (NOT cancel_requested AND cancel_requested_at IS NULL "
            "AND cancel_requested_by IS NULL)",
            name="learning_training_job_cancel_evidence_valid",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL) "
            "OR (status <> 'running' AND lease_owner IS NULL AND lease_until IS NULL)",
            name="learning_training_job_lease_consistent",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            [f"{ATTEMPT}.workspace_id", f"{ATTEMPT}.id"],
            name="fk_learning_training_job_attempt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "cancel_requested_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_learning_training_job_cancel_actor",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_training_job")),
        sa.UniqueConstraint("workspace_id", "attempt_id", name="uq_learning_training_job_attempt"),
    )
    op.create_index(
        "ix_learning_training_job_claim", TABLE, ["workspace_id", "status", "created_at"]
    )
    op.create_index(
        "ix_learning_training_job_workspace_created", TABLE, ["workspace_id", "created_at"]
    )
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY learning_training_job_admin_scope ON public.{TABLE} FOR ALL "
        f"TO {ADMIN} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY learning_training_job_trainer_scope ON public.{TABLE} FOR ALL "
        f"TO {TRAINER} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.{TABLE} FROM PUBLIC, {ADMIN}, {TRAINER}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{TABLE} TO {ADMIN}")
    op.execute(
        f"GRANT UPDATE (cancel_requested, cancel_requested_at, cancel_requested_by, updated_at) "
        f"ON TABLE public.{TABLE} TO {ADMIN}"
    )
    op.execute(f"GRANT SELECT ON TABLE public.{TABLE} TO {TRAINER}")
    op.execute(
        f"GRANT UPDATE (status, stage, progress, attempt_count, lease_owner, lease_generation, "
        f"lease_until, error_code, updated_at) ON TABLE public.{TABLE} TO {TRAINER}"
    )

    op.execute(f"""
        CREATE FUNCTION public.wp06_validate_learning_training_job()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            v_attempt_state text;
            v_latest_state text;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF current_user <> '{ADMIN}' OR NEW.status <> 'queued'
                    OR NEW.stage <> 'queued' OR NEW.progress <> 0
                    OR NEW.attempt_count <> 0 OR NEW.lease_generation <> 0
                    OR NEW.lease_owner IS NOT NULL OR NEW.lease_until IS NOT NULL
                    OR NEW.cancel_requested OR NEW.cancel_requested_at IS NOT NULL
                    OR NEW.cancel_requested_by IS NOT NULL OR NEW.error_code IS NOT NULL
                    OR NEW.workspace_id IS DISTINCT FROM
                        NULLIF(current_setting('app.workspace_id', true), '')::uuid THEN
                    RAISE EXCEPTION 'WP06_TRAINING_JOB_INSERT_DENIED';
                END IF;
                SELECT state INTO v_attempt_state FROM public.{EVENT}
                 WHERE workspace_id = NEW.workspace_id AND attempt_id = NEW.attempt_id
                 ORDER BY sequence DESC LIMIT 1;
                IF v_attempt_state IS DISTINCT FROM 'queued' THEN
                    RAISE EXCEPTION 'WP06_TRAINING_JOB_QUEUED_ATTEMPT_REQUIRED';
                END IF;
                RETURN NEW;
            END IF;

            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
                OR NEW.attempt_id IS DISTINCT FROM OLD.attempt_id THEN
                RAISE EXCEPTION 'WP06_TRAINING_JOB_SCOPE_IMMUTABLE';
            END IF;

            IF current_user = '{ADMIN}' THEN
                IF NEW.status IS DISTINCT FROM OLD.status
                    OR NEW.stage IS DISTINCT FROM OLD.stage
                    OR NEW.progress IS DISTINCT FROM OLD.progress
                    OR NEW.attempt_count IS DISTINCT FROM OLD.attempt_count
                    OR NEW.lease_owner IS DISTINCT FROM OLD.lease_owner
                    OR NEW.lease_generation IS DISTINCT FROM OLD.lease_generation
                    OR NEW.lease_until IS DISTINCT FROM OLD.lease_until
                    OR NEW.error_code IS DISTINCT FROM OLD.error_code
                    OR OLD.status NOT IN ('queued', 'running')
                    OR NEW.cancel_requested IS DISTINCT FROM TRUE
                    OR NEW.cancel_requested_at IS NULL
                    OR NEW.cancel_requested_by IS NULL THEN
                    RAISE EXCEPTION 'WP06_TRAINING_JOB_ADMIN_CANCEL_ONLY';
                END IF;
                IF OLD.cancel_requested AND (
                    NEW.cancel_requested IS DISTINCT FROM OLD.cancel_requested
                    OR NEW.cancel_requested_at IS DISTINCT FROM OLD.cancel_requested_at
                    OR NEW.cancel_requested_by IS DISTINCT FROM OLD.cancel_requested_by
                ) THEN
                    RAISE EXCEPTION 'WP06_TRAINING_JOB_CANCEL_EVIDENCE_IMMUTABLE';
                END IF;
                RETURN NEW;
            END IF;

            IF current_user <> '{TRAINER}' THEN
                RAISE EXCEPTION 'WP06_TRAINING_JOB_CALLER_DENIED';
            END IF;
            IF NEW.cancel_requested IS DISTINCT FROM OLD.cancel_requested
                OR NEW.cancel_requested_at IS DISTINCT FROM OLD.cancel_requested_at
                OR NEW.cancel_requested_by IS DISTINCT FROM OLD.cancel_requested_by THEN
                RAISE EXCEPTION 'WP06_TRAINING_JOB_CANCEL_ACTOR_IMMUTABLE';
            END IF;
            IF NEW.lease_generation < OLD.lease_generation
                OR NEW.lease_generation > OLD.lease_generation + 1
                OR NEW.attempt_count < OLD.attempt_count
                OR NEW.attempt_count > 3 THEN
                RAISE EXCEPTION 'WP06_TRAINING_JOB_GENERATION_INVALID';
            END IF;
            IF (NEW.lease_generation = OLD.lease_generation
                    AND NEW.attempt_count IS DISTINCT FROM OLD.attempt_count)
                OR (NEW.lease_generation = OLD.lease_generation + 1
                    AND (NEW.status IS DISTINCT FROM 'running'
                        OR NEW.attempt_count IS DISTINCT FROM OLD.attempt_count + 1)) THEN
                RAISE EXCEPTION 'WP06_TRAINING_JOB_CLAIM_COUNT_MISMATCH';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
                (OLD.status = 'queued' AND NEW.status IN ('running', 'cancelled', 'failed'))
                OR (OLD.status = 'running' AND NEW.status IN
                    ('queued', 'succeeded', 'failed', 'cancelled'))
            ) THEN
                RAISE EXCEPTION 'WP06_TRAINING_JOB_TRANSITION_INVALID';
            END IF;
            IF NEW.status = 'queued' THEN
                SELECT state INTO v_latest_state FROM public.{EVENT}
                 WHERE workspace_id = NEW.workspace_id AND attempt_id = NEW.attempt_id
                 ORDER BY sequence DESC LIMIT 1;
                IF v_latest_state IS DISTINCT FROM 'queued' THEN
                    RAISE EXCEPTION 'WP06_TRAINING_JOB_REQUEUE_AFTER_RECEIPT_DENIED';
                END IF;
            END IF;
            IF NEW.status IN ('succeeded', 'failed', 'cancelled') THEN
                SELECT state INTO v_latest_state FROM public.{EVENT}
                 WHERE workspace_id = NEW.workspace_id AND attempt_id = NEW.attempt_id
                 ORDER BY sequence DESC LIMIT 1;
                IF v_latest_state IS DISTINCT FROM NEW.status THEN
                    RAISE EXCEPTION 'WP06_TRAINING_JOB_TERMINAL_EVENT_REQUIRED';
                END IF;
                NEW.lease_owner := NULL;
                NEW.lease_until := NULL;
                NEW.progress := 100;
                NEW.stage := CASE NEW.status
                    WHEN 'succeeded' THEN 'complete'
                    WHEN 'failed' THEN 'failed'
                    ELSE 'cancelled'
                END;
            END IF;
            RETURN NEW;
        END
        $$""")
    op.execute(f"""
        CREATE FUNCTION public.wp06_training_job_attempt_state_matches()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            v_job public.{TABLE}%ROWTYPE;
            v_latest_state text;
            v_has_artifact boolean;
        BEGIN
            SELECT * INTO v_job FROM public.{TABLE}
             WHERE workspace_id = NEW.workspace_id AND id = NEW.id;
            IF NOT FOUND THEN RETURN NULL; END IF;
            SELECT state INTO v_latest_state FROM public.{EVENT}
             WHERE workspace_id = v_job.workspace_id AND attempt_id = v_job.attempt_id
             ORDER BY sequence DESC LIMIT 1;
            SELECT EXISTS (
                SELECT 1 FROM public.learning_training_artifact AS artifact
                 WHERE artifact.workspace_id = v_job.workspace_id
                   AND artifact.attempt_id = v_job.attempt_id
            ) INTO v_has_artifact;
            IF (v_job.status = 'queued' AND v_latest_state IS DISTINCT FROM 'queued')
                OR (v_job.status = 'running'
                    AND v_latest_state IS DISTINCT FROM 'queued'
                    AND v_latest_state IS DISTINCT FROM 'running')
                OR (v_job.status = 'succeeded' AND
                    (v_latest_state IS DISTINCT FROM 'succeeded' OR NOT v_has_artifact))
                OR (v_job.status = 'failed' AND v_latest_state IS DISTINCT FROM 'failed')
                OR (v_job.status = 'cancelled' AND v_latest_state IS DISTINCT FROM 'cancelled') THEN
                RAISE EXCEPTION 'WP06_TRAINING_JOB_ATTEMPT_STATE_MISMATCH';
            END IF;
            RETURN NULL;
        END
        $$""")
    op.execute(
        f"CREATE TRIGGER learning_training_job_validate BEFORE INSERT OR UPDATE "
        f"ON public.{TABLE} FOR EACH ROW EXECUTE FUNCTION "
        "public.wp06_validate_learning_training_job()"
    )
    op.execute(f"""
        CREATE CONSTRAINT TRIGGER learning_training_job_state_match
        AFTER INSERT OR UPDATE ON public.{TABLE} DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.wp06_training_job_attempt_state_matches()
    """)
    for function in (
        "wp06_validate_learning_training_job",
        "wp06_training_job_attempt_state_matches",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{function}() FROM PUBLIC")


def downgrade() -> None:
    op.execute(f"LOCK TABLE public.{TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.{TABLE}) THEN
            RAISE EXCEPTION 'WP06_TRAINING_JOB_DOWNGRADE_REQUIRES_EMPTY_TABLE';
        END IF;
    END $$""")
    op.execute(f"DROP TRIGGER learning_training_job_state_match ON public.{TABLE}")
    op.execute(f"DROP TRIGGER learning_training_job_validate ON public.{TABLE}")
    op.execute("DROP FUNCTION public.wp06_training_job_attempt_state_matches()")
    op.execute("DROP FUNCTION public.wp06_validate_learning_training_job()")
    op.execute(f"DROP POLICY learning_training_job_trainer_scope ON public.{TABLE}")
    op.execute(f"DROP POLICY learning_training_job_admin_scope ON public.{TABLE}")
    op.drop_index("ix_learning_training_job_workspace_created", table_name=TABLE)
    op.drop_index("ix_learning_training_job_claim", table_name=TABLE)
    op.drop_table(TABLE)
