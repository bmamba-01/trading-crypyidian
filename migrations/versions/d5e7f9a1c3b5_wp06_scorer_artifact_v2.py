"""WP06: v2 calibration artifact and metadata-only trainer lineage gateway."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d5e7f9a1c3b5"
down_revision: str | None = "c4e6a8b0d2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "learning_scorer_artifact"
TRAINER = "crypidian_trainer_worker"
ADMIN = "crypidian_admin_web"
EVALUATOR = "crypidian_learning_evaluator"
READER = "crypidian_learning_batch_reader"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
LINEAGE_FUNCTION = "public.wp06_claim_learning_training_lineage"
LINEAGE_FUNCTION_SIGNATURE = f"{LINEAGE_FUNCTION}(uuid, uuid)"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("base_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("normalized_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("fold_plan_hash", sa.String(64), nullable=False),
        sa.Column("fold_id", sa.String(64), nullable=False),
        sa.Column("approval_hash", sa.String(64), nullable=False),
        sa.Column("base_artifact_hash", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(128), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=True),
        sa.Column("strategy_release_id", sa.String(80), nullable=True),
        sa.Column("strategy_release_content_hash", sa.String(64), nullable=True),
        sa.Column("vector_schema_version", sa.String(128), nullable=True),
        sa.Column("vector_schema_hash", sa.String(64), nullable=False),
        sa.Column("training_membership_digest", sa.String(64), nullable=False),
        sa.Column("validation_membership_digest", sa.String(64), nullable=False),
        sa.Column("training_sample_count", sa.Integer(), nullable=False),
        sa.Column("validation_sample_count", sa.Integer(), nullable=False),
        sa.Column("calibrator_method", sa.String(64), nullable=False),
        sa.Column("calibrator_version", sa.String(80), nullable=False),
        sa.Column("calibration_policy_version", sa.String(120), nullable=True),
        sa.Column("calibration_policy_hash", sa.String(64), nullable=True),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column(
            "validation_class_support", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("validation_nll_before", sa.Float(), nullable=True),
        sa.Column("validation_nll_after", sa.Float(), nullable=True),
        sa.Column("calibration_iterations", sa.Integer(), nullable=True),
        sa.Column("trainer_version", sa.String(64), nullable=False),
        sa.Column("trainer_code_version", sa.String(80), nullable=False),
        sa.Column("dependency_fingerprint", sa.String(64), nullable=False),
        sa.Column("artifact_hash", sa.String(64), nullable=False),
        sa.Column("canonical_artifact", sa.Text(), nullable=False),
        sa.Column("artifact", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'crypidian.learning.scorer-temperature.v2'",
            name="learning_scorer_artifact_schema_valid",
        ),
        sa.CheckConstraint(
            "state IN ('ready_for_evaluation', 'abstain')",
            name="learning_scorer_artifact_state_valid",
        ),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND fold_id ~ '^[0-9a-f]{64}$' AND approval_hash ~ '^[0-9a-f]{64}$' "
            "AND base_artifact_hash ~ '^[0-9a-f]{64}$' AND vector_schema_hash ~ '^[0-9a-f]{64}$' "
            "AND training_membership_digest ~ '^[0-9a-f]{64}$' "
            "AND validation_membership_digest ~ '^[0-9a-f]{64}$' "
            "AND dependency_fingerprint ~ '^[0-9a-f]{64}$' AND artifact_hash ~ '^[0-9a-f]{64}$' "
            "AND (strategy_release_content_hash IS NULL OR "
            "strategy_release_content_hash ~ '^[0-9a-f]{64}$') "
            "AND (calibration_policy_hash IS NULL OR calibration_policy_hash ~ '^[0-9a-f]{64}$')",
            name="learning_scorer_artifact_hashes_valid",
        ),
        sa.CheckConstraint(
            "training_sample_count > 0 AND validation_sample_count > 0 "
            "AND jsonb_typeof(validation_class_support) = 'array'",
            name="learning_scorer_artifact_counts_valid",
        ),
        sa.CheckConstraint(
            "(state = 'ready_for_evaluation' AND reason_code IS NULL AND temperature > 0 "
            "AND strategy_release_id IS NOT NULL AND strategy_release_content_hash IS NOT NULL "
            "AND vector_schema_version IS NOT NULL AND calibration_policy_version IS NOT NULL "
            "AND calibration_policy_hash IS NOT NULL AND validation_nll_before IS NOT NULL "
            "AND validation_nll_after IS NOT NULL AND calibration_iterations > 0) OR "
            "(state = 'abstain' AND reason_code IS NOT NULL AND temperature IS NULL)",
            name="learning_scorer_artifact_readiness_valid",
        ),
        sa.CheckConstraint(
            "calibrator_method = 'temperature_scaling' AND length(calibrator_version) > 0 "
            "AND length(trainer_version) > 0 AND length(trainer_code_version) > 0 "
            "AND jsonb_typeof(artifact) = 'object' AND length(canonical_artifact) > 0",
            name="learning_scorer_artifact_payload_valid",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["learning_training_attempt.workspace_id", "learning_training_attempt.id"],
            name="fk_learning_scorer_artifact_attempt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["learning_training_artifact.workspace_id", "learning_training_artifact.attempt_id"],
            name="fk_learning_scorer_artifact_base_attempt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "normalized_dataset_id", "fold_id"],
            [
                "learning_fold_resource_profile.workspace_id",
                "learning_fold_resource_profile.normalized_dataset_id",
                "learning_fold_resource_profile.fold_id",
            ],
            name="fk_learning_scorer_artifact_profile",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_scorer_artifact")),
        sa.UniqueConstraint(
            "workspace_id", "attempt_id", name="uq_learning_scorer_artifact_attempt"
        ),
    )
    op.create_index(
        "ix_learning_scorer_artifact_workspace_created",
        TABLE,
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_learning_scorer_artifact_manifest_fold",
        TABLE,
        ["workspace_id", "manifest_hash", "fold_plan_hash", "fold_id"],
    )
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"REVOKE ALL PRIVILEGES ON TABLE public.{TABLE} FROM PUBLIC, crypidian_app, "
        f"crypidian_trading_runtime, crypidian_execution_gateway, {ADMIN}, {TRAINER}, {EVALUATOR}"
    )
    op.execute(
        f"CREATE POLICY learning_scorer_artifact_trainer_read ON public.{TABLE} "
        f"FOR SELECT TO {TRAINER} USING ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY learning_scorer_artifact_trainer_insert ON public.{TABLE} "
        f"FOR INSERT TO {TRAINER} WITH CHECK ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY learning_scorer_artifact_admin_read ON public.{TABLE} "
        f"FOR SELECT TO {ADMIN} USING ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY learning_scorer_artifact_evaluator_read ON public.{TABLE} "
        f"FOR SELECT TO {EVALUATOR} USING ({WORKSPACE})"
    )
    op.execute(f"GRANT SELECT ON TABLE public.{TABLE} TO {ADMIN}, {EVALUATOR}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{TABLE} TO {TRAINER}")
    op.execute("""
        CREATE FUNCTION public.reject_learning_scorer_artifact_mutation()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN RAISE EXCEPTION 'WP06_SCORER_ARTIFACT_APPEND_ONLY'; END $$
    """)
    op.execute(
        f"CREATE TRIGGER {TABLE}_immutable BEFORE UPDATE OR DELETE ON public.{TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION public.reject_learning_scorer_artifact_mutation()"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.reject_learning_scorer_artifact_mutation() FROM PUBLIC"
    )
    op.execute(f"""
        CREATE FUNCTION public.wp06_validate_learning_scorer_artifact()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            v_payload jsonb;
            v_base public.learning_training_artifact%ROWTYPE;
            v_attempt public.learning_training_attempt%ROWTYPE;
        BEGIN
            IF current_user <> '{TRAINER}' THEN
                RAISE EXCEPTION 'WP06_SCORER_ARTIFACT_CALLER_DENIED';
            END IF;
            v_payload := NEW.canonical_artifact::jsonb;
            IF (SELECT count(*) FROM jsonb_object_keys(v_payload)) <> 31
                OR NOT (v_payload ?& ARRAY[
                    'schema_version','state','reason_code','workspace_id','attempt_id',
                    'normalized_dataset_id','manifest_hash','fold_plan_hash','fold_id',
                    'approval_hash','base_artifact_hash','strategy_release_id',
                    'strategy_release_content_hash','vector_schema_version','vector_schema_hash',
                    'training_membership_digest','validation_membership_digest',
                    'training_sample_count','validation_sample_count','calibrator_method',
                    'calibrator_version','calibration_policy_version','calibration_policy_hash',
                    'temperature','validation_class_support','validation_nll_before',
                    'validation_nll_after','calibration_iterations','trainer_version',
                    'trainer_code_version','dependency_fingerprint'
                ]::text[])
                OR v_payload <> NEW.artifact
                OR pg_catalog.encode(pg_catalog.sha256(
                    pg_catalog.convert_to(NEW.canonical_artifact, 'UTF8')
                ), 'hex') <> NEW.artifact_hash
                OR v_payload ? 'samples' OR v_payload ? 'labels'
                OR v_payload ? 'features' OR v_payload ? 'test_payload'
                OR v_payload ? 'artifact_hash'
                OR v_payload->>'schema_version' IS DISTINCT FROM
                    'crypidian.learning.scorer-temperature.v2'
                OR v_payload->>'base_artifact_hash' IS DISTINCT FROM NEW.base_artifact_hash
                OR v_payload->>'workspace_id' IS DISTINCT FROM NEW.workspace_id::text
                OR v_payload->>'attempt_id' IS DISTINCT FROM NEW.attempt_id::text
                OR v_payload->>'normalized_dataset_id' IS DISTINCT FROM
                    NEW.normalized_dataset_id::text
                OR v_payload->>'manifest_hash' IS DISTINCT FROM NEW.manifest_hash
                OR v_payload->>'fold_plan_hash' IS DISTINCT FROM NEW.fold_plan_hash
                OR v_payload->>'fold_id' IS DISTINCT FROM NEW.fold_id
                OR v_payload->>'approval_hash' IS DISTINCT FROM NEW.approval_hash
                OR v_payload->>'state' IS DISTINCT FROM NEW.state
                OR v_payload->>'reason_code' IS DISTINCT FROM NEW.reason_code
                OR v_payload->>'strategy_release_id' IS DISTINCT FROM NEW.strategy_release_id
                OR v_payload->>'strategy_release_content_hash' IS DISTINCT FROM
                    NEW.strategy_release_content_hash
                OR v_payload->>'vector_schema_version' IS DISTINCT FROM
                    NEW.vector_schema_version
                OR v_payload->>'vector_schema_hash' IS DISTINCT FROM NEW.vector_schema_hash
                OR v_payload->>'training_membership_digest' IS DISTINCT FROM
                    NEW.training_membership_digest
                OR v_payload->>'validation_membership_digest' IS DISTINCT FROM
                    NEW.validation_membership_digest
                OR (v_payload->>'training_sample_count')::integer IS DISTINCT FROM
                    NEW.training_sample_count
                OR (v_payload->>'validation_sample_count')::integer IS DISTINCT FROM
                    NEW.validation_sample_count
                OR v_payload->>'calibrator_method' IS DISTINCT FROM NEW.calibrator_method
                OR v_payload->>'calibrator_version' IS DISTINCT FROM NEW.calibrator_version
                OR v_payload->>'calibration_policy_version' IS DISTINCT FROM
                    NEW.calibration_policy_version
                OR v_payload->>'calibration_policy_hash' IS DISTINCT FROM
                    NEW.calibration_policy_hash
                OR (v_payload->>'temperature')::double precision IS DISTINCT FROM NEW.temperature
                OR v_payload->'validation_class_support' IS DISTINCT FROM
                    to_jsonb(NEW.validation_class_support)
                OR (v_payload->>'validation_nll_before')::double precision IS DISTINCT FROM
                    NEW.validation_nll_before
                OR (v_payload->>'validation_nll_after')::double precision IS DISTINCT FROM
                    NEW.validation_nll_after
                OR (v_payload->>'calibration_iterations')::integer IS DISTINCT FROM
                    NEW.calibration_iterations
                OR v_payload->>'trainer_version' IS DISTINCT FROM NEW.trainer_version
                OR v_payload->>'trainer_code_version' IS DISTINCT FROM NEW.trainer_code_version
                OR v_payload->>'dependency_fingerprint' IS DISTINCT FROM
                    NEW.dependency_fingerprint THEN
                RAISE EXCEPTION 'WP06_SCORER_ARTIFACT_CANONICAL_INVALID';
            END IF;
            SELECT * INTO v_attempt FROM public.learning_training_attempt
             WHERE workspace_id = NEW.workspace_id AND id = NEW.attempt_id;
            SELECT * INTO v_base FROM public.learning_training_artifact
             WHERE workspace_id = NEW.workspace_id AND attempt_id = NEW.attempt_id;
            IF NOT FOUND OR v_base.id IS DISTINCT FROM NEW.base_artifact_id
                OR v_base.artifact_hash IS DISTINCT FROM NEW.base_artifact_hash
                OR v_attempt.normalized_dataset_id IS DISTINCT FROM NEW.normalized_dataset_id
                OR v_attempt.manifest_hash IS DISTINCT FROM NEW.manifest_hash
                OR v_attempt.fold_plan_hash IS DISTINCT FROM NEW.fold_plan_hash
                OR v_attempt.fold_id IS DISTINCT FROM NEW.fold_id
                OR v_attempt.approval_hash IS DISTINCT FROM NEW.approval_hash
                OR v_attempt.dependency_fingerprint IS DISTINCT FROM NEW.dependency_fingerprint THEN
                RAISE EXCEPTION 'WP06_SCORER_ARTIFACT_LINEAGE_INVALID';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER wp06_validate_learning_scorer_artifact BEFORE INSERT "
        f"ON public.{TABLE} FOR EACH ROW EXECUTE FUNCTION "
        "public.wp06_validate_learning_scorer_artifact()"
    )
    op.execute("REVOKE ALL ON FUNCTION public.wp06_validate_learning_scorer_artifact() FROM PUBLIC")

    _create_lineage_gateway()


def _create_lineage_gateway() -> None:
    op.execute(
        "CREATE POLICY learning_batch_lineage_reader_scope "
        "ON public.backtest_decision_opportunity "
        f"FOR SELECT TO {READER} USING ({WORKSPACE})"
    )
    op.execute(
        f"GRANT SELECT (workspace_id,run_id,trial_record_id,event_id,schema_version,"
        "input_state,strategy_state,strategy_release_id,strategy_content_hash,"
        "feature_schema_hash,vector_hash,candidate_id) "
        f"ON public.backtest_decision_opportunity TO {READER}"
    )
    op.execute(f"GRANT SELECT (workspace_id,id,manifest) ON public.backtest_run TO {READER}")
    op.execute(
        "GRANT SELECT (workspace_id,id,trial_record_id,manifest_hash,outcome_hash,"
        "entry_opportunity_event_id,entry_candidate_id,entry_vector_hash,"
        "entry_feature_schema_hash) "
        f"ON public.backtest_trade_outcome TO {READER}"
    )
    op.execute(f"""
        CREATE FUNCTION {LINEAGE_FUNCTION}(
            target_workspace uuid, target_attempt_id uuid
        ) RETURNS TABLE (
            attempt_id uuid, workspace_id uuid, normalized_dataset_id uuid,
            manifest_hash text, split_policy_version text, fold_plan_hash text,
            approval_hash text, fold_id text, sample_id text, role text,
            strategy_release_id text, strategy_release_content_hash text,
            vector_schema_version text, vector_schema_hash text, vector_hash text
        ) LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            attempt_row public.learning_training_attempt%ROWTYPE;
            expected_count integer;
            lineage_count integer;
        BEGIN
            IF session_user <> '{TRAINER}' THEN
                RAISE EXCEPTION 'WP06_TRAINING_LINEAGE_CALLER_DENIED';
            END IF;
            IF target_workspace IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_TRAINING_LINEAGE_WORKSPACE_DENIED';
            END IF;
            SELECT * INTO attempt_row FROM public.learning_training_attempt AS attempt
             WHERE attempt.workspace_id = target_workspace AND attempt.id = target_attempt_id;
            IF NOT FOUND OR NOT EXISTS (
                SELECT 1 FROM public.learning_training_access_receipt AS receipt
                 WHERE receipt.workspace_id = target_workspace
                   AND receipt.attempt_id = target_attempt_id
                   AND receipt.normalized_dataset_id = attempt_row.normalized_dataset_id
                   AND receipt.manifest_hash = attempt_row.manifest_hash
                   AND receipt.fold_plan_hash = attempt_row.fold_plan_hash
                   AND receipt.fold_id = attempt_row.fold_id
                   AND receipt.approval_hash = attempt_row.approval_hash
                   AND receipt.request_fingerprint = attempt_row.content_hash
                   AND receipt.actor_principal = '{TRAINER}'
                   AND receipt.purpose = 'trainer-input'
            ) THEN
                RAISE EXCEPTION 'WP06_TRAINING_LINEAGE_RECEIPT_REQUIRED';
            END IF;
            SELECT count(*)::integer INTO expected_count
              FROM public.learning_fold_assignment AS assignment
             WHERE assignment.workspace_id = target_workspace
               AND assignment.manifest_hash = attempt_row.manifest_hash
               AND assignment.fold_plan_hash = attempt_row.fold_plan_hash
               AND assignment.fold_id = attempt_row.fold_id
               AND assignment.role IN ('train','validation');
            SELECT count(*)::integer INTO lineage_count
              FROM public.learning_fold_assignment AS assignment
              JOIN public.learning_normalized_sample AS sample
                ON sample.workspace_id = assignment.workspace_id
               AND sample.manifest_hash = assignment.manifest_hash
               AND sample.fold_plan_hash = assignment.fold_plan_hash
               AND sample.sample_id = assignment.sample_id
              JOIN public.backtest_trade_outcome AS outcome
                ON outcome.workspace_id = sample.workspace_id
               AND outcome.id::text = sample.outcome_id
               AND outcome.manifest_hash = sample.manifest_hash
               AND outcome.outcome_hash = sample.outcome_hash
              JOIN public.backtest_decision_opportunity AS opportunity
                ON opportunity.workspace_id = outcome.workspace_id
               AND opportunity.trial_record_id = outcome.trial_record_id
               AND opportunity.event_id = outcome.entry_opportunity_event_id
              JOIN public.backtest_run AS run
                ON run.workspace_id = opportunity.workspace_id
               AND run.id = opportunity.run_id
             WHERE assignment.workspace_id = target_workspace
               AND assignment.manifest_hash = attempt_row.manifest_hash
               AND assignment.fold_plan_hash = attempt_row.fold_plan_hash
               AND assignment.fold_id = attempt_row.fold_id
               AND assignment.role IN ('train','validation')
               AND opportunity.schema_version = 'crypidian-replay-decision-opportunity-v3'
               AND opportunity.input_state = 'EVALUATED'
               AND opportunity.strategy_state = 'CANDIDATE'
               AND opportunity.candidate_id = outcome.entry_candidate_id
               AND opportunity.vector_hash = outcome.entry_vector_hash
               AND opportunity.feature_schema_hash = outcome.entry_feature_schema_hash
               AND opportunity.feature_schema_hash = sample.feature_schema_hash
               AND run.manifest->>'strategy_release_id' = opportunity.strategy_release_id
               AND run.manifest->>'strategy_content_hash' = opportunity.strategy_content_hash;
            IF expected_count = 0 OR lineage_count <> expected_count THEN
                RETURN;
            END IF;
            RETURN QUERY
                SELECT attempt_row.id, attempt_row.workspace_id, attempt_row.normalized_dataset_id,
                       attempt_row.manifest_hash::text, attempt_row.split_policy_version::text,
                       attempt_row.fold_plan_hash::text, attempt_row.approval_hash::text,
                       attempt_row.fold_id::text, sample.sample_id, assignment.role::text,
                       opportunity.strategy_release_id, opportunity.strategy_content_hash,
                       'crypidian-learning-feature-vector-v1'::text,
                       opportunity.feature_schema_hash, opportunity.vector_hash
                  FROM public.learning_fold_assignment AS assignment
                  JOIN public.learning_normalized_sample AS sample
                    ON sample.workspace_id = assignment.workspace_id
                   AND sample.manifest_hash = assignment.manifest_hash
                   AND sample.fold_plan_hash = assignment.fold_plan_hash
                   AND sample.sample_id = assignment.sample_id
                  JOIN public.backtest_trade_outcome AS outcome
                    ON outcome.workspace_id = sample.workspace_id
                   AND outcome.id::text = sample.outcome_id
                   AND outcome.manifest_hash = sample.manifest_hash
                   AND outcome.outcome_hash = sample.outcome_hash
                  JOIN public.backtest_decision_opportunity AS opportunity
                    ON opportunity.workspace_id = outcome.workspace_id
                   AND opportunity.trial_record_id = outcome.trial_record_id
                   AND opportunity.event_id = outcome.entry_opportunity_event_id
                  JOIN public.backtest_run AS run
                    ON run.workspace_id = opportunity.workspace_id
                   AND run.id = opportunity.run_id
                 WHERE assignment.workspace_id = target_workspace
                   AND assignment.manifest_hash = attempt_row.manifest_hash
                   AND assignment.fold_plan_hash = attempt_row.fold_plan_hash
                   AND assignment.fold_id = attempt_row.fold_id
                   AND assignment.role IN ('train','validation')
                   AND opportunity.schema_version = 'crypidian-replay-decision-opportunity-v3'
                   AND opportunity.input_state = 'EVALUATED'
                   AND opportunity.strategy_state = 'CANDIDATE'
                   AND opportunity.candidate_id = outcome.entry_candidate_id
                   AND opportunity.vector_hash = outcome.entry_vector_hash
                   AND opportunity.feature_schema_hash = outcome.entry_feature_schema_hash
                   AND opportunity.feature_schema_hash = sample.feature_schema_hash
                   AND run.manifest->>'strategy_release_id' = opportunity.strategy_release_id
                   AND run.manifest->>'strategy_content_hash' = opportunity.strategy_content_hash
                 ORDER BY assignment.role, sample.sample_id;
        END $$
    """)
    op.execute(f"ALTER FUNCTION {LINEAGE_FUNCTION_SIGNATURE} OWNER TO {READER}")
    op.execute(f"REVOKE ALL ON FUNCTION {LINEAGE_FUNCTION_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {LINEAGE_FUNCTION_SIGNATURE} TO {TRAINER}")


def downgrade() -> None:
    op.execute(f"LOCK TABLE public.{TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.{TABLE}) THEN
            RAISE EXCEPTION 'WP06_SCORER_ARTIFACT_DOWNGRADE_REQUIRES_EMPTY_TABLE';
        END IF;
    END $$""")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {LINEAGE_FUNCTION_SIGNATURE} FROM {TRAINER}")
    op.execute(f"DROP FUNCTION {LINEAGE_FUNCTION_SIGNATURE}")
    op.execute(
        "DROP POLICY learning_batch_lineage_reader_scope ON public.backtest_decision_opportunity"
    )
    op.execute(
        "REVOKE SELECT (workspace_id,run_id,trial_record_id,event_id,schema_version,"
        "input_state,strategy_state,strategy_release_id,strategy_content_hash,"
        "feature_schema_hash,vector_hash,candidate_id) "
        f"ON public.backtest_decision_opportunity FROM {READER}"
    )
    op.execute(f"REVOKE SELECT (workspace_id,id,manifest) ON public.backtest_run FROM {READER}")
    op.execute(
        "REVOKE SELECT (workspace_id,id,trial_record_id,manifest_hash,outcome_hash,"
        "entry_opportunity_event_id,entry_candidate_id,entry_vector_hash,"
        "entry_feature_schema_hash) "
        f"ON public.backtest_trade_outcome FROM {READER}"
    )
    op.execute(
        "DROP TRIGGER wp06_validate_learning_scorer_artifact ON public.learning_scorer_artifact"
    )
    op.execute("DROP FUNCTION public.wp06_validate_learning_scorer_artifact()")
    op.execute(f"DROP TRIGGER {TABLE}_immutable ON public.{TABLE}")
    op.execute("DROP FUNCTION public.reject_learning_scorer_artifact_mutation()")
    for policy in (
        "learning_scorer_artifact_evaluator_read",
        "learning_scorer_artifact_admin_read",
        "learning_scorer_artifact_trainer_insert",
        "learning_scorer_artifact_trainer_read",
    ):
        op.execute(f"DROP POLICY {policy} ON public.{TABLE}")
    op.drop_index("ix_learning_scorer_artifact_manifest_fold", table_name=TABLE)
    op.drop_index("ix_learning_scorer_artifact_workspace_created", table_name=TABLE)
    op.drop_table(TABLE)
