"""Khôi phục quyền TRAINING exact-scope sau thứ tự nhánh migration lịch sử."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e2f4a6c8b0d4"
down_revision: str | Sequence[str] | None = "e2f4a6c8b0d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRAINER = "crypidian_trainer_worker"
TRAINER_READER = "crypidian_learning_batch_reader"
GATEWAY = "public.wp06_claim_learning_training_batch"
BACKUP = "public.wp06_claim_learning_training_batch_pre_exact_scope"

SOURCE_AWARE_GATEWAY = """CREATE FUNCTION public.wp06_claim_learning_training_batch(
    target_workspace uuid, target_attempt_id uuid
) RETURNS TABLE (
    attempt_id uuid, workspace_id uuid, normalized_dataset_id uuid,
    manifest_hash text, split_policy_version text, fold_plan_hash text,
    approval_hash text, fold_id text, seed integer, trainer_version text,
    dependency_fingerprint text, sample_id text, role text, outcome_id text,
    outcome_hash text, feature_snapshot_hash text, feature_schema_hash text,
    label_hash text, label_value integer, features jsonb, sample_hash text
) LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    attempt_row public.learning_training_attempt%ROWTYPE;
    stored_manifest jsonb;
    latest_state text;
    latest_actor text;
    latest_sequence integer;
    approved_hash text;
    stored_dataset_id uuid;
    train_count integer;
    validation_count integer;
    invalid_count integer;
    profile_train_count integer;
    profile_validation_count integer;
    profile_feature_count integer;
    profile_schema_hash text;
    profile_hash text;
BEGIN
    IF session_user <> 'crypidian_trainer_worker' THEN
        RAISE EXCEPTION 'WP06_TRAINING_BATCH_CALLER_DENIED';
    END IF;
    IF target_workspace IS DISTINCT FROM
        NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
        RAISE EXCEPTION 'WP06_TRAINING_BATCH_WORKSPACE_DENIED';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(target_attempt_id::text, 0)
    );
    SELECT attempt.* INTO attempt_row
      FROM public.learning_training_attempt AS attempt
     WHERE attempt.workspace_id = target_workspace
       AND attempt.id = target_attempt_id;
    IF NOT FOUND OR attempt_row.initial_state <> 'queued' THEN
        RAISE EXCEPTION 'WP06_TRAINING_BATCH_ATTEMPT_NOT_FOUND';
    END IF;
    SELECT event.state, event.sequence, event.actor_principal
      INTO latest_state, latest_sequence, latest_actor
      FROM public.learning_attempt_event AS event
     WHERE event.workspace_id = target_workspace
       AND event.attempt_id = target_attempt_id
     ORDER BY event.sequence DESC LIMIT 1;
    IF NOT FOUND OR latest_state <> 'running' OR latest_sequence <> 2
       OR latest_actor <> 'crypidian_trainer_worker' THEN
        RAISE EXCEPTION 'WP06_TRAINING_BATCH_ATTEMPT_NOT_RUNNING';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.learning_training_access_receipt AS receipt
         WHERE receipt.workspace_id = target_workspace
           AND receipt.attempt_id = target_attempt_id
    ) THEN
        RAISE EXCEPTION 'WP06_TRAINING_BATCH_ALREADY_CONSUMED';
    END IF;
    SELECT manifest_record.manifest INTO stored_manifest
      FROM public.dataset_manifest AS manifest_record
     WHERE manifest_record.workspace_id = target_workspace
       AND manifest_record.manifest_hash = attempt_row.manifest_hash;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'WP06_TRAINING_BATCH_MANIFEST_NOT_FOUND';
    END IF;
    SELECT approval.approval_hash INTO approved_hash
      FROM public.learning_split_approval AS approval
     WHERE approval.workspace_id = target_workspace
       AND approval.manifest_hash = attempt_row.manifest_hash
       AND approval.split_policy_version = attempt_row.split_policy_version
       AND approval.fold_plan_hash = attempt_row.fold_plan_hash;
    IF NOT FOUND OR approved_hash <> attempt_row.approval_hash THEN
        RAISE EXCEPTION 'WP06_TRAINING_BATCH_APPROVAL_MISMATCH';
    END IF;
    SELECT dataset.id INTO stored_dataset_id
      FROM public.learning_normalized_dataset AS dataset
     WHERE dataset.workspace_id = target_workspace
       AND dataset.id = attempt_row.normalized_dataset_id
       AND dataset.manifest_hash = attempt_row.manifest_hash
       AND dataset.split_policy_version = attempt_row.split_policy_version
       AND dataset.fold_plan_hash = attempt_row.fold_plan_hash
       AND dataset.approval_hash = attempt_row.approval_hash;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'WP06_TRAINING_BATCH_DATASET_MISMATCH';
    END IF;

    PERFORM public.wp06_check_replay_training_scope(
        target_workspace, attempt_row.manifest_hash, stored_manifest,
        stored_dataset_id, attempt_row.fold_plan_hash, 'training',
        target_attempt_id, true
    );

    SELECT profile.training_samples, profile.validation_samples, profile.feature_count,
           profile.feature_schema_hash, profile.profile_hash
      INTO profile_train_count, profile_validation_count, profile_feature_count,
           profile_schema_hash, profile_hash
      FROM public.learning_fold_resource_profile AS profile
     WHERE profile.workspace_id = target_workspace
       AND profile.normalized_dataset_id = stored_dataset_id
       AND profile.manifest_hash = attempt_row.manifest_hash
       AND profile.fold_plan_hash = attempt_row.fold_plan_hash
       AND profile.fold_id = attempt_row.fold_id;
    IF NOT FOUND
       OR profile_train_count <> (attempt_row.resource_request->>'training_samples')::integer
       OR profile_validation_count <> (attempt_row.resource_request->>'validation_samples')::integer
       OR profile_feature_count <> (attempt_row.resource_request->>'feature_count')::integer THEN
        RAISE EXCEPTION 'WP06_TRAINING_BATCH_PROFILE_MISMATCH';
    END IF;
    SELECT count(*) FILTER (WHERE assignment.role = 'train')::integer,
           count(*) FILTER (WHERE assignment.role = 'validation')::integer,
           count(*) FILTER (
               WHERE sample.feature_schema_hash <> profile_schema_hash
                  OR pg_catalog.jsonb_typeof(sample.features) <> 'object'
                  OR sample.features = '{}'::jsonb
                  OR (SELECT count(*) FROM pg_catalog.jsonb_object_keys(sample.features))
                        <> profile_feature_count
           )::integer
      INTO train_count, validation_count, invalid_count
      FROM public.learning_fold_assignment AS assignment
      JOIN public.learning_normalized_sample AS sample
        ON sample.workspace_id = assignment.workspace_id
       AND sample.manifest_hash = assignment.manifest_hash
       AND sample.fold_plan_hash = assignment.fold_plan_hash
       AND sample.sample_id = assignment.sample_id
     WHERE assignment.workspace_id = target_workspace
       AND assignment.manifest_hash = attempt_row.manifest_hash
       AND assignment.fold_plan_hash = attempt_row.fold_plan_hash
       AND assignment.fold_id = attempt_row.fold_id
       AND assignment.role IN ('train', 'validation');
    IF train_count IS DISTINCT FROM profile_train_count
       OR validation_count IS DISTINCT FROM profile_validation_count
       OR invalid_count <> 0 THEN
        RAISE EXCEPTION 'WP06_TRAINING_BATCH_COUNTS_INVALID';
    END IF;
    INSERT INTO public.learning_training_access_receipt(
        id, workspace_id, attempt_id, normalized_dataset_id, manifest_hash,
        fold_plan_hash, fold_id, approval_hash, request_fingerprint, profile_hash,
        purpose, actor_principal, training_samples, validation_samples, sample_count
    ) VALUES (
        pg_catalog.gen_random_uuid(), target_workspace, target_attempt_id,
        stored_dataset_id, attempt_row.manifest_hash, attempt_row.fold_plan_hash,
        attempt_row.fold_id, attempt_row.approval_hash, attempt_row.content_hash,
        profile_hash, 'trainer-input', 'crypidian_trainer_worker', train_count,
        validation_count, train_count + validation_count
    );

    PERFORM public.wp06_check_replay_training_scope(
        target_workspace, attempt_row.manifest_hash, stored_manifest,
        stored_dataset_id, attempt_row.fold_plan_hash, 'training',
        target_attempt_id, false
    );
    RETURN QUERY
        SELECT attempt_row.id, attempt_row.workspace_id,
               attempt_row.normalized_dataset_id,
               attempt_row.manifest_hash::text,
               attempt_row.split_policy_version::text,
               attempt_row.fold_plan_hash::text, attempt_row.approval_hash::text,
               attempt_row.fold_id::text, attempt_row.seed,
               attempt_row.trainer_version::text,
               attempt_row.dependency_fingerprint::text, sample.sample_id,
               assignment.role::text, sample.outcome_id::text,
               sample.outcome_hash::text, sample.feature_snapshot_hash::text,
               sample.feature_schema_hash::text, sample.label_hash::text,
               sample.label_value, sample.features, sample.sample_hash::text
          FROM public.learning_fold_assignment AS assignment
          JOIN public.learning_normalized_sample AS sample
            ON sample.workspace_id = assignment.workspace_id
           AND sample.manifest_hash = assignment.manifest_hash
           AND sample.fold_plan_hash = assignment.fold_plan_hash
           AND sample.sample_id = assignment.sample_id
         WHERE assignment.workspace_id = target_workspace
           AND assignment.manifest_hash = attempt_row.manifest_hash
           AND assignment.fold_plan_hash = attempt_row.fold_plan_hash
           AND assignment.fold_id = attempt_row.fold_id
           AND assignment.role IN ('train', 'validation')
         ORDER BY assignment.role, assignment.sample_id;
END $$"""


def upgrade() -> None:
    """Lưu claim gateway tiền nhiệm rồi cài exact-scope current-rights gate."""
    op.execute(
        "ALTER FUNCTION public.wp06_claim_learning_training_batch(uuid,uuid) "
        "RENAME TO wp06_claim_learning_training_batch_pre_exact_scope"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_claim_learning_training_batch_pre_exact_scope"
        f"(uuid,uuid) FROM PUBLIC, {TRAINER}"
    )
    op.execute(SOURCE_AWARE_GATEWAY)
    op.execute(f"ALTER FUNCTION {GATEWAY}(uuid,uuid) OWNER TO {TRAINER_READER}")
    op.execute(f"REVOKE ALL ON FUNCTION {GATEWAY}(uuid,uuid) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {GATEWAY}(uuid,uuid) TO {TRAINER}")


def downgrade() -> None:
    """Khôi phục chính xác claim function đã được đổi tên khi nâng cấp."""
    op.execute(f"DROP FUNCTION {GATEWAY}(uuid,uuid)")
    op.execute(
        "ALTER FUNCTION public.wp06_claim_learning_training_batch_pre_exact_scope(uuid,uuid) "
        "RENAME TO wp06_claim_learning_training_batch"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {GATEWAY}(uuid,uuid) TO {TRAINER}")
