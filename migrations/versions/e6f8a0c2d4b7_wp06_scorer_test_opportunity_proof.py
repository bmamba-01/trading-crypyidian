"""WP06: append-only identity-only TEST opportunity proof for scorer C.3."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6f8a0c2d4b7"
down_revision: str | None = "d5e7f9a1c3b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "learning_scorer_test_opportunity_proof"
BUILDER = "crypidian_candidate_builder"
EVALUATOR = "crypidian_learning_evaluator"
ADMIN = "crypidian_admin_web"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_run_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("normalized_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("fold_plan_hash", sa.String(64), nullable=False),
        sa.Column("fold_id", sa.String(64), nullable=False),
        sa.Column("approval_hash", sa.String(64), nullable=False),
        sa.Column("bundle_hash", sa.String(64), nullable=False),
        sa.Column("vector_schema_version", sa.String(128), nullable=False),
        sa.Column("vector_schema_hash", sa.String(64), nullable=False),
        sa.Column("test_membership_digest", sa.String(64), nullable=False),
        sa.Column("source_scope_hash", sa.String(64), nullable=False),
        sa.Column("expected_opportunity_count", sa.Integer(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("no_candidate_count", sa.Integer(), nullable=False),
        sa.Column("input_excluded_count", sa.Integer(), nullable=False),
        sa.Column("source_gap_count", sa.Integer(), nullable=False),
        sa.Column("membership_mismatch_count", sa.Integer(), nullable=False),
        sa.Column("unlabeled_candidate_count", sa.Integer(), nullable=False),
        sa.Column("completeness_verified", sa.Boolean(), nullable=False),
        sa.Column("proof_hash", sa.String(64), nullable=False),
        sa.Column("canonical_proof", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND fold_id ~ '^[0-9a-f]{64}$' AND approval_hash ~ '^[0-9a-f]{64}$' "
            "AND bundle_hash ~ '^[0-9a-f]{64}$' AND vector_schema_hash ~ '^[0-9a-f]{64}$' "
            "AND test_membership_digest ~ '^[0-9a-f]{64}$' "
            "AND source_scope_hash ~ '^[0-9a-f]{64}$' AND proof_hash ~ '^[0-9a-f]{64}$'",
            name="learning_scorer_test_proof_hashes_valid",
        ),
        sa.CheckConstraint(
            "length(btrim(vector_schema_version)) BETWEEN 1 AND 128 "
            "AND length(canonical_proof) > 0",
            name="learning_scorer_test_proof_payload_valid",
        ),
        sa.CheckConstraint(
            "expected_opportunity_count > 0 AND candidate_count >= 0 "
            "AND no_candidate_count >= 0 AND input_excluded_count >= 0 "
            "AND source_gap_count >= 0 AND membership_mismatch_count >= 0 "
            "AND unlabeled_candidate_count >= 0 "
            "AND candidate_count + no_candidate_count + input_excluded_count "
            "+ membership_mismatch_count = expected_opportunity_count "
            "AND unlabeled_candidate_count <= candidate_count",
            name="learning_scorer_test_proof_counts_valid",
        ),
        sa.CheckConstraint(
            "completeness_verified = (input_excluded_count = 0 AND source_gap_count = 0 "
            "AND membership_mismatch_count = 0 AND unlabeled_candidate_count = 0 "
            "AND candidate_count + no_candidate_count = expected_opportunity_count)",
            name="learning_scorer_test_proof_completeness_valid",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["learning_training_attempt.workspace_id", "learning_training_attempt.id"],
            name="fk_learning_scorer_test_proof_attempt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["learning_scorer_artifact.workspace_id", "learning_scorer_artifact.attempt_id"],
            name="fk_learning_scorer_test_proof_artifact",
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
            name="fk_learning_scorer_test_proof_dataset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "manifest_hash"],
            ["learning_split_approval.workspace_id", "learning_split_approval.manifest_hash"],
            name="fk_learning_scorer_test_proof_approval",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_scorer_test_opportunity_proof")),
        sa.UniqueConstraint(
            "workspace_id",
            "attempt_id",
            "fold_id",
            name="uq_learning_scorer_test_proof_attempt_fold",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "evaluation_run_id",
            name="uq_learning_scorer_test_proof_evaluation_run",
        ),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_learning_scorer_test_proof_workspace_id"
        ),
    )
    op.create_index(
        "ix_learning_scorer_test_proof_workspace_created",
        TABLE,
        ["workspace_id", "created_at"],
    )
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {TABLE}_builder_select ON public.{TABLE} "
        f"FOR SELECT TO {BUILDER} USING ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY {TABLE}_builder_insert ON public.{TABLE} "
        f"FOR INSERT TO {BUILDER} WITH CHECK ({WORKSPACE})"
    )
    for role in (EVALUATOR, ADMIN):
        op.execute(
            f"CREATE POLICY {TABLE}_{role}_select ON public.{TABLE} "
            f"FOR SELECT TO {role} USING ({WORKSPACE})"
        )
    op.execute(
        f"REVOKE ALL ON TABLE public.{TABLE} FROM PUBLIC, crypidian_app, "
        "crypidian_trading_runtime, crypidian_ingestion_worker, "
        "crypidian_execution_gateway, crypidian_backtest_worker, "
        "crypidian_signal_worker, crypidian_market_signal_worker, "
        "crypidian_replay_writer, crypidian_trainer_worker, "
        "crypidian_learning_batch_reader, crypidian_learning_evaluation_reader, "
        "crypidian_learning_shadow_observer, "
        f"{BUILDER}, {EVALUATOR}, {ADMIN}"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{TABLE} TO {BUILDER}")
    op.execute(f"GRANT SELECT ON TABLE public.{TABLE} TO {EVALUATOR}, {ADMIN}")
    op.execute("""CREATE FUNCTION public.wp06_scorer_test_proof_canonical_json(value jsonb)
        RETURNS text LANGUAGE plpgsql IMMUTABLE STRICT
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            v_type text;
            v_result text;
        BEGIN
            v_type := jsonb_typeof(value);
            IF v_type = 'object' THEN
                SELECT '{' || coalesce(string_agg(
                    to_json(item.key)::text || ':' ||
                    public.wp06_scorer_test_proof_canonical_json(item.value),
                    ',' ORDER BY item.key COLLATE "C"
                ), '') || '}'
                  INTO v_result
                  FROM jsonb_each(value) AS item(key, value);
                RETURN v_result;
            ELSIF v_type = 'array' THEN
                SELECT '[' || coalesce(string_agg(
                    public.wp06_scorer_test_proof_canonical_json(item.value),
                    ',' ORDER BY item.ordinality
                ), '') || ']'
                  INTO v_result
                  FROM jsonb_array_elements(value) WITH ORDINALITY
                       AS item(value, ordinality);
                RETURN v_result;
            END IF;
            RETURN value::text;
        END $$""")
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_scorer_test_proof_canonical_json(jsonb) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.wp06_scorer_test_proof_canonical_json(jsonb) "
        f"TO {BUILDER}"
    )
    op.execute(f"""CREATE FUNCTION public.validate_learning_scorer_test_opportunity_proof()
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
                        v_entry->>'stable_id_hash' ~ '^[0-9a-f]{64}$', false
                    ) IS NOT TRUE THEN
                    RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_ENTRY_INVALID';
                END IF;
                IF v_entry->>'status' = 'candidate' THEN
                    IF coalesce(
                        v_entry->>'candidate_id_hash' ~ '^[0-9a-f]{64}$', false
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
                        v_entry->>'outcome_id_hash' ~ '^[0-9a-f]{64}$', false
                    ) IS NOT TRUE OR coalesce(
                        v_entry->>'outcome_hash' ~ '^[0-9a-f]{64}$', false
                    ) IS NOT TRUE OR coalesce(
                        v_entry->>'sample_id_hash' ~ '^[0-9a-f]{64}$', false
                    ) IS NOT TRUE OR coalesce(
                        v_entry->>'sample_hash' ~ '^[0-9a-f]{64}$', false
                    ) IS NOT TRUE OR coalesce(
                        v_entry->>'vector_hash' ~ '^[0-9a-f]{64}$', false
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
        END $$""")
    op.execute(
        "REVOKE ALL ON FUNCTION public.validate_learning_scorer_test_opportunity_proof() "
        "FROM PUBLIC"
    )
    op.execute(
        f"CREATE TRIGGER {TABLE}_validate BEFORE INSERT ON public.{TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION public.validate_learning_scorer_test_opportunity_proof()"
    )
    op.execute("""CREATE FUNCTION public.reject_learning_scorer_test_proof_mutation()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$ BEGIN
        RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_IMMUTABLE'; END $$""")
    op.execute(
        "REVOKE ALL ON FUNCTION public.reject_learning_scorer_test_proof_mutation() FROM PUBLIC"
    )
    op.execute(
        f"CREATE TRIGGER {TABLE}_immutable BEFORE UPDATE OR DELETE ON public.{TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION public.reject_learning_scorer_test_proof_mutation()"
    )


def downgrade() -> None:
    op.execute(f"""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.{TABLE}) THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_DOWNGRADE_REQUIRES_EMPTY_TABLE';
        END IF;
    END $$""")
    op.execute(f"DROP TRIGGER {TABLE}_immutable ON public.{TABLE}")
    op.execute(f"DROP TRIGGER {TABLE}_validate ON public.{TABLE}")
    op.execute(f"DROP POLICY {TABLE}_{ADMIN}_select ON public.{TABLE}")
    op.execute(f"DROP POLICY {TABLE}_{EVALUATOR}_select ON public.{TABLE}")
    op.execute(f"DROP POLICY {TABLE}_builder_insert ON public.{TABLE}")
    op.execute(f"DROP POLICY {TABLE}_builder_select ON public.{TABLE}")
    op.execute(f"ALTER TABLE public.{TABLE} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION public.reject_learning_scorer_test_proof_mutation()")
    op.execute("DROP FUNCTION public.validate_learning_scorer_test_opportunity_proof()")
    op.execute("DROP FUNCTION public.wp06_scorer_test_proof_canonical_json(jsonb)")
    op.drop_index("ix_learning_scorer_test_proof_workspace_created", table_name=TABLE)
    op.drop_table(TABLE)
