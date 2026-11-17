"""Thêm writer giới hạn cho report PAPER sau khi thu hồi INSERT trực tiếp."""

# Ruff: SQL cố định để ACL và trigger boundary được review trực tiếp.
# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e2f4a6c8b0d7"
down_revision: str | Sequence[str] | None = "e2f4a6c8b0d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
WRITER = "crypidian_learning_paper_performance_report_writer"
TABLE = "public.learning_paper_performance_report"
FUNCTION = (
    "public.wp06_insert_paper_performance_report("
    "uuid,uuid,uuid,uuid,text,text,text,text,text,timestamptz,text,uuid,text,jsonb,text)"
)
REPORT_COLUMNS = (
    "id,workspace_id,bot_id,baseline_report_id,baseline_report_hash,policy_version,"
    "policy_hash,target_fingerprint,membership_digest,as_of,status,idempotency_key,"
    "report_hash,report,canonical_report"
)
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"


def _create_writer_role() -> None:
    op.execute(f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='{WRITER}') THEN
            CREATE ROLE {WRITER} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
                NOCREATEDB NOCREATEROLE NOREPLICATION;
        ELSIF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
             WHERE role.rolname='{WRITER}'
               AND (role.rolcanlogin OR role.rolsuper OR role.rolbypassrls OR role.rolinherit
                    OR role.rolcreatedb OR role.rolcreaterole OR role.rolreplication
                    OR EXISTS (
                        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
                         WHERE membership.roleid=role.oid OR membership.member=role.oid))
        ) THEN
            RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_REPORT_WRITER_ROLE_UNSAFE';
        END IF;
    END $$""")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {WRITER}")


def _create_writer() -> None:
    op.execute(f"""
        CREATE FUNCTION {FUNCTION.split("(")[0]}(
            target_id uuid,
            target_workspace uuid,
            target_bot uuid,
            target_baseline_report_id uuid,
            target_baseline_report_hash text,
            target_policy_version text,
            target_policy_hash text,
            target_fingerprint text,
            target_membership_digest text,
            target_as_of timestamptz,
            target_status text,
            target_idempotency_key uuid,
            target_report_hash text,
            target_report jsonb,
            target_canonical_report text
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            aggregate_row record;
            aggregate_count integer := 0;
            report_cohort jsonb;
            cohort_index integer;
            cohort_status text;
            cohort_reasons text[];
            overall_reasons text[] := ARRAY[]::text[];
            cohort_has_support boolean;
            assessment_status text;
            older_membership_digest text;
            newer_membership_digest text;
            current_rights_digest text;
            expected_policy_hash text;
            policy_document jsonb;
            combined_membership jsonb;
            combined_membership_digest text;
        BEGIN
            IF session_user <> '{ADMIN}'
               OR target_id IS NULL OR target_workspace IS NULL OR target_bot IS NULL
               OR target_baseline_report_id IS NULL OR target_idempotency_key IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed'
               OR target_as_of IS DISTINCT FROM pg_catalog.transaction_timestamp()
               OR target_policy_version IS DISTINCT FROM 'wp06-paper-performance-v1'
               OR target_status NOT IN ('healthy','degraded','inconclusive') THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_REPORT_WRITER_SCOPE_DENIED';
            END IF;
            policy_document := pg_catalog.jsonb_build_object(
                'class_order', pg_catalog.jsonb_build_array(-1, 0, 1),
                'cohort_days', 30,
                'log_loss_epsilon', '1E-12',
                'maturity_days', 30,
                'maximum_brier_regression', '0.01',
                'maximum_log_loss_regression', '0.01',
                'minimum_class_support', 100,
                'minimum_coverage', '0.80',
                'minimum_outcomes', 1000,
                'version', 'wp06-paper-performance-v1'
            );
            expected_policy_hash := pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(
                    public.wp06_license_canonical_json(policy_document), 'UTF8')),
                'hex');
            IF target_report IS NULL
               OR target_canonical_report IS NULL
               OR target_policy_hash IS DISTINCT FROM expected_policy_hash
               OR public.wp06_license_canonical_json(target_report)
                    IS DISTINCT FROM target_canonical_report
               OR target_report_hash IS DISTINCT FROM pg_catalog.encode(
                    pg_catalog.sha256(pg_catalog.convert_to(target_canonical_report, 'UTF8')),
                    'hex') THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_REPORT_WRITER_PAYLOAD_INVALID';
            END IF;
            IF ARRAY(SELECT object_key FROM pg_catalog.jsonb_object_keys(target_report)
                     AS item(object_key) ORDER BY object_key COLLATE "C") IS DISTINCT FROM
                   ARRAY['as_of','automation','baseline','cohorts','policy_hash',
                         'policy_version','reasons','schema_version','scope',
                         'selection_bias_note','status','target_fingerprint','thresholds']::text[]
               OR target_report->>'schema_version'
                    IS DISTINCT FROM 'crypidian.learning.paper-performance.v1'
               OR target_report->>'scope' IS DISTINCT FROM 'PAPER_ONLY_DIAGNOSTIC'
               OR target_report->>'automation' IS DISTINCT FROM 'NO_STATE_CHANGE'
               OR target_report->>'selection_bias_note' IS DISTINCT FROM
                    'Chỉ gồm intent PAPER được scorer duyệt, risk chấp thuận và có '
                    'giao dịch đóng đúng hạn; không suy diễn kết quả cho quyết định '
                    'không vào lệnh hoặc lợi nhuận tương lai.'
               OR target_report->>'policy_version' IS DISTINCT FROM target_policy_version
               OR target_report->>'policy_hash' IS DISTINCT FROM target_policy_hash
               OR target_report->>'target_fingerprint' IS DISTINCT FROM target_fingerprint
               OR target_report->>'as_of' IS NULL
               OR (target_report->>'as_of')::timestamptz IS DISTINCT FROM target_as_of
               OR target_report->'baseline' IS DISTINCT FROM pg_catalog.jsonb_build_object(
                    'description', 'TEST baseline đã đóng băng',
                    'report_hash', target_baseline_report_hash)
               OR target_report->'thresholds' IS DISTINCT FROM pg_catalog.jsonb_build_object(
                    'cohort_days', 30, 'maturity_days', 30, 'minimum_outcomes', 1000,
                    'minimum_class_support', 100, 'minimum_coverage', '0.80',
                    'maximum_brier_regression', '0.01',
                    'maximum_log_loss_regression', '0.01')
               OR pg_catalog.jsonb_typeof(target_report->'cohorts') IS DISTINCT FROM 'array'
               OR pg_catalog.jsonb_array_length(target_report->'cohorts') <> 2 THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_REPORT_WRITER_PAYLOAD_INVALID';
            END IF;

            FOR aggregate_row IN
                SELECT * FROM public.wp06_aggregate_paper_performance(
                    target_workspace, target_bot, target_fingerprint,
                    target_baseline_report_id, target_baseline_report_hash, target_as_of
                ) AS aggregate
                ORDER BY CASE aggregate.cohort_name WHEN 'older' THEN 0 ELSE 1 END
            LOOP
                aggregate_count := aggregate_count + 1;
                cohort_index := aggregate_count - 1;
                IF aggregate_count > 2 OR aggregate_row.target_fingerprint
                        IS DISTINCT FROM target_fingerprint
                   OR aggregate_row.baseline_report_id IS DISTINCT FROM target_baseline_report_id
                   OR aggregate_row.baseline_report_hash IS DISTINCT FROM target_baseline_report_hash
                   OR aggregate_row.cohort_name IS DISTINCT FROM
                        (CASE cohort_index WHEN 0 THEN 'older' ELSE 'newer' END)
                   OR (aggregate_count = 1 AND aggregate_row.rights_digest IS NULL)
                   OR (aggregate_count = 2 AND aggregate_row.rights_digest
                        IS DISTINCT FROM current_rights_digest) THEN
                    RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_REPORT_AGGREGATE_MISMATCH';
                END IF;
                IF aggregate_count = 1 THEN
                    current_rights_digest := aggregate_row.rights_digest;
                    older_membership_digest := aggregate_row.membership_digest;
                ELSE
                    newer_membership_digest := aggregate_row.membership_digest;
                END IF;

                report_cohort := target_report->'cohorts'->cohort_index;
                IF ARRAY(SELECT object_key FROM pg_catalog.jsonb_object_keys(report_cohort)
                         AS item(object_key) ORDER BY object_key COLLATE "C") IS DISTINCT FROM
                       ARRAY['baseline_brier','baseline_log_loss','brier_regression',
                             'class_support','coverage','covered_outcome_count',
                             'eligible_decision_count','log_loss_regression',
                             'membership_digest','observed_brier','observed_log_loss',
                             'reasons','status','window_end','window_start']::text[]
                   OR report_cohort->>'window_start' IS NULL
                   OR (report_cohort->>'window_start')::timestamptz
                        IS DISTINCT FROM aggregate_row.window_start
                   OR report_cohort->>'window_end' IS NULL
                   OR (report_cohort->>'window_end')::timestamptz
                        IS DISTINCT FROM aggregate_row.window_end
                   OR report_cohort->'eligible_decision_count'
                        IS DISTINCT FROM pg_catalog.to_jsonb(aggregate_row.eligible_decision_count)
                   OR report_cohort->'covered_outcome_count'
                        IS DISTINCT FROM pg_catalog.to_jsonb(aggregate_row.covered_outcome_count)
                   OR report_cohort->'class_support' IS DISTINCT FROM pg_catalog.jsonb_build_array(
                        pg_catalog.jsonb_build_object('class', -1,
                            'count', aggregate_row.class_minus_one_count),
                        pg_catalog.jsonb_build_object('class', 0,
                            'count', aggregate_row.class_zero_count),
                        pg_catalog.jsonb_build_object('class', 1,
                            'count', aggregate_row.class_plus_one_count))
                   OR report_cohort->>'membership_digest'
                        IS DISTINCT FROM aggregate_row.membership_digest
                   OR pg_catalog.jsonb_typeof(report_cohort->'coverage') IS DISTINCT FROM 'string'
                   -- Sai số này chỉ bù phép chia NUMERIC so với Decimal của Python.
                   -- Status luôn tính lại từ aggregate gốc và ngưỡng chính xác bên dưới.
                   OR (aggregate_row.eligible_decision_count = 0 AND
                        (report_cohort->>'coverage')::numeric <> 0)
                   OR (aggregate_row.eligible_decision_count > 0 AND
                        abs((report_cohort->>'coverage')::numeric -
                            aggregate_row.covered_outcome_count::numeric /
                                NULLIF(aggregate_row.eligible_decision_count, 0))
                            > 0.000000000000001)
                   OR pg_catalog.jsonb_typeof(report_cohort->'baseline_brier')
                        IS DISTINCT FROM 'string'
                   OR (report_cohort->>'baseline_brier')::numeric
                        IS DISTINCT FROM aggregate_row.baseline_brier
                   OR pg_catalog.jsonb_typeof(report_cohort->'baseline_log_loss')
                        IS DISTINCT FROM 'string'
                   OR (report_cohort->>'baseline_log_loss')::numeric
                        IS DISTINCT FROM aggregate_row.baseline_log_loss
                   OR (aggregate_row.observed_brier IS NULL AND
                        pg_catalog.jsonb_typeof(report_cohort->'observed_brier')
                            IS DISTINCT FROM 'null')
                   OR (aggregate_row.observed_brier IS NOT NULL AND (
                        pg_catalog.jsonb_typeof(report_cohort->'observed_brier')
                            IS DISTINCT FROM 'string'
                        OR (report_cohort->>'observed_brier')::numeric
                            IS DISTINCT FROM aggregate_row.observed_brier))
                   OR (aggregate_row.observed_log_loss IS NULL AND
                        pg_catalog.jsonb_typeof(report_cohort->'observed_log_loss')
                            IS DISTINCT FROM 'null')
                   OR (aggregate_row.observed_log_loss IS NOT NULL AND (
                        pg_catalog.jsonb_typeof(report_cohort->'observed_log_loss')
                            IS DISTINCT FROM 'string'
                        OR (report_cohort->>'observed_log_loss')::numeric
                            IS DISTINCT FROM aggregate_row.observed_log_loss)) THEN
                    RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_REPORT_AGGREGATE_MISMATCH';
                END IF;

                cohort_reasons := ARRAY[]::text[];
                IF aggregate_row.covered_outcome_count < 1000 THEN
                    cohort_reasons := pg_catalog.array_append(
                        cohort_reasons, 'minimum_outcomes_not_met');
                END IF;
                cohort_has_support := aggregate_row.class_minus_one_count >= 100
                    AND aggregate_row.class_zero_count >= 100
                    AND aggregate_row.class_plus_one_count >= 100;
                IF NOT cohort_has_support THEN
                    cohort_reasons := pg_catalog.array_append(
                        cohort_reasons, 'minimum_class_support_not_met');
                END IF;
                IF aggregate_row.eligible_decision_count = 0
                   OR aggregate_row.covered_outcome_count::numeric * 5 <
                        aggregate_row.eligible_decision_count::numeric * 4 THEN
                    cohort_reasons := pg_catalog.array_append(
                        cohort_reasons, 'minimum_mature_coverage_not_met');
                END IF;
                cohort_status := CASE
                    WHEN pg_catalog.cardinality(cohort_reasons) > 0 THEN 'inconclusive'
                    WHEN aggregate_row.observed_brier - aggregate_row.baseline_brier > 0.01
                     AND aggregate_row.observed_log_loss - aggregate_row.baseline_log_loss > 0.01
                        THEN 'degraded'
                    ELSE 'healthy'
                END;
                IF report_cohort->>'status' IS DISTINCT FROM cohort_status
                   OR report_cohort->'reasons' IS DISTINCT FROM
                        pg_catalog.to_jsonb(cohort_reasons)
                   OR (aggregate_row.observed_brier IS NULL AND
                        report_cohort->'brier_regression' IS DISTINCT FROM 'null'::jsonb)
                   OR (aggregate_row.observed_log_loss IS NULL AND
                        report_cohort->'log_loss_regression' IS DISTINCT FROM 'null'::jsonb)
                   OR (aggregate_row.observed_brier IS NOT NULL AND (
                        pg_catalog.jsonb_typeof(report_cohort->'brier_regression')
                            IS DISTINCT FROM 'string'
                        OR abs((report_cohort->>'brier_regression')::numeric -
                            (aggregate_row.observed_brier - aggregate_row.baseline_brier))
                            > 0.000000000000001))
                   OR (aggregate_row.observed_log_loss IS NOT NULL AND (
                        pg_catalog.jsonb_typeof(report_cohort->'log_loss_regression')
                            IS DISTINCT FROM 'string'
                        OR abs((report_cohort->>'log_loss_regression')::numeric -
                            (aggregate_row.observed_log_loss - aggregate_row.baseline_log_loss))
                            > 0.000000000000001)) THEN
                    RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_REPORT_AGGREGATE_MISMATCH';
                END IF;
                IF pg_catalog.cardinality(cohort_reasons) > 0 THEN
                    overall_reasons := pg_catalog.array_cat(
                        overall_reasons,
                        ARRAY(SELECT 'cohort_' || cohort_index::text || '_' || reason
                              FROM pg_catalog.unnest(cohort_reasons)
                                   WITH ORDINALITY AS item(reason, position)
                              ORDER BY item.position));
                END IF;
            END LOOP;
            IF aggregate_count <> 2 OR older_membership_digest IS NULL
               OR newer_membership_digest IS NULL OR current_rights_digest IS NULL THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_REPORT_AGGREGATE_MISMATCH';
            END IF;
            combined_membership := pg_catalog.jsonb_build_object(
                'cohort_membership_digests',
                    pg_catalog.jsonb_build_array(older_membership_digest, newer_membership_digest),
                'rights_digest', current_rights_digest,
                'schema_version', 'crypidian.wp06.paper-performance-membership.v2');
            combined_membership_digest := pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(
                    public.wp06_license_canonical_json(combined_membership), 'UTF8')),
                'hex');
            IF target_membership_digest IS DISTINCT FROM combined_membership_digest THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_REPORT_AGGREGATE_MISMATCH';
            END IF;
            IF pg_catalog.cardinality(overall_reasons) > 0 THEN
                IF (target_report->'cohorts'->0->>'status') = 'degraded'
                   AND (target_report->'cohorts'->1->>'status') = 'degraded' THEN
                    RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_REPORT_AGGREGATE_MISMATCH';
                END IF;
                assessment_status := 'inconclusive';
            ELSIF (target_report->'cohorts'->0->>'status') = 'degraded'
              AND (target_report->'cohorts'->1->>'status') = 'degraded' THEN
                assessment_status := 'degraded';
                overall_reasons := ARRAY['joint_metric_regression_in_both_cohorts'];
            ELSE
                assessment_status := 'healthy';
            END IF;
            IF target_status IS DISTINCT FROM assessment_status
               OR target_report->>'status' IS DISTINCT FROM assessment_status
               OR target_report->'reasons' IS DISTINCT FROM pg_catalog.to_jsonb(overall_reasons) THEN
                RAISE EXCEPTION 'WP06_PAPER_PERFORMANCE_REPORT_AGGREGATE_MISMATCH';
            END IF;
            INSERT INTO {TABLE} (
                {REPORT_COLUMNS}
            ) VALUES (
                target_id, target_workspace, target_bot, target_baseline_report_id,
                target_baseline_report_hash, target_policy_version, target_policy_hash,
                target_fingerprint, target_membership_digest, target_as_of, target_status,
                target_idempotency_key, target_report_hash, target_report,
                target_canonical_report
            );
        END; $$
    """)
    op.execute(f"GRANT CREATE ON SCHEMA public TO {WRITER}")
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO {WRITER}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {WRITER}")
    op.execute(
        f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC, crypidian_app, "
        f"crypidian_trading_runtime, crypidian_learning_paper_performance_evaluator, {WRITER}"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {ADMIN}")


def upgrade() -> None:
    _create_writer_role()
    op.execute(f"""
        CREATE POLICY wp06_paper_performance_report_writer_insert
        ON {TABLE} FOR INSERT TO {WRITER} WITH CHECK ({WORKSPACE})
    """)
    op.execute(f"GRANT INSERT ({REPORT_COLUMNS}) ON {TABLE} TO {WRITER}")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.wp06_license_canonical_json(jsonb) TO {WRITER}")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_aggregate_paper_performance("
        "uuid,uuid,text,uuid,text,timestamptz) TO "
        f"{WRITER}"
    )
    _create_writer()


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {FUNCTION}")
    op.execute(f"DROP POLICY wp06_paper_performance_report_writer_insert ON {TABLE}")
    op.execute(f"REVOKE INSERT ({REPORT_COLUMNS}) ON {TABLE} FROM {WRITER}")
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION public.wp06_license_canonical_json(jsonb) FROM {WRITER}"
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_aggregate_paper_performance("
        "uuid,uuid,text,uuid,text,timestamptz) FROM "
        f"{WRITER}"
    )
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {WRITER}")
    op.execute(f"DROP ROLE {WRITER}")
