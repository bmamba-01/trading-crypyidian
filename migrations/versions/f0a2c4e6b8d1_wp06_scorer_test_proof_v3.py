"""WP06C: lưu NOT_RUN riêng trong scorer TEST proof v3."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f0a2c4e6b8d1"
down_revision: str | None = "c6e8a0b2d4f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "learning_scorer_test_opportunity_proof"
OPPORTUNITY_TABLE = "backtest_decision_opportunity"
BUILDER = "crypidian_candidate_builder"


def upgrade() -> None:
    """Thêm count nullable cho v2 và thay validator để nhận v2/v3 bất biến."""
    op.add_column(TABLE, sa.Column("not_run_count", sa.Integer(), nullable=True))
    op.drop_constraint("learning_scorer_test_proof_counts_valid", TABLE, type_="check")
    op.drop_constraint("learning_scorer_test_proof_completeness_valid", TABLE, type_="check")
    op.create_check_constraint(
        "learning_scorer_test_proof_counts_valid",
        TABLE,
        "expected_opportunity_count > 0 AND candidate_count >= 0 "
        "AND no_candidate_count >= 0 AND input_excluded_count >= 0 "
        "AND (not_run_count IS NULL OR not_run_count >= 0) "
        "AND source_gap_count >= 0 AND membership_mismatch_count >= 0 "
        "AND unlabeled_candidate_count >= 0 "
        "AND ((not_run_count IS NULL AND candidate_count + no_candidate_count "
        "+ input_excluded_count + membership_mismatch_count = expected_opportunity_count) "
        "OR (not_run_count IS NOT NULL AND candidate_count + no_candidate_count "
        "+ input_excluded_count + not_run_count + membership_mismatch_count "
        "= expected_opportunity_count)) "
        "AND unlabeled_candidate_count <= candidate_count",
    )
    op.create_check_constraint(
        "learning_scorer_test_proof_completeness_valid",
        TABLE,
        "completeness_verified = (input_excluded_count = 0 AND source_gap_count = 0 "
        "AND coalesce(not_run_count, 0) = 0 AND membership_mismatch_count = 0 "
        "AND unlabeled_candidate_count = 0 "
        "AND candidate_count + no_candidate_count = expected_opportunity_count)",
    )
    op.execute(_v2_v3_validator())


def downgrade() -> None:
    """Không bỏ v3 khi sổ đã dùng; giữ nguyên proof v2 cũ."""
    op.execute(f"LOCK TABLE public.{TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.{TABLE} WHERE not_run_count IS NOT NULL) THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_V3_DOWNGRADE_POPULATED_DENIED';
        END IF;
    END $$""")
    op.execute(_v2_validator())
    op.drop_constraint("learning_scorer_test_proof_counts_valid", TABLE, type_="check")
    op.drop_constraint("learning_scorer_test_proof_completeness_valid", TABLE, type_="check")
    op.create_check_constraint(
        "learning_scorer_test_proof_counts_valid",
        TABLE,
        "expected_opportunity_count > 0 AND candidate_count >= 0 "
        "AND no_candidate_count >= 0 AND input_excluded_count >= 0 "
        "AND source_gap_count >= 0 AND membership_mismatch_count >= 0 "
        "AND unlabeled_candidate_count >= 0 "
        "AND candidate_count + no_candidate_count + input_excluded_count "
        "+ membership_mismatch_count = expected_opportunity_count "
        "AND unlabeled_candidate_count <= candidate_count",
    )
    op.create_check_constraint(
        "learning_scorer_test_proof_completeness_valid",
        TABLE,
        "completeness_verified = (input_excluded_count = 0 AND source_gap_count = 0 "
        "AND membership_mismatch_count = 0 AND unlabeled_candidate_count = 0 "
        "AND candidate_count + no_candidate_count = expected_opportunity_count)",
    )
    op.drop_column(TABLE, "not_run_count")


def _v2_v3_validator() -> str:
    """Trigger kiểm canonical v2/v3, reason và mọi count đã index."""
    return f"""CREATE OR REPLACE FUNCTION public.validate_learning_scorer_test_opportunity_proof()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            v_payload jsonb;
            v_entries jsonb;
            v_sorted_entries jsonb;
            v_entry jsonb;
            v_schema_version text;
            v_entry_count integer;
            v_candidate_count integer := 0;
            v_no_candidate_count integer := 0;
            v_input_excluded_count integer := 0;
            v_not_run_count integer := 0;
            v_unlabeled_candidate_count integer := 0;
            v_distinct_event_count integer;
            v_distinct_candidate_count integer;
        BEGIN
            IF session_user <> '{BUILDER}' THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_CALLER_DENIED';
            END IF;
            v_payload := NEW.canonical_proof::jsonb;
            v_schema_version := v_payload->>'schema_version';
            IF jsonb_typeof(v_payload) IS DISTINCT FROM 'object'
                OR (v_schema_version IS DISTINCT FROM
                        'wp06-scorer-test-opportunity-proof-v2'
                    AND v_schema_version IS DISTINCT FROM
                        'wp06-scorer-test-opportunity-proof-v3')
                OR (v_schema_version = 'wp06-scorer-test-opportunity-proof-v2'
                    AND (SELECT count(*) FROM jsonb_object_keys(v_payload)) <> 19)
                OR (v_schema_version = 'wp06-scorer-test-opportunity-proof-v3'
                    AND (SELECT count(*) FROM jsonb_object_keys(v_payload)) <> 20)
                OR (v_payload ?& ARRAY[
                    'schema_version','workspace_id','evaluation_run_id','attempt_id',
                    'normalized_dataset_id','manifest_hash','fold_plan_hash','fold_id',
                    'approval_hash','bundle_hash','vector_schema_version',
                    'vector_schema_hash','test_membership_digest','source_scope_hash',
                    'expected_opportunity_count','source_gap_count',
                    'membership_mismatch_count','unlabeled_candidate_count','entries'
                ]::text[]) IS NOT TRUE
                OR (v_schema_version = 'wp06-scorer-test-opportunity-proof-v2'
                    AND (NEW.not_run_count IS NOT NULL OR v_payload ? 'not_run_count'))
                OR (v_schema_version = 'wp06-scorer-test-opportunity-proof-v3'
                    AND (NEW.not_run_count IS NULL OR NOT (v_payload ? 'not_run_count')
                         OR jsonb_typeof(v_payload->'not_run_count') IS DISTINCT FROM 'number'
                         OR (v_payload->>'not_run_count') !~ '^(0|[1-9][0-9]*)$'
                         OR (v_payload->>'not_run_count')::integer IS DISTINCT FROM
                            NEW.not_run_count))
                OR pg_catalog.encode(pg_catalog.sha256(
                    pg_catalog.convert_to(NEW.canonical_proof, 'UTF8')
                ), 'hex') IS DISTINCT FROM NEW.proof_hash
                OR NEW.canonical_proof IS DISTINCT FROM
                    public.wp06_scorer_test_proof_canonical_json(v_payload)
                OR v_payload->>'workspace_id' IS DISTINCT FROM NEW.workspace_id::text
                OR v_payload->>'evaluation_run_id' IS DISTINCT FROM NEW.evaluation_run_id::text
                OR v_payload->>'attempt_id' IS DISTINCT FROM NEW.attempt_id::text
                OR v_payload->>'normalized_dataset_id' IS DISTINCT FROM
                    NEW.normalized_dataset_id::text
                OR v_payload->>'manifest_hash' IS DISTINCT FROM NEW.manifest_hash
                OR v_payload->>'fold_plan_hash' IS DISTINCT FROM NEW.fold_plan_hash
                OR v_payload->>'fold_id' IS DISTINCT FROM NEW.fold_id
                OR v_payload->>'approval_hash' IS DISTINCT FROM NEW.approval_hash
                OR v_payload->>'bundle_hash' IS DISTINCT FROM NEW.bundle_hash
                OR v_payload->>'vector_schema_version' IS DISTINCT FROM
                    NEW.vector_schema_version
                OR v_payload->>'vector_schema_hash' IS DISTINCT FROM NEW.vector_schema_hash
                OR v_payload->>'test_membership_digest' IS DISTINCT FROM
                    NEW.test_membership_digest
                OR v_payload->>'source_scope_hash' IS DISTINCT FROM NEW.source_scope_hash
                OR (v_payload->>'expected_opportunity_count')::integer IS DISTINCT FROM
                    NEW.expected_opportunity_count
                OR (v_payload->>'source_gap_count')::integer IS DISTINCT FROM NEW.source_gap_count
                OR (v_payload->>'membership_mismatch_count')::integer IS DISTINCT FROM
                    NEW.membership_mismatch_count
                OR (v_payload->>'unlabeled_candidate_count')::integer IS DISTINCT FROM
                    NEW.unlabeled_candidate_count THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_CANONICAL_INVALID';
            END IF;
            v_entries := v_payload->'entries';
            IF jsonb_typeof(v_entries) IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_ENTRIES_INVALID';
            END IF;
            FOR v_entry IN
                SELECT value FROM jsonb_array_elements(v_entries) AS entry_values(value)
            LOOP
                IF jsonb_typeof(v_entry) IS DISTINCT FROM 'object'
                    OR (v_schema_version = 'wp06-scorer-test-opportunity-proof-v2'
                        AND (SELECT count(*) FROM jsonb_object_keys(v_entry)) <> 8)
                    OR (v_schema_version = 'wp06-scorer-test-opportunity-proof-v3'
                        AND (SELECT count(*) FROM jsonb_object_keys(v_entry)) <> 9)
                    OR (v_entry ?& ARRAY[
                        'stable_id_hash','status','candidate_id_hash','outcome_id_hash',
                        'outcome_hash','sample_id_hash','sample_hash','vector_hash'
                    ]::text[]) IS NOT TRUE
                    OR (v_schema_version = 'wp06-scorer-test-opportunity-proof-v2'
                        AND v_entry ? 'reason_code')
                    OR (v_schema_version = 'wp06-scorer-test-opportunity-proof-v3'
                        AND NOT (v_entry ? 'reason_code'))
                    OR coalesce(
                        v_entry->>'stable_id_hash' ~ '^[0-9a-f]{{64}}$', false
                    ) IS NOT TRUE THEN
                    RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_ENTRY_INVALID';
                END IF;
                IF v_entry->>'status' = 'candidate' THEN
                    IF coalesce(
                        v_entry->>'candidate_id_hash' ~ '^[0-9a-f]{{64}}$', false
                    ) IS NOT TRUE
                        OR (v_schema_version = 'wp06-scorer-test-opportunity-proof-v3'
                            AND v_entry->'reason_code' IS DISTINCT FROM 'null'::jsonb) THEN
                        RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_ENTRY_INVALID';
                    END IF;
                    IF v_entry->'outcome_id_hash' = 'null'::jsonb
                        AND v_entry->'outcome_hash' = 'null'::jsonb
                        AND v_entry->'sample_id_hash' = 'null'::jsonb
                        AND v_entry->'sample_hash' = 'null'::jsonb
                        AND v_entry->'vector_hash' = 'null'::jsonb THEN
                        v_unlabeled_candidate_count := v_unlabeled_candidate_count + 1;
                    ELSIF coalesce(
                        v_entry->>'outcome_id_hash' ~ '^[0-9a-f]{{64}}$', false
                    ) IS NOT TRUE OR coalesce(
                        v_entry->>'outcome_hash' ~ '^[0-9a-f]{{64}}$', false
                    ) IS NOT TRUE OR coalesce(
                        v_entry->>'sample_id_hash' ~ '^[0-9a-f]{{64}}$', false
                    ) IS NOT TRUE OR coalesce(
                        v_entry->>'sample_hash' ~ '^[0-9a-f]{{64}}$', false
                    ) IS NOT TRUE OR coalesce(
                        v_entry->>'vector_hash' ~ '^[0-9a-f]{{64}}$', false
                    ) IS NOT TRUE THEN
                        RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_ENTRY_INVALID';
                    END IF;
                    v_candidate_count := v_candidate_count + 1;
                ELSIF v_entry->>'status' = 'no_candidate'
                    AND v_entry->'candidate_id_hash' = 'null'::jsonb
                    AND v_entry->'outcome_id_hash' = 'null'::jsonb
                    AND v_entry->'outcome_hash' = 'null'::jsonb
                    AND v_entry->'sample_id_hash' = 'null'::jsonb
                    AND v_entry->'sample_hash' = 'null'::jsonb
                    AND v_entry->'vector_hash' = 'null'::jsonb
                    AND (v_schema_version = 'wp06-scorer-test-opportunity-proof-v2'
                         OR v_entry->'reason_code' IS NOT DISTINCT FROM 'null'::jsonb) THEN
                    v_no_candidate_count := v_no_candidate_count + 1;
                ELSIF v_entry->>'status' = 'input_excluded'
                    AND v_entry->'candidate_id_hash' = 'null'::jsonb
                    AND v_entry->'outcome_id_hash' = 'null'::jsonb
                    AND v_entry->'outcome_hash' = 'null'::jsonb
                    AND v_entry->'sample_id_hash' = 'null'::jsonb
                    AND v_entry->'sample_hash' = 'null'::jsonb
                    AND v_entry->'vector_hash' = 'null'::jsonb
                    AND (v_schema_version = 'wp06-scorer-test-opportunity-proof-v2'
                         OR (jsonb_typeof(v_entry->'reason_code') = 'string'
                             AND coalesce(v_entry->>'reason_code' ~
                                '^[A-Za-z0-9][A-Za-z0-9_.:-]{{0,119}}$', false))) THEN
                    v_input_excluded_count := v_input_excluded_count + 1;
                ELSIF v_schema_version = 'wp06-scorer-test-opportunity-proof-v3'
                    AND v_entry->>'status' = 'not_run'
                    AND v_entry->'candidate_id_hash' = 'null'::jsonb
                    AND v_entry->'outcome_id_hash' = 'null'::jsonb
                    AND v_entry->'outcome_hash' = 'null'::jsonb
                    AND v_entry->'sample_id_hash' = 'null'::jsonb
                    AND v_entry->'sample_hash' = 'null'::jsonb
                    AND coalesce(v_entry->>'vector_hash' ~ '^[0-9a-f]{{64}}$', false)
                    AND jsonb_typeof(v_entry->'reason_code') = 'string'
                    AND coalesce(v_entry->>'reason_code' ~
                        '^[A-Za-z0-9][A-Za-z0-9_.:-]{{0,119}}$', false) THEN
                    v_not_run_count := v_not_run_count + 1;
                ELSE
                    RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_ENTRY_INVALID';
                END IF;
            END LOOP;
            SELECT count(*)::integer,
                   count(DISTINCT item->>'stable_id_hash')::integer,
                   count(DISTINCT item->>'candidate_id_hash') FILTER
                       (WHERE item->>'status' = 'candidate')::integer,
                   coalesce(jsonb_agg(item ORDER BY item->>'stable_id_hash'), '[]'::jsonb)
              INTO v_entry_count, v_distinct_event_count,
                   v_distinct_candidate_count, v_sorted_entries
              FROM jsonb_array_elements(v_entries) AS entry_rows(item);
            IF v_entry_count IS DISTINCT FROM v_distinct_event_count
                OR v_candidate_count IS DISTINCT FROM v_distinct_candidate_count
                OR v_sorted_entries IS DISTINCT FROM v_entries
                OR v_candidate_count IS DISTINCT FROM NEW.candidate_count
                OR v_no_candidate_count IS DISTINCT FROM NEW.no_candidate_count
                OR v_input_excluded_count IS DISTINCT FROM NEW.input_excluded_count
                OR v_unlabeled_candidate_count IS DISTINCT FROM
                    NEW.unlabeled_candidate_count
                OR v_entry_count + NEW.membership_mismatch_count IS DISTINCT FROM
                    NEW.expected_opportunity_count
                OR (v_schema_version = 'wp06-scorer-test-opportunity-proof-v2'
                    AND (NEW.candidate_count + NEW.no_candidate_count
                         + NEW.input_excluded_count + NEW.membership_mismatch_count
                         IS DISTINCT FROM NEW.expected_opportunity_count))
                OR (v_schema_version = 'wp06-scorer-test-opportunity-proof-v3'
                    AND (v_not_run_count IS DISTINCT FROM NEW.not_run_count
                         OR NEW.candidate_count + NEW.no_candidate_count
                            + NEW.input_excluded_count + NEW.not_run_count
                            + NEW.membership_mismatch_count
                            IS DISTINCT FROM NEW.expected_opportunity_count))
                OR v_unlabeled_candidate_count > NEW.candidate_count
                OR NEW.completeness_verified IS DISTINCT FROM (
                    NEW.input_excluded_count = 0 AND v_not_run_count = 0
                    AND NEW.source_gap_count = 0
                    AND NEW.membership_mismatch_count = 0
                    AND NEW.unlabeled_candidate_count = 0
                    AND NEW.candidate_count + NEW.no_candidate_count =
                        NEW.expected_opportunity_count
                ) THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_COUNTS_INVALID';
            END IF;
            RETURN NEW;
        END $$"""


def _v2_validator() -> str:
    """Khôi phục nguyên contract v2 khi downgrade còn an toàn."""
    return f"""CREATE OR REPLACE FUNCTION public.validate_learning_scorer_test_opportunity_proof()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            v_payload jsonb;
            v_entries jsonb;
            v_sorted_entries jsonb;
            v_entry jsonb;
            v_entry_count integer;
            v_candidate_count integer := 0;
            v_no_candidate_count integer := 0;
            v_input_excluded_count integer := 0;
            v_unlabeled_candidate_count integer := 0;
            v_distinct_event_count integer;
            v_distinct_candidate_count integer;
        BEGIN
            IF session_user <> '{BUILDER}' THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_CALLER_DENIED';
            END IF;
            v_payload := NEW.canonical_proof::jsonb;
            IF jsonb_typeof(v_payload) IS DISTINCT FROM 'object'
                OR (SELECT count(*) FROM jsonb_object_keys(v_payload)) <> 19
                OR NOT (v_payload ?& ARRAY[
                    'schema_version','workspace_id','evaluation_run_id','attempt_id',
                    'normalized_dataset_id','manifest_hash','fold_plan_hash','fold_id',
                    'approval_hash','bundle_hash','vector_schema_version',
                    'vector_schema_hash','test_membership_digest','source_scope_hash',
                    'expected_opportunity_count','source_gap_count',
                    'membership_mismatch_count','unlabeled_candidate_count','entries'
                ]::text[])
                OR v_payload->>'schema_version' IS DISTINCT FROM
                    'wp06-scorer-test-opportunity-proof-v2'
                OR pg_catalog.encode(pg_catalog.sha256(
                    pg_catalog.convert_to(NEW.canonical_proof, 'UTF8')
                ), 'hex') IS DISTINCT FROM NEW.proof_hash
                OR NEW.canonical_proof IS DISTINCT FROM
                    public.wp06_scorer_test_proof_canonical_json(v_payload)
                OR v_payload->>'workspace_id' IS DISTINCT FROM NEW.workspace_id::text
                OR v_payload->>'evaluation_run_id' IS DISTINCT FROM NEW.evaluation_run_id::text
                OR v_payload->>'attempt_id' IS DISTINCT FROM NEW.attempt_id::text
                OR v_payload->>'normalized_dataset_id' IS DISTINCT FROM
                    NEW.normalized_dataset_id::text
                OR v_payload->>'manifest_hash' IS DISTINCT FROM NEW.manifest_hash
                OR v_payload->>'fold_plan_hash' IS DISTINCT FROM NEW.fold_plan_hash
                OR v_payload->>'fold_id' IS DISTINCT FROM NEW.fold_id
                OR v_payload->>'approval_hash' IS DISTINCT FROM NEW.approval_hash
                OR v_payload->>'bundle_hash' IS DISTINCT FROM NEW.bundle_hash
                OR v_payload->>'vector_schema_version' IS DISTINCT FROM
                    NEW.vector_schema_version
                OR v_payload->>'vector_schema_hash' IS DISTINCT FROM NEW.vector_schema_hash
                OR v_payload->>'test_membership_digest' IS DISTINCT FROM
                    NEW.test_membership_digest
                OR v_payload->>'source_scope_hash' IS DISTINCT FROM NEW.source_scope_hash
                OR (v_payload->>'expected_opportunity_count')::integer IS DISTINCT FROM
                    NEW.expected_opportunity_count
                OR (v_payload->>'source_gap_count')::integer IS DISTINCT FROM NEW.source_gap_count
                OR (v_payload->>'membership_mismatch_count')::integer IS DISTINCT FROM
                    NEW.membership_mismatch_count
                OR (v_payload->>'unlabeled_candidate_count')::integer IS DISTINCT FROM
                    NEW.unlabeled_candidate_count THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_CANONICAL_INVALID';
            END IF;
            v_entries := v_payload->'entries';
            IF jsonb_typeof(v_entries) IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_ENTRIES_INVALID';
            END IF;
            FOR v_entry IN
                SELECT value FROM jsonb_array_elements(v_entries) AS entry_values(value)
            LOOP
                IF jsonb_typeof(v_entry) IS DISTINCT FROM 'object'
                    OR (SELECT count(*) FROM jsonb_object_keys(v_entry)) <> 8
                    OR NOT (v_entry ?& ARRAY[
                        'stable_id_hash','status','candidate_id_hash','outcome_id_hash',
                        'outcome_hash','sample_id_hash','sample_hash','vector_hash'
                    ]::text[])
                    OR coalesce(
                        v_entry->>'stable_id_hash' ~ '^[0-9a-f]{{64}}$', false
                    ) IS NOT TRUE THEN
                    RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_ENTRY_INVALID';
                END IF;
                IF v_entry->>'status' = 'candidate' THEN
                    IF coalesce(
                        v_entry->>'candidate_id_hash' ~ '^[0-9a-f]{{64}}$', false
                    ) IS NOT TRUE THEN
                        RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_ENTRY_INVALID';
                    END IF;
                    IF v_entry->'outcome_id_hash' = 'null'::jsonb
                        AND v_entry->'outcome_hash' = 'null'::jsonb
                        AND v_entry->'sample_id_hash' = 'null'::jsonb
                        AND v_entry->'sample_hash' = 'null'::jsonb
                        AND v_entry->'vector_hash' = 'null'::jsonb THEN
                        v_unlabeled_candidate_count := v_unlabeled_candidate_count + 1;
                    ELSIF coalesce(
                        v_entry->>'outcome_id_hash' ~ '^[0-9a-f]{{64}}$', false
                    ) IS NOT TRUE OR coalesce(
                        v_entry->>'outcome_hash' ~ '^[0-9a-f]{{64}}$', false
                    ) IS NOT TRUE OR coalesce(
                        v_entry->>'sample_id_hash' ~ '^[0-9a-f]{{64}}$', false
                    ) IS NOT TRUE OR coalesce(
                        v_entry->>'sample_hash' ~ '^[0-9a-f]{{64}}$', false
                    ) IS NOT TRUE OR coalesce(
                        v_entry->>'vector_hash' ~ '^[0-9a-f]{{64}}$', false
                    ) IS NOT TRUE THEN
                        RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_ENTRY_INVALID';
                    END IF;
                    v_candidate_count := v_candidate_count + 1;
                ELSIF v_entry->>'status' = 'no_candidate'
                    AND v_entry->'candidate_id_hash' = 'null'::jsonb
                    AND v_entry->'outcome_id_hash' = 'null'::jsonb
                    AND v_entry->'outcome_hash' = 'null'::jsonb
                    AND v_entry->'sample_id_hash' = 'null'::jsonb
                    AND v_entry->'sample_hash' = 'null'::jsonb
                    AND v_entry->'vector_hash' = 'null'::jsonb THEN
                    v_no_candidate_count := v_no_candidate_count + 1;
                ELSIF v_entry->>'status' = 'input_excluded'
                    AND v_entry->'candidate_id_hash' = 'null'::jsonb
                    AND v_entry->'outcome_id_hash' = 'null'::jsonb
                    AND v_entry->'outcome_hash' = 'null'::jsonb
                    AND v_entry->'sample_id_hash' = 'null'::jsonb
                    AND v_entry->'sample_hash' = 'null'::jsonb
                    AND v_entry->'vector_hash' = 'null'::jsonb THEN
                    v_input_excluded_count := v_input_excluded_count + 1;
                ELSE
                    RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_ENTRY_INVALID';
                END IF;
            END LOOP;
            SELECT count(*)::integer,
                   count(DISTINCT item->>'stable_id_hash')::integer,
                   count(DISTINCT item->>'candidate_id_hash') FILTER
                       (WHERE item->>'status' = 'candidate')::integer,
                   coalesce(jsonb_agg(item ORDER BY item->>'stable_id_hash'), '[]'::jsonb)
              INTO v_entry_count, v_distinct_event_count,
                   v_distinct_candidate_count, v_sorted_entries
              FROM jsonb_array_elements(v_entries) AS entry_rows(item);
            IF v_entry_count IS DISTINCT FROM v_distinct_event_count
                OR v_candidate_count IS DISTINCT FROM v_distinct_candidate_count
                OR v_sorted_entries IS DISTINCT FROM v_entries
                OR v_candidate_count IS DISTINCT FROM NEW.candidate_count
                OR v_no_candidate_count IS DISTINCT FROM NEW.no_candidate_count
                OR v_input_excluded_count IS DISTINCT FROM NEW.input_excluded_count
                OR v_unlabeled_candidate_count IS DISTINCT FROM
                    NEW.unlabeled_candidate_count
                OR v_entry_count + NEW.membership_mismatch_count IS DISTINCT FROM
                    NEW.expected_opportunity_count
                OR NEW.candidate_count + NEW.no_candidate_count + NEW.input_excluded_count
                    + NEW.membership_mismatch_count IS DISTINCT FROM
                    NEW.expected_opportunity_count
                OR NEW.unlabeled_candidate_count > NEW.candidate_count
                OR NEW.completeness_verified IS DISTINCT FROM (
                    NEW.input_excluded_count = 0 AND NEW.source_gap_count = 0
                    AND NEW.membership_mismatch_count = 0
                    AND NEW.unlabeled_candidate_count = 0
                    AND NEW.candidate_count + NEW.no_candidate_count =
                        NEW.expected_opportunity_count
                ) THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_COUNTS_INVALID';
            END IF;
            RETURN NEW;
        END $$"""
