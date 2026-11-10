"""WP06 Task 6: registry artifact, event audit và active pointer PAPER CAS."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1a2c4e6f8b0"
down_revision: str | None = "d3f5a7c9e1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
APP = "crypidian_app"
TRAINER = "crypidian_trainer_worker"
REGISTRY = "learning_model_registry"
EVENT = "learning_model_registry_event"
POINTER = "learning_model_active_pointer"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        REGISTRY,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_hash", sa.String(64), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("fold_plan_hash", sa.String(64), nullable=False),
        sa.Column("approval_hash", sa.String(64), nullable=False),
        sa.Column("evaluation_report_id", sa.Uuid(), nullable=True),
        sa.Column("evaluation_report_hash", sa.String(64), nullable=True),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by_membership_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "state IN ('draft', 'evaluated', 'rejected', 'paper_active', 'retired')",
            name="learning_model_registry_state_valid",
        ),
        sa.CheckConstraint(
            "artifact_hash ~ '^[0-9a-f]{64}$' AND manifest_hash ~ '^[0-9a-f]{64}$' "
            "AND fold_plan_hash ~ '^[0-9a-f]{64}$' AND approval_hash ~ '^[0-9a-f]{64}$'",
            name="learning_model_registry_hashes_valid",
        ),
        sa.CheckConstraint(
            "(evaluation_report_id IS NULL AND evaluation_report_hash IS NULL) OR "
            "(evaluation_report_id IS NOT NULL AND "
            "evaluation_report_hash ~ '^[0-9a-f]{64}$')",
            name="learning_model_registry_evaluation_pair_valid",
        ),
        sa.CheckConstraint("revision >= 1", name="learning_model_registry_revision_positive"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["learning_training_artifact.id"],
            name="fk_learning_model_registry_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by_membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_learning_model_registry_creator",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "evaluation_report_id"],
            ["learning_evaluation_report.workspace_id", "learning_evaluation_report.id"],
            name="fk_learning_model_registry_evaluation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_model_registry")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_learning_model_registry_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "artifact_id", name="uq_learning_model_registry_artifact"
        ),
    )
    op.create_index(
        "ix_learning_model_registry_workspace_state",
        REGISTRY,
        ["workspace_id", "state", "created_at"],
    )

    op.create_table(
        EVENT,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("registry_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("from_state", sa.String(24), nullable=True),
        sa.Column("to_state", sa.String(24), nullable=False),
        sa.Column("actor_membership_id", sa.Uuid(), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("mfa_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expected_revision", sa.Integer(), nullable=True),
        sa.Column("expected_pointer_version", sa.Integer(), nullable=True),
        sa.Column("evaluation_report_id", sa.Uuid(), nullable=True),
        sa.Column("evaluation_report_hash", sa.String(64), nullable=True),
        sa.CheckConstraint("sequence >= 1", name="learning_model_registry_event_sequence_positive"),
        sa.CheckConstraint(
            "action IN ('register', 'evaluate', 'approve', 'reject', 'activate', "
            "'rollback', 'retire')",
            name="learning_model_registry_event_action_valid",
        ),
        sa.CheckConstraint(
            "(from_state IS NULL OR from_state IN "
            "('draft', 'evaluated', 'rejected', 'paper_active', 'retired')) AND "
            "to_state IN ('draft', 'evaluated', 'rejected', 'paper_active', 'retired')",
            name="learning_model_registry_event_state_valid",
        ),
        sa.CheckConstraint(
            "length(btrim(policy_version)) BETWEEN 1 AND 128 AND "
            "length(btrim(reason_code)) BETWEEN 1 AND 64 AND "
            "length(btrim(reason)) BETWEEN 12 AND 500",
            name="learning_model_registry_event_audit_valid",
        ),
        sa.CheckConstraint(
            "(action IN ('approve', 'reject', 'activate', 'rollback', 'retire') AND "
            "mfa_verified_at IS NOT NULL) OR "
            "(action IN ('register', 'evaluate') AND mfa_verified_at IS NULL)",
            name="learning_model_registry_event_mfa_required",
        ),
        sa.CheckConstraint(
            "(evaluation_report_id IS NULL AND evaluation_report_hash IS NULL) OR "
            "(evaluation_report_id IS NOT NULL AND "
            "evaluation_report_hash ~ '^[0-9a-f]{64}$')",
            name="learning_model_registry_event_evaluation_pair_valid",
        ),
        sa.CheckConstraint(
            "(expected_revision IS NULL OR expected_revision >= 1) AND "
            "(expected_pointer_version IS NULL OR expected_pointer_version >= 0)",
            name="learning_model_registry_event_expected_versions_valid",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "registry_id"],
            ["learning_model_registry.workspace_id", "learning_model_registry.id"],
            name="fk_learning_model_registry_event_entry",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "actor_membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_learning_model_registry_event_actor",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_model_registry_event")),
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_model_registry_event_key"
        ),
        sa.UniqueConstraint(
            "workspace_id", "registry_id", "sequence", name="uq_learning_model_registry_event_seq"
        ),
    )
    op.create_index(
        "ix_learning_model_registry_event_workspace_time", EVENT, ["workspace_id", "created_at"]
    )

    op.create_table(
        POINTER,
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("registry_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_by_membership_id", sa.Uuid(), nullable=True),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version >= 0", name="learning_model_active_pointer_version_nonnegative"
        ),
        sa.CheckConstraint(
            "(version = 0 AND registry_id IS NULL AND updated_by_membership_id IS NULL) OR "
            "(version > 0 AND registry_id IS NOT NULL AND updated_by_membership_id IS NOT NULL)",
            name="learning_model_active_pointer_initial_state_valid",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "registry_id"],
            ["learning_model_registry.workspace_id", "learning_model_registry.id"],
            name="fk_learning_model_active_pointer_registry",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by_membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_learning_model_active_pointer_actor",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", name=op.f("pk_learning_model_active_pointer")),
    )

    for table in (REGISTRY, EVENT, POINTER):
        op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE public."{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY {table}_admin_scope ON public."{table}" '
            f"FOR ALL TO {ADMIN} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
        )

    op.execute("""
        CREATE FUNCTION public.wp06_learning_model_registry_entry_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        DECLARE artifact_row record;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_IMMUTABLE';
          END IF;
          IF TG_OP = 'UPDATE' THEN
            IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
              OR NEW.artifact_id IS DISTINCT FROM OLD.artifact_id
              OR NEW.artifact_hash IS DISTINCT FROM OLD.artifact_hash
              OR NEW.manifest_hash IS DISTINCT FROM OLD.manifest_hash
              OR NEW.fold_plan_hash IS DISTINCT FROM OLD.fold_plan_hash
              OR NEW.approval_hash IS DISTINCT FROM OLD.approval_hash
              OR NEW.created_by_membership_id IS DISTINCT FROM OLD.created_by_membership_id
              OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
              RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_LINEAGE_IMMUTABLE';
            END IF;
            IF NEW.revision <> OLD.revision + 1 THEN
              RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_REVISION_INVALID';
            END IF;
            IF NOT EXISTS (
              SELECT 1 FROM public.learning_model_registry_event e
              WHERE e.workspace_id = OLD.workspace_id AND e.registry_id = OLD.id
                AND e.sequence = NEW.revision AND e.expected_revision = OLD.revision
                AND e.from_state = OLD.state AND e.to_state = NEW.state
            ) THEN
              RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_TRANSITION_WITHOUT_EVENT';
            END IF;
            RETURN NEW;
          END IF;

          IF NEW.state <> 'draft' OR NEW.revision <> 1
            OR NEW.evaluation_report_id IS NOT NULL
            OR NEW.evaluation_report_hash IS NOT NULL THEN
            RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_MUST_START_DRAFT';
          END IF;
          SELECT * INTO artifact_row FROM public.learning_training_artifact a
          WHERE a.id = NEW.artifact_id AND a.workspace_id = NEW.workspace_id;
          IF NOT FOUND OR artifact_row.artifact_hash IS DISTINCT FROM NEW.artifact_hash
            OR artifact_row.manifest_hash IS DISTINCT FROM NEW.manifest_hash
            OR artifact_row.fold_plan_hash IS DISTINCT FROM NEW.fold_plan_hash
            OR artifact_row.approval_hash IS DISTINCT FROM NEW.approval_hash
            OR artifact_row.artifact->>'scorer_state' IS DISTINCT FROM 'abstain'
            OR artifact_row.artifact->>'calibrated' IS DISTINCT FROM 'false' THEN
            RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_ARTIFACT_MISMATCH';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM public.user_membership m
            WHERE m.workspace_id = NEW.workspace_id AND m.id = NEW.created_by_membership_id
              AND m.status = 'ACTIVE'
          ) THEN
            RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_CREATOR_INVALID';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.wp06_learning_model_registry_event_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        DECLARE entry public.learning_model_registry%ROWTYPE;
        DECLARE report_row public.learning_evaluation_report%ROWTYPE;
        DECLARE active_pointer public.learning_model_active_pointer%ROWTYPE;
        DECLARE mfa_now timestamptz := clock_timestamp();
        BEGIN
          SELECT * INTO entry FROM public.learning_model_registry
          WHERE workspace_id = NEW.workspace_id AND id = NEW.registry_id FOR UPDATE;
          IF NOT FOUND THEN RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_NOT_FOUND'; END IF;
          IF NOT EXISTS (
            SELECT 1 FROM public.user_membership m
            WHERE m.workspace_id = NEW.workspace_id AND m.id = NEW.actor_membership_id
              AND m.status = 'ACTIVE'
          ) THEN RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_ACTOR_INVALID'; END IF;

          IF NEW.action = 'register' THEN
            IF NEW.sequence <> 1 OR entry.revision <> 1 OR entry.state <> 'draft'
              OR NEW.from_state IS NOT NULL OR NEW.to_state <> 'draft'
              OR NEW.expected_revision IS NOT NULL THEN
              RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_REGISTER_INVALID';
            END IF;
            RETURN NEW;
          END IF;
          IF NEW.expected_revision IS DISTINCT FROM entry.revision
            OR NEW.sequence <> entry.revision + 1
            OR NEW.from_state IS DISTINCT FROM entry.state THEN
            RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_STALE_REVISION';
          END IF;

          IF NEW.action IN ('approve', 'reject', 'activate', 'rollback', 'retire')
            AND (NEW.mfa_verified_at IS NULL OR NEW.mfa_verified_at > mfa_now
              OR NEW.mfa_verified_at < mfa_now - interval '15 minutes') THEN
            RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_MFA_REQUIRED';
          END IF;

          IF NEW.action = 'evaluate' THEN
            SELECT * INTO report_row FROM public.learning_evaluation_report r
            WHERE r.workspace_id = NEW.workspace_id AND r.id = NEW.evaluation_report_id;
            IF entry.state <> 'draft' OR NOT FOUND
              OR report_row.report_hash IS DISTINCT FROM NEW.evaluation_report_hash
              OR report_row.manifest_hash IS DISTINCT FROM entry.manifest_hash
              OR report_row.fold_plan_hash IS DISTINCT FROM entry.fold_plan_hash
              OR report_row.evaluation_mode <> 'diagnostic'
              OR report_row.candidate_completeness_verified IS NOT FALSE
              OR NEW.to_state <> 'evaluated' THEN
              RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_EVALUATION_MISMATCH';
            END IF;
          ELSIF NEW.action = 'approve' THEN
            IF entry.state <> 'evaluated' OR NEW.to_state <> 'evaluated'
              OR NOT EXISTS (
                SELECT 1 FROM public.learning_model_registry_event e
                WHERE e.workspace_id = entry.workspace_id AND e.registry_id = entry.id
                  AND e.action = 'evaluate'
                  AND e.evaluation_report_hash = entry.evaluation_report_hash
              ) THEN
              RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_APPROVAL_INVALID';
            END IF;
          ELSIF NEW.action = 'reject' THEN
            IF entry.state NOT IN ('draft', 'evaluated') OR NEW.to_state <> 'rejected' THEN
              RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_REJECTION_INVALID';
            END IF;
          ELSIF NEW.action = 'activate' THEN
            IF entry.state <> 'evaluated' OR NEW.to_state <> 'paper_active'
              OR NOT EXISTS (
                SELECT 1 FROM public.learning_model_registry_event e
                WHERE e.workspace_id = entry.workspace_id AND e.registry_id = entry.id
                  AND e.action = 'approve' AND e.to_state = 'evaluated'
              ) THEN
              RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_ACTIVATION_INVALID';
            END IF;
          ELSIF NEW.action = 'rollback' THEN
            IF entry.state <> 'retired' OR NEW.to_state <> 'paper_active'
              OR NOT EXISTS (
                SELECT 1 FROM public.learning_model_registry_event e
                WHERE e.workspace_id = entry.workspace_id AND e.registry_id = entry.id
                  AND e.action IN ('activate', 'rollback') AND e.to_state = 'paper_active'
              )
              OR NOT EXISTS (
                SELECT 1 FROM public.learning_model_registry_event e
                WHERE e.workspace_id = entry.workspace_id AND e.registry_id = entry.id
                  AND e.action = 'approve'
              ) THEN
              RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_ROLLBACK_INVALID';
            END IF;
          ELSIF NEW.action = 'retire' THEN
            IF entry.state = 'retired' OR NEW.to_state <> 'retired' THEN
              RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_RETIRE_INVALID';
            END IF;
            SELECT * INTO active_pointer FROM public.learning_model_active_pointer p
            WHERE p.workspace_id = NEW.workspace_id AND p.registry_id = NEW.registry_id;
            IF FOUND AND NEW.expected_pointer_version IS DISTINCT FROM active_pointer.version THEN
              RAISE EXCEPTION 'WP06_LEARNING_MODEL_ACTIVE_RETIRE_REQUIRES_POINTER_CAS';
            ELSIF NOT FOUND AND NEW.expected_pointer_version IS NOT NULL THEN
              RAISE EXCEPTION 'WP06_LEARNING_MODEL_INACTIVE_RETIRE_POINTER_VERSION_INVALID';
            END IF;
          ELSE
            RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_ACTION_INVALID';
          END IF;

          RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.wp06_learning_model_registry_event_apply()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        BEGIN
          IF NEW.action <> 'register' THEN
            UPDATE public.learning_model_registry
            SET state = NEW.to_state,
                revision = NEW.sequence,
                evaluation_report_id = CASE WHEN NEW.action = 'evaluate'
                  THEN NEW.evaluation_report_id ELSE evaluation_report_id END,
                evaluation_report_hash = CASE WHEN NEW.action = 'evaluate'
                  THEN NEW.evaluation_report_hash ELSE evaluation_report_hash END
            WHERE workspace_id = NEW.workspace_id AND id = NEW.registry_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_NOT_FOUND'; END IF;
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.wp06_learning_model_registry_event_immutable()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_EVENT_IMMUTABLE'; END $$
    """)
    op.execute("""
        CREATE FUNCTION public.wp06_learning_model_registry_initial_event_check()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM public.learning_model_registry_event e
            WHERE e.workspace_id = NEW.workspace_id AND e.registry_id = NEW.id
              AND e.sequence = 1 AND e.action = 'register'
              AND e.from_state IS NULL AND e.to_state = 'draft'
          ) THEN RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_INITIAL_EVENT_REQUIRED'; END IF;
          RETURN NULL;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.wp06_learning_model_pointer_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        DECLARE pointer_event record;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'WP06_LEARNING_MODEL_ACTIVE_POINTER_IMMUTABLE';
          END IF;
          IF TG_OP = 'INSERT' THEN
            IF NEW.version <> 0 OR NEW.registry_id IS NOT NULL
              OR NEW.updated_by_membership_id IS NOT NULL THEN
              RAISE EXCEPTION 'WP06_LEARNING_MODEL_ACTIVE_POINTER_INITIAL_STATE_INVALID';
            END IF;
            RETURN NEW;
          END IF;
          IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
            OR NEW.version <> OLD.version + 1
            OR NEW.updated_by_membership_id IS NULL THEN
            RAISE EXCEPTION 'WP06_LEARNING_MODEL_ACTIVE_POINTER_CAS_INVALID';
          END IF;
          IF NEW.registry_id IS NOT NULL THEN
            SELECT * INTO pointer_event FROM public.learning_model_registry_event e
            WHERE e.workspace_id = NEW.workspace_id AND e.registry_id = NEW.registry_id
              AND e.action IN ('activate', 'rollback')
              AND e.expected_pointer_version = OLD.version
              AND e.actor_membership_id = NEW.updated_by_membership_id
            ORDER BY e.sequence DESC LIMIT 1;
            IF NOT FOUND OR NOT EXISTS (
              SELECT 1 FROM public.learning_model_registry r
              WHERE r.workspace_id = NEW.workspace_id AND r.id = NEW.registry_id
                AND r.state = 'paper_active'
            ) THEN
              RAISE EXCEPTION 'WP06_LEARNING_MODEL_ACTIVE_POINTER_EVENT_REQUIRED';
            END IF;
          END IF;
          IF OLD.registry_id IS NOT NULL AND OLD.registry_id IS DISTINCT FROM NEW.registry_id THEN
            SELECT * INTO pointer_event FROM public.learning_model_registry_event e
            WHERE e.workspace_id = OLD.workspace_id AND e.registry_id = OLD.registry_id
              AND e.action = 'retire' AND e.expected_pointer_version = OLD.version
              AND e.actor_membership_id = NEW.updated_by_membership_id
            ORDER BY e.sequence DESC LIMIT 1;
            IF NOT FOUND OR NOT EXISTS (
              SELECT 1 FROM public.learning_model_registry r
              WHERE r.workspace_id = OLD.workspace_id AND r.id = OLD.registry_id
                AND r.state = 'retired'
            ) THEN
              RAISE EXCEPTION 'WP06_LEARNING_MODEL_ACTIVE_POINTER_RETIRE_EVENT_REQUIRED';
            END IF;
          END IF;
          RETURN NEW;
        END $$
    """)

    op.execute(
        f"CREATE TRIGGER learning_model_registry_entry_guard BEFORE INSERT OR UPDATE OR DELETE "
        f"ON public.{REGISTRY} FOR EACH ROW EXECUTE FUNCTION "
        "public.wp06_learning_model_registry_entry_guard()"
    )
    op.execute(
        f"CREATE TRIGGER learning_model_registry_event_guard BEFORE INSERT ON public.{EVENT} "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_learning_model_registry_event_guard()"
    )
    op.execute(
        f"CREATE TRIGGER learning_model_registry_event_apply AFTER INSERT ON public.{EVENT} "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_learning_model_registry_event_apply()"
    )
    op.execute(
        f"CREATE TRIGGER learning_model_registry_event_immutable BEFORE UPDATE OR DELETE "
        f"ON public.{EVENT} FOR EACH ROW EXECUTE FUNCTION "
        "public.wp06_learning_model_registry_event_immutable()"
    )
    op.execute(
        f"CREATE CONSTRAINT TRIGGER learning_model_registry_initial_event "
        f"AFTER INSERT ON public.{REGISTRY} DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_learning_model_registry_initial_event_check()"
    )
    op.execute(
        f"CREATE TRIGGER learning_model_active_pointer_guard BEFORE INSERT OR UPDATE OR DELETE "
        f"ON public.{POINTER} FOR EACH ROW EXECUTE FUNCTION "
        "public.wp06_learning_model_pointer_guard()"
    )

    for function in (
        "wp06_learning_model_registry_entry_guard",
        "wp06_learning_model_registry_event_guard",
        "wp06_learning_model_registry_event_apply",
        "wp06_learning_model_registry_event_immutable",
        "wp06_learning_model_registry_initial_event_check",
        "wp06_learning_model_pointer_guard",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{function}() FROM PUBLIC")

    for table in (REGISTRY, EVENT, POINTER):
        op.execute(
            f"REVOKE ALL PRIVILEGES ON TABLE public.{table} FROM PUBLIC, {APP}, {ADMIN}, {TRAINER}"
        )
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{REGISTRY} TO {ADMIN}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{EVENT} TO {ADMIN}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE public.{POINTER} TO {ADMIN}")


def downgrade() -> None:
    for table in (REGISTRY, EVENT, POINTER):
        op.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM public.{table}) THEN "
            "RAISE EXCEPTION 'WP06_MODEL_REGISTRY_DOWNGRADE_REQUIRES_EMPTY_TABLE'; "
            "END IF; END $$"
        )
    op.execute(f"DROP TRIGGER learning_model_active_pointer_guard ON public.{POINTER}")
    op.execute(f"DROP TRIGGER learning_model_registry_initial_event ON public.{REGISTRY}")
    op.execute(f"DROP TRIGGER learning_model_registry_event_immutable ON public.{EVENT}")
    op.execute(f"DROP TRIGGER learning_model_registry_event_apply ON public.{EVENT}")
    op.execute(f"DROP TRIGGER learning_model_registry_event_guard ON public.{EVENT}")
    op.execute(f"DROP TRIGGER learning_model_registry_entry_guard ON public.{REGISTRY}")
    for function in (
        "wp06_learning_model_pointer_guard",
        "wp06_learning_model_registry_initial_event_check",
        "wp06_learning_model_registry_event_immutable",
        "wp06_learning_model_registry_event_apply",
        "wp06_learning_model_registry_event_guard",
        "wp06_learning_model_registry_entry_guard",
    ):
        op.execute(f"DROP FUNCTION public.{function}()")
    for table in (REGISTRY, EVENT, POINTER):
        op.execute(f"DROP POLICY {table}_admin_scope ON public.{table}")
        op.execute(f"ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.{table} FROM {ADMIN}")
    op.drop_table(POINTER)
    op.drop_index("ix_learning_model_registry_event_workspace_time", table_name=EVENT)
    op.drop_table(EVENT)
    op.drop_index("ix_learning_model_registry_workspace_state", table_name=REGISTRY)
    op.drop_table(REGISTRY)
