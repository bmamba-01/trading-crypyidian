"""WP06 Task 4: hồ sơ tài nguyên theo fold và sổ ghi lần chạy training."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a0c2e4f6b8d0"
down_revision: str | None = "f8b0d2e4a6c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
TRAINER = "crypidian_trainer_worker"
EVALUATOR = "crypidian_learning_evaluator"
BUILDER = "crypidian_candidate_builder"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
PROFILE = "learning_fold_resource_profile"
ATTEMPT = "learning_training_attempt"
EVENT = "learning_attempt_event"


def _require_roles() -> None:
    op.execute(f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='{ADMIN}')
            OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='{TRAINER}')
            OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='{EVALUATOR}')
            OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='{BUILDER}') THEN
            RAISE EXCEPTION 'WP06_ATTEMPT_LEDGER_PROCESS_ROLE_MISSING';
        END IF;
    END $$""")


def upgrade() -> None:
    _require_roles()
    op.create_table(
        PROFILE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("normalized_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("fold_plan_hash", sa.String(64), nullable=False),
        sa.Column("fold_id", sa.String(64), nullable=False),
        sa.Column("training_samples", sa.Integer(), nullable=False),
        sa.Column("validation_samples", sa.Integer(), nullable=False),
        sa.Column("feature_count", sa.Integer(), nullable=False),
        sa.Column("feature_schema_hash", sa.String(64), nullable=False),
        sa.Column("profile_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("fold_id ~ '^[0-9a-f]{64}$'", name="learning_fold_profile_fold_hash"),
        sa.CheckConstraint(
            "feature_schema_hash ~ '^[0-9a-f]{64}$' AND profile_hash ~ '^[0-9a-f]{64}$'",
            name="learning_fold_profile_hashes_valid",
        ),
        sa.CheckConstraint(
            "training_samples > 0 AND validation_samples > 0 AND feature_count > 0",
            name="learning_fold_profile_counts_positive",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "manifest_hash", "fold_plan_hash", "normalized_dataset_id"],
            [
                "learning_normalized_dataset.workspace_id",
                "learning_normalized_dataset.manifest_hash",
                "learning_normalized_dataset.fold_plan_hash",
                "learning_normalized_dataset.id",
            ],
            name="fk_learning_fold_resource_profile_dataset",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_fold_resource_profile")),
        sa.UniqueConstraint(
            "workspace_id",
            "normalized_dataset_id",
            "fold_id",
            name="uq_learning_fold_resource_profile_fold",
        ),
    )
    op.create_index(
        "ix_learning_fold_resource_profile_dataset",
        PROFILE,
        ["workspace_id", "manifest_hash", "fold_plan_hash", "fold_id"],
    )

    op.create_table(
        ATTEMPT,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("normalized_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("actor_membership_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("split_policy_version", sa.String(128), nullable=False),
        sa.Column("fold_plan_hash", sa.String(64), nullable=False),
        sa.Column("approval_hash", sa.String(64), nullable=False),
        sa.Column("parameter_family_hash", sa.String(64), nullable=False),
        sa.Column("trial_index", sa.Integer(), nullable=False),
        sa.Column("trial_count", sa.Integer(), nullable=False),
        sa.Column("fold_id", sa.String(64), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("trainer_version", sa.String(64), nullable=False),
        sa.Column("dependency_fingerprint", sa.String(64), nullable=False),
        sa.Column("resource_policy_version", sa.String(64), nullable=False),
        sa.Column("resource_request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("resource_plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("rejection_reason_code", sa.String(40), nullable=True),
        sa.Column("initial_state", sa.String(16), nullable=False),
        sa.Column("canonical_request", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND approval_hash ~ '^[0-9a-f]{64}$' AND parameter_family_hash ~ '^[0-9a-f]{64}$' "
            "AND fold_id ~ '^[0-9a-f]{64}$' AND dependency_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND content_hash ~ '^[0-9a-f]{64}$'",
            name="learning_training_attempt_hashes_valid",
        ),
        sa.CheckConstraint(
            "trial_count BETWEEN 1 AND 100 AND trial_index >= 0 AND trial_index < trial_count "
            "AND seed >= 0",
            name="learning_training_attempt_trial_bounds",
        ),
        sa.CheckConstraint(
            "length(btrim(split_policy_version)) BETWEEN 1 AND 128 "
            "AND length(btrim(trainer_version)) BETWEEN 1 AND 64 "
            "AND length(btrim(resource_policy_version)) BETWEEN 1 AND 64",
            name="learning_training_attempt_versions_present",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(resource_request) = 'object' "
            "AND (resource_plan IS NULL OR jsonb_typeof(resource_plan) = 'object') "
            "AND length(canonical_request) > 0 "
            "AND initial_state IN ('queued', 'rejected') "
            "AND ((resource_plan IS NULL AND initial_state = 'rejected' "
            "AND rejection_reason_code = 'resource_limit') OR "
            "(resource_plan IS NOT NULL AND initial_state = 'queued' "
            "AND rejection_reason_code IS NULL))",
            name="learning_training_attempt_payload_valid",
        ),
        sa.CheckConstraint(
            "rejection_reason_code IS NULL OR rejection_reason_code IN "
            "('resource_limit', 'approval_missing', 'rights_denied', 'invalid_request', "
            "'adapter_error', 'cancelled_by_operator')",
            name="learning_training_attempt_reason_valid",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "actor_membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_learning_training_attempt_actor_membership",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "manifest_hash", "fold_plan_hash", "normalized_dataset_id"],
            [
                "learning_normalized_dataset.workspace_id",
                "learning_normalized_dataset.manifest_hash",
                "learning_normalized_dataset.fold_plan_hash",
                "learning_normalized_dataset.id",
            ],
            name="fk_learning_training_attempt_dataset",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_training_attempt")),
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_training_attempt_idempotency"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "parameter_family_hash",
            "trial_index",
            "fold_id",
            name="uq_learning_training_attempt_trial_fold",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_learning_training_attempt_workspace"),
    )
    op.create_index(
        "ix_learning_training_attempt_workspace_created",
        ATTEMPT,
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_learning_training_attempt_queue",
        ATTEMPT,
        ["workspace_id", "created_at"],
        postgresql_where=sa.text("initial_state = 'queued'"),
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
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_principal", sa.String(80), nullable=False),
        sa.Column("reason_code", sa.String(40), nullable=True),
        sa.Column("elapsed_milliseconds", sa.Integer(), nullable=True),
        sa.Column("peak_memory_bytes", sa.Integer(), nullable=True),
        sa.Column("actual_operation_units", sa.Integer(), nullable=True),
        sa.Column("canonical_event", sa.Text(), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("sequence > 0", name="learning_attempt_event_sequence_positive"),
        sa.CheckConstraint(
            "state IN ('queued', 'rejected', 'running', 'succeeded', 'failed', 'cancelled')",
            name="learning_attempt_event_state_valid",
        ),
        sa.CheckConstraint(
            "actor_principal IN ('crypidian_admin_web', 'crypidian_trainer_worker', "
            "'crypidian_learning_evaluator')",
            name="learning_attempt_event_actor_valid",
        ),
        sa.CheckConstraint(
            "reason_code IS NULL OR reason_code IN ('resource_limit', 'approval_missing', "
            "'rights_denied', 'invalid_request', 'adapter_error', 'cancelled_by_operator')",
            name="learning_attempt_event_reason_valid",
        ),
        sa.CheckConstraint(
            "((state IN ('rejected', 'failed', 'cancelled')) = (reason_code IS NOT NULL)) "
            "AND ((state IN ('succeeded', 'failed', 'cancelled')) = "
            "(elapsed_milliseconds IS NOT NULL))",
            name="learning_attempt_event_terminal_fields_valid",
        ),
        sa.CheckConstraint(
            "(elapsed_milliseconds IS NULL OR elapsed_milliseconds >= 0) "
            "AND (peak_memory_bytes IS NULL OR peak_memory_bytes >= 0) "
            "AND (actual_operation_units IS NULL OR actual_operation_units >= 0) "
            "AND canonical_event <> '' AND event_hash ~ '^[0-9a-f]{64}$'",
            name="learning_attempt_event_metrics_valid",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["learning_training_attempt.workspace_id", "learning_training_attempt.id"],
            name="fk_learning_attempt_event_attempt",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_attempt_event")),
        sa.UniqueConstraint(
            "workspace_id", "attempt_id", "sequence", name="uq_learning_attempt_event_sequence"
        ),
    )
    op.create_index(
        "ix_learning_attempt_event_attempt", EVENT, ["workspace_id", "attempt_id", "sequence"]
    )

    op.execute("""
        CREATE FUNCTION public.wp06_validate_learning_fold_resource_profile()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE v_train integer; v_validation integer; v_schema_count integer;
                v_key_set_count integer; v_feature_count integer;
                v_schema_hash text;
        BEGIN
            WITH per_sample AS (
                SELECT s.sample_id, s.feature_schema_hash, a.role,
                       keys.feature_keys
                FROM public.learning_fold_assignment a
                JOIN public.learning_normalized_sample s
                  ON s.workspace_id = a.workspace_id
                 AND s.manifest_hash = a.manifest_hash
                 AND s.fold_plan_hash = a.fold_plan_hash
                 AND s.sample_id = a.sample_id
                CROSS JOIN LATERAL (
                    SELECT array_agg(k.key ORDER BY k.key) AS feature_keys
                    FROM jsonb_object_keys(s.features) AS k(key)
                ) keys
                WHERE a.workspace_id = NEW.workspace_id
                  AND a.manifest_hash = NEW.manifest_hash
                  AND a.fold_plan_hash = NEW.fold_plan_hash
                  AND a.fold_id = NEW.fold_id
                  AND a.role IN ('train', 'validation')
            )
            SELECT count(*) FILTER (WHERE role = 'train')::integer,
                   count(*) FILTER (WHERE role = 'validation')::integer,
                   count(DISTINCT feature_schema_hash)::integer,
                   count(DISTINCT feature_keys)::integer,
                   min(cardinality(feature_keys))::integer
              INTO v_train, v_validation, v_schema_count, v_key_set_count, v_feature_count
              FROM per_sample;
            IF v_train IS DISTINCT FROM NEW.training_samples
                OR v_validation IS DISTINCT FROM NEW.validation_samples
                OR v_schema_count <> 1 OR v_key_set_count <> 1
                OR v_feature_count IS DISTINCT FROM NEW.feature_count THEN
                RAISE EXCEPTION 'WP06_LEARNING_RESOURCE_PROFILE_NOT_DERIVED_FROM_ROWS';
            END IF;
            SELECT s.feature_schema_hash INTO STRICT v_schema_hash
              FROM public.learning_normalized_sample s
              JOIN public.learning_fold_assignment a
                ON a.workspace_id = s.workspace_id AND a.manifest_hash = s.manifest_hash
               AND a.fold_plan_hash = s.fold_plan_hash AND a.sample_id = s.sample_id
             WHERE a.workspace_id = NEW.workspace_id AND a.manifest_hash = NEW.manifest_hash
               AND a.fold_plan_hash = NEW.fold_plan_hash AND a.fold_id = NEW.fold_id
               AND a.role = 'train'
             ORDER BY a.sample_id LIMIT 1;
            IF NEW.feature_schema_hash <> v_schema_hash THEN
                RAISE EXCEPTION 'WP06_LEARNING_RESOURCE_PROFILE_SCHEMA_MISMATCH';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.wp06_validate_learning_training_attempt()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE v_approval_hash text; v_train integer; v_validation integer; v_features integer;
                v_steps integer; v_runtime integer; v_memory bigint; v_samples bigint;
                v_ops bigint; v_estimated_memory bigint; v_rejected boolean;
                v_request_fields integer; v_plan_fields integer;
        BEGIN
            IF current_user <> '{ADMIN}' THEN
                RAISE EXCEPTION 'WP06_LEARNING_ATTEMPT_ADMIN_INSERT_ONLY';
            END IF;
            SELECT approval_hash INTO v_approval_hash
              FROM public.learning_split_approval
             WHERE workspace_id = NEW.workspace_id AND manifest_hash = NEW.manifest_hash
               AND split_policy_version = NEW.split_policy_version
               AND fold_plan_hash = NEW.fold_plan_hash;
            IF v_approval_hash IS NULL OR v_approval_hash <> NEW.approval_hash THEN
                RAISE EXCEPTION 'WP06_LEARNING_ATTEMPT_APPROVAL_MISMATCH';
            END IF;
            SELECT training_samples, validation_samples, feature_count
              INTO v_train, v_validation, v_features
              FROM public.{PROFILE}
             WHERE workspace_id = NEW.workspace_id
               AND normalized_dataset_id = NEW.normalized_dataset_id
               AND manifest_hash = NEW.manifest_hash
               AND fold_plan_hash = NEW.fold_plan_hash AND fold_id = NEW.fold_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_LEARNING_ATTEMPT_RESOURCE_PROFILE_MISSING';
            END IF;
            IF jsonb_typeof(NEW.resource_request) <> 'object'
                OR EXISTS (
                    SELECT 1 FROM jsonb_object_keys(NEW.resource_request) AS key(name)
                    WHERE name NOT IN ('training_samples', 'validation_samples', 'feature_count',
                        'max_steps', 'max_runtime_seconds', 'memory_limit_bytes')
                )
                OR jsonb_typeof(NEW.resource_request->'training_samples') <> 'number'
                OR jsonb_typeof(NEW.resource_request->'validation_samples') <> 'number'
                OR jsonb_typeof(NEW.resource_request->'feature_count') <> 'number'
                OR jsonb_typeof(NEW.resource_request->'max_steps') <> 'number'
                OR jsonb_typeof(NEW.resource_request->'max_runtime_seconds') <> 'number'
                OR jsonb_typeof(NEW.resource_request->'memory_limit_bytes') <> 'number'
                OR (NEW.resource_request->>'training_samples') !~ '^[0-9]+$'
                OR (NEW.resource_request->>'validation_samples') !~ '^[0-9]+$'
                OR (NEW.resource_request->>'feature_count') !~ '^[0-9]+$'
                OR (NEW.resource_request->>'max_steps') !~ '^[0-9]+$'
                OR (NEW.resource_request->>'max_runtime_seconds') !~ '^[0-9]+$'
                OR (NEW.resource_request->>'memory_limit_bytes') !~ '^[0-9]+$' THEN
                RAISE EXCEPTION 'WP06_LEARNING_ATTEMPT_RESOURCE_REQUEST_INVALID';
            END IF;
            SELECT count(*)::integer INTO v_request_fields
              FROM jsonb_object_keys(NEW.resource_request);
            IF v_request_fields <> 6 THEN
                RAISE EXCEPTION 'WP06_LEARNING_ATTEMPT_RESOURCE_REQUEST_INVALID';
            END IF;
            v_train := (NEW.resource_request->>'training_samples')::integer;
            v_validation := (NEW.resource_request->>'validation_samples')::integer;
            v_features := (NEW.resource_request->>'feature_count')::integer;
            v_steps := (NEW.resource_request->>'max_steps')::integer;
            v_runtime := (NEW.resource_request->>'max_runtime_seconds')::integer;
            v_memory := (NEW.resource_request->>'memory_limit_bytes')::bigint;
            SELECT training_samples, validation_samples, feature_count
              INTO STRICT v_train, v_validation, v_features
              FROM public.{PROFILE}
             WHERE workspace_id = NEW.workspace_id
               AND normalized_dataset_id = NEW.normalized_dataset_id
               AND manifest_hash = NEW.manifest_hash
               AND fold_plan_hash = NEW.fold_plan_hash AND fold_id = NEW.fold_id;
            IF (NEW.resource_request->>'training_samples')::integer <> v_train
                OR (NEW.resource_request->>'validation_samples')::integer <> v_validation
                OR (NEW.resource_request->>'feature_count')::integer <> v_features THEN
                RAISE EXCEPTION 'WP06_LEARNING_ATTEMPT_COUNTS_NOT_AUTHORITATIVE';
            END IF;
            v_samples := v_train::bigint + v_validation::bigint;
            v_ops := v_samples * v_features::bigint * v_steps::bigint * 3;
            v_estimated_memory := v_samples * v_features::bigint * 64
                + v_samples * 128 + v_features::bigint * 1024 + 65536;
            v_rejected := v_samples > 10000 OR v_features > 256 OR v_steps <= 0 OR v_steps > 2000
                OR v_runtime <= 0 OR v_runtime > 300 OR v_memory <= 0 OR v_memory > 536870912
                OR v_ops > 2000000000
                OR v_estimated_memory > v_memory;
            IF NEW.initial_state = 'queued' THEN
                IF v_rejected OR jsonb_typeof(NEW.resource_plan) <> 'object'
                    OR EXISTS (
                        SELECT 1 FROM jsonb_object_keys(NEW.resource_plan) AS key(name)
                        WHERE name NOT IN ('sample_count', 'feature_count', 'max_steps',
                            'max_runtime_seconds', 'memory_limit_bytes',
                            'estimated_operation_units', 'estimated_memory_bytes')
                    )
                    OR (NEW.resource_plan->>'sample_count')::bigint <> v_samples
                    OR (NEW.resource_plan->>'feature_count')::integer <> v_features
                    OR (NEW.resource_plan->>'max_steps')::integer <> v_steps
                    OR (NEW.resource_plan->>'max_runtime_seconds')::integer <> v_runtime
                    OR (NEW.resource_plan->>'memory_limit_bytes')::bigint <> v_memory
                    OR (NEW.resource_plan->>'estimated_operation_units')::bigint <> v_ops
                    OR (NEW.resource_plan->>'estimated_memory_bytes')::bigint
                        <> v_estimated_memory THEN
                    RAISE EXCEPTION 'WP06_LEARNING_ATTEMPT_RESOURCE_PLAN_INVALID';
                END IF;
                SELECT count(*)::integer INTO v_plan_fields
                  FROM jsonb_object_keys(NEW.resource_plan);
                IF v_plan_fields <> 7 THEN
                    RAISE EXCEPTION 'WP06_LEARNING_ATTEMPT_RESOURCE_PLAN_INVALID';
                END IF;
            ELSIF NOT v_rejected THEN
                RAISE EXCEPTION 'WP06_LEARNING_ATTEMPT_RESOURCE_REJECTION_INVALID';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.wp06_validate_learning_attempt_event()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE v_initial text; v_previous_state text; v_previous_sequence integer;
                v_previous_at timestamptz;
        BEGIN
            IF NEW.actor_principal <> current_user THEN
                RAISE EXCEPTION 'WP06_LEARNING_ATTEMPT_ACTOR_MISMATCH';
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended(NEW.attempt_id::text, 0));
            SELECT initial_state INTO v_initial FROM public.{ATTEMPT}
             WHERE workspace_id = NEW.workspace_id AND id = NEW.attempt_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'WP06_LEARNING_ATTEMPT_MISSING'; END IF;
            SELECT state, sequence, occurred_at
              INTO v_previous_state, v_previous_sequence, v_previous_at
              FROM public.{EVENT}
             WHERE workspace_id = NEW.workspace_id AND attempt_id = NEW.attempt_id
             ORDER BY sequence DESC LIMIT 1;
            IF NOT FOUND THEN
                IF NEW.sequence <> 1 OR NEW.state <> v_initial
                    OR current_user <> '{ADMIN}' THEN
                    RAISE EXCEPTION 'WP06_LEARNING_ATTEMPT_INITIAL_EVENT_INVALID';
                END IF;
            ELSE
                IF current_user <> '{TRAINER}' OR NEW.sequence <> v_previous_sequence + 1
                    OR NEW.occurred_at < v_previous_at THEN
                    RAISE EXCEPTION 'WP06_LEARNING_ATTEMPT_EVENT_SEQUENCE_INVALID';
                END IF;
                IF NOT (
                    (
                        v_previous_state = 'queued'
                        AND NEW.state IN ('running', 'failed', 'cancelled')
                    )
                    OR (
                        v_previous_state = 'running'
                        AND NEW.state IN ('succeeded', 'failed', 'cancelled')
                    )
                ) THEN
                    RAISE EXCEPTION 'WP06_LEARNING_ATTEMPT_TRANSITION_INVALID';
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.wp06_learning_attempt_immutable()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN RAISE EXCEPTION 'WP06_LEARNING_ATTEMPT_APPEND_ONLY'; END $$
    """)
    op.execute(f"""
        CREATE FUNCTION public.wp06_learning_attempt_initial_event_required()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE v_state text;
        BEGIN
            SELECT state INTO v_state FROM public.{EVENT}
             WHERE workspace_id = NEW.workspace_id AND attempt_id = NEW.id AND sequence = 1;
            IF v_state IS DISTINCT FROM NEW.initial_state THEN
                RAISE EXCEPTION 'WP06_LEARNING_ATTEMPT_INITIAL_EVENT_REQUIRED';
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute(
        f"CREATE TRIGGER learning_fold_resource_profile_validate BEFORE INSERT ON public.{PROFILE} "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_validate_learning_fold_resource_profile()"
    )
    for table in (PROFILE, ATTEMPT, EVENT):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON public.{table} "
            "FOR EACH ROW EXECUTE FUNCTION public.wp06_learning_attempt_immutable()"
        )
    op.execute(
        f"CREATE TRIGGER learning_training_attempt_validate BEFORE INSERT ON public.{ATTEMPT} "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_validate_learning_training_attempt()"
    )
    op.execute(
        f"CREATE TRIGGER learning_attempt_event_validate BEFORE INSERT ON public.{EVENT} "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_validate_learning_attempt_event()"
    )
    op.execute(f"""
        CREATE CONSTRAINT TRIGGER learning_attempt_initial_event_required
        AFTER INSERT ON public.{ATTEMPT} DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.wp06_learning_attempt_initial_event_required()
    """)

    for table in (PROFILE, ATTEMPT, EVENT):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"REVOKE ALL PRIVILEGES ON TABLE public.{table} FROM PUBLIC, {ADMIN}, "
            f"{TRAINER}, {EVALUATOR}, {BUILDER}"
        )
    op.execute(
        f"CREATE POLICY learning_profile_builder_scope ON public.{PROFILE} FOR ALL "
        f"TO {BUILDER} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY learning_profile_admin_read ON public.{PROFILE} FOR SELECT "
        f"TO {ADMIN} USING ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY learning_profile_trainer_read ON public.{PROFILE} FOR SELECT "
        f"TO {TRAINER} USING ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY learning_attempt_admin_scope ON public.{ATTEMPT} FOR ALL "
        f"TO {ADMIN} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY learning_attempt_trainer_read ON public.{ATTEMPT} FOR SELECT "
        f"TO {TRAINER} USING ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY learning_attempt_evaluator_read ON public.{ATTEMPT} FOR SELECT "
        f"TO {EVALUATOR} USING ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY learning_attempt_event_admin_scope ON public.{EVENT} FOR ALL "
        f"TO {ADMIN} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY learning_attempt_event_trainer_scope ON public.{EVENT} FOR ALL "
        f"TO {TRAINER} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY learning_attempt_event_evaluator_read ON public.{EVENT} FOR SELECT "
        f"TO {EVALUATOR} USING ({WORKSPACE})"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{PROFILE} TO {BUILDER}")
    op.execute(f"GRANT SELECT ON TABLE public.{PROFILE} TO {ADMIN}, {TRAINER}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{ATTEMPT} TO {ADMIN}")
    op.execute(f"GRANT SELECT ON TABLE public.{ATTEMPT} TO {TRAINER}, {EVALUATOR}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{EVENT} TO {ADMIN}, {TRAINER}")
    op.execute(f"GRANT SELECT ON TABLE public.{EVENT} TO {EVALUATOR}")
    for function in (
        "wp06_validate_learning_fold_resource_profile",
        "wp06_validate_learning_training_attempt",
        "wp06_validate_learning_attempt_event",
        "wp06_learning_attempt_immutable",
        "wp06_learning_attempt_initial_event_required",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{function}() FROM PUBLIC")


def downgrade() -> None:
    op.execute(
        f"LOCK TABLE public.{EVENT}, public.{ATTEMPT}, public.{PROFILE} IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(f"""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.{EVENT}) OR EXISTS (SELECT 1 FROM public.{ATTEMPT})
            OR EXISTS (SELECT 1 FROM public.{PROFILE}) THEN
            RAISE EXCEPTION 'WP06_ATTEMPT_LEDGER_DOWNGRADE_REQUIRES_EMPTY_TABLES';
        END IF;
    END $$""")
    op.execute(f"DROP TRIGGER learning_attempt_initial_event_required ON public.{ATTEMPT}")
    op.execute(f"DROP TRIGGER learning_attempt_event_validate ON public.{EVENT}")
    op.execute(f"DROP TRIGGER learning_training_attempt_validate ON public.{ATTEMPT}")
    op.execute(f"DROP TRIGGER learning_fold_resource_profile_validate ON public.{PROFILE}")
    for table in (EVENT, ATTEMPT, PROFILE):
        op.execute(f"DROP TRIGGER {table}_immutable ON public.{table}")
    for policy in (
        "learning_attempt_event_evaluator_read",
        "learning_attempt_event_trainer_scope",
        "learning_attempt_event_admin_scope",
    ):
        op.execute(f"DROP POLICY {policy} ON public.{EVENT}")
    for policy in (
        "learning_attempt_evaluator_read",
        "learning_attempt_trainer_read",
        "learning_attempt_admin_scope",
    ):
        op.execute(f"DROP POLICY {policy} ON public.{ATTEMPT}")
    for policy in (
        "learning_profile_trainer_read",
        "learning_profile_admin_read",
        "learning_profile_builder_scope",
    ):
        op.execute(f"DROP POLICY {policy} ON public.{PROFILE}")
    for table in (EVENT, ATTEMPT, PROFILE):
        op.execute(f"ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_learning_attempt_event_attempt", table_name=EVENT)
    op.drop_index("ix_learning_training_attempt_queue", table_name=ATTEMPT)
    op.drop_index("ix_learning_training_attempt_workspace_created", table_name=ATTEMPT)
    op.drop_index("ix_learning_fold_resource_profile_dataset", table_name=PROFILE)
    op.drop_table(EVENT)
    op.drop_table(ATTEMPT)
    op.drop_table(PROFILE)
    for function in (
        "wp06_learning_attempt_initial_event_required",
        "wp06_learning_attempt_immutable",
        "wp06_validate_learning_attempt_event",
        "wp06_validate_learning_training_attempt",
        "wp06_validate_learning_fold_resource_profile",
    ):
        op.execute(f"DROP FUNCTION public.{function}()")
