"""Probe PostgreSQL ACL và denial của gateway khi chưa có scorer target."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Connection
from sqlalchemy.exc import DBAPIError

from trading_bot.domain.learning.paper_performance import PaperPerformancePolicy

EVALUATOR = "crypidian_learning_paper_performance_evaluator"
UNSCOPED_SIGNATURE = (
    "public.wp06_aggregate_paper_performance_unscoped("
    "uuid,uuid,text,uuid,text,timestamp with time zone)"
)


def _assert_aggregate_sql_contract(connection: Connection, workspace_id: UUID) -> None:
    """Chạy SQL cài trong gateway dưới role evaluator trên DB chưa có active target."""
    source = connection.execute(
        text("SELECT prosrc FROM pg_catalog.pg_proc WHERE oid=CAST(:signature AS regprocedure)"),
        {"signature": UNSCOPED_SIGNATURE},
    ).scalar_one()
    baseline = re.search(r"(SELECT report\..+?) INTO baseline_row(\s+FROM .+?);", source, re.S)
    metric = re.search(
        r"metric_rows AS \(\s*(SELECT scored\.\*.+?FROM scored WHERE outcome_label IS NOT NULL)",
        source,
        re.S,
    )
    assert baseline is not None
    assert metric is not None
    baseline_sql = re.sub(
        r"\b(target_workspace|expected_baseline_report_id)\b",
        r":\1",
        "".join(baseline.groups()),
    )
    sql_errors = {}
    with connection.begin_nested():
        connection.execute(text(f"SET LOCAL ROLE {EVALUATOR}"))
        connection.execute(text("SET LOCAL search_path = pg_catalog, pg_temp"))
        connection.execute(
            text("SELECT pg_catalog.set_config('app.workspace_id', :workspace, true)"),
            {"workspace": str(workspace_id)},
        )
        for name, query, params in (
            (
                "baseline",
                baseline_sql,
                {"target_workspace": workspace_id, "expected_baseline_report_id": uuid4()},
            ),
            ("event_status", "SELECT evaluation_status FROM public.learning_score_event", {}),
            (
                "log_loss",
                "WITH scored(outcome_label, p_minus, p_zero, p_plus) AS "
                "(VALUES (-1, 0.75, 0.20, 0.05), (1, 0.75, 0.25, 0.00), "
                "(NULL, 0.75, 0.25, 0.00)) "
                "SELECT round(brier_value, 6), round(log_loss_value, 6) "
                f"FROM ({metric[1]}) AS metrics ORDER BY outcome_label",
                {},
            ),
        ):
            try:
                with connection.begin_nested():
                    rows = connection.execute(text(query), params).all()
            except DBAPIError as error:
                sql_errors[name] = (error.orig.sqlstate, error.orig.diag.message_primary)
                continue
            if name == "log_loss":
                assert rows == [
                    (Decimal("0.105000"), Decimal("0.287682")),
                    (Decimal("1.625000"), Decimal("27.631021")),
                ]
            else:
                assert rows == []
        connection.execute(text("RESET ROLE"))
    assert not sql_errors, sql_errors

    expected_columns = {
        "learning_scorer_test_evaluation_report": set(
            "workspace_id id report_hash report canonical_report evaluation_status "
            "scorer_artifact_hash base_artifact_hash policy_hash".split()
        ),
        "learning_score_event": set(
            "workspace_id bot_id bot_decision_id bot_decision_hash final_status candidate_id "
            "mode deployment_event_id deployment_revision registry_id registry_revision "
            "active_pointer_version scorer_artifact_id scorer_artifact_hash base_artifact_hash "
            "evaluation_report_id evaluation_report_hash evaluation_status report_scope_hash "
            "proof_hash policy_id policy_hash feature_schema_hash source_rights_fingerprint "
            "decision class_probabilities event_hash".split()
        ),
    }
    for table, columns in expected_columns.items():
        privileges = {"role": EVALUATOR, "relation": f"public.{table}"}
        assert (
            connection.execute(
                text("SELECT pg_catalog.has_table_privilege(:role, :relation, 'SELECT')"),
                privileges,
            ).scalar_one()
            is False
        )
        actual = connection.execute(
            text(
                "SELECT a.attname FROM pg_catalog.pg_attribute AS a "
                "WHERE a.attrelid=CAST(:relation AS regclass) AND a.attnum > 0 "
                "AND NOT a.attisdropped AND pg_catalog.has_column_privilege("
                ":role, a.attrelid, a.attnum, 'SELECT')"
            ),
            privileges,
        ).scalars()
        assert set(actual) == columns


def _url(user: str, password: str) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=user,
        password=password,
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
    )


def main() -> None:
    workspace_id = UUID(os.environ["CRYPIDIAN_E2E_WORKSPACE_ID"])
    bot_id = uuid4()
    super_engine = create_engine(
        _url(
            os.environ["TRADING_BOT_DATABASE_USER"],
            Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip(),
        )
    )
    admin_engine = create_engine(
        _url(
            "crypidian_admin_web",
            Path("/run/secrets/database_admin_web_password").read_text().strip(),
        )
    )
    try:
        with super_engine.begin() as connection:
            _assert_aggregate_sql_contract(connection, workspace_id)
        with super_engine.begin() as connection:
            connection.execute(text("SET LOCAL session_replication_role = replica"))
            connection.execute(
                text(
                    "INSERT INTO public.bot_instance "
                    "(id, workspace_id, trading_account_id, name, status) "
                    "VALUES (:id, :workspace_id, :account_id, "
                    "'WP06 diagnostic fixture', 'DISABLED')"
                ),
                {
                    "id": bot_id,
                    "workspace_id": workspace_id,
                    "account_id": uuid4(),
                },
            )
            acl = (
                connection.execute(
                    text(
                        """
                    SELECT NOT role.rolcanlogin AND NOT role.rolinherit
                               AND NOT role.rolsuper AND NOT role.rolbypassrls
                               AND NOT EXISTS (
                                   SELECT 1 FROM pg_catalog.pg_auth_members AS membership
                                   WHERE membership.roleid=role.oid
                                      OR membership.member=role.oid
                               ) AS evaluator_isolated,
                           pg_catalog.has_column_privilege(
                               role.rolname, 'public.trade', 'net_pnl', 'SELECT'
                           ) AND NOT pg_catalog.has_table_privilege(
                               role.rolname, 'public.trade', 'SELECT'
                           ) AS trade_column_scoped,
                           NOT pg_catalog.has_table_privilege(
                               'crypidian_admin_web',
                               'public.learning_paper_performance_report', 'SELECT'
                           ) AND NOT pg_catalog.has_table_privilege(
                               'crypidian_admin_web',
                               'public.learning_paper_performance_report', 'INSERT'
                           ) AND NOT EXISTS (
                               SELECT 1
                                 FROM pg_catalog.pg_attribute AS column_info
                                WHERE column_info.attrelid=report.oid
                                  AND column_info.attnum > 0
                                  AND NOT column_info.attisdropped
                                  AND pg_catalog.has_column_privilege(
                                      'crypidian_admin_web', report.oid,
                                      column_info.attname, 'INSERT')
                           ) AS report_table_access_denied,
                           pg_catalog.has_column_privilege(
                               'crypidian_admin_web', report.oid, 'workspace_id', 'SELECT'
                           ) AND pg_catalog.has_column_privilege(
                               'crypidian_admin_web', report.oid, 'id', 'SELECT'
                           ) AND pg_catalog.has_column_privilege(
                               'crypidian_admin_web', report.oid, 'bot_id', 'SELECT'
                           ) AND pg_catalog.has_column_privilege(
                               'crypidian_admin_web', report.oid, 'idempotency_key', 'SELECT'
                           ) AND pg_catalog.has_column_privilege(
                               'crypidian_admin_web', report.oid, 'target_fingerprint', 'SELECT'
                           ) AND pg_catalog.has_column_privilege(
                               'crypidian_admin_web', report.oid, 'policy_hash', 'SELECT'
                           ) AND pg_catalog.has_column_privilege(
                               'crypidian_admin_web', report.oid, 'as_of', 'SELECT'
                           ) AND pg_catalog.has_column_privilege(
                               'crypidian_admin_web', report.oid, 'membership_digest', 'SELECT'
                           ) AND pg_catalog.has_column_privilege(
                               'crypidian_admin_web', report.oid, 'created_at', 'SELECT'
                           ) AS report_metadata_select,
                           NOT pg_catalog.has_column_privilege(
                               'crypidian_admin_web', report.oid, 'report', 'SELECT'
                           ) AND NOT pg_catalog.has_column_privilege(
                               'crypidian_admin_web', report.oid, 'canonical_report', 'SELECT'
                           ) AND NOT pg_catalog.has_column_privilege(
                               'crypidian_admin_web', report.oid, 'report_hash', 'SELECT'
                           ) AS report_payload_select_denied,
                           report.relrowsecurity AND report.relforcerowsecurity AS report_rls
                      FROM pg_catalog.pg_roles AS role
                      JOIN pg_catalog.pg_class AS report
                        ON report.oid='public.learning_paper_performance_report'::regclass
                     WHERE role.rolname='crypidian_learning_paper_performance_evaluator'
                    """
                    )
                )
                .mappings()
                .one()
            )
            writer_acl = (
                connection.execute(
                    text(
                        """
                    SELECT NOT writer.rolcanlogin AND NOT writer.rolinherit
                               AND NOT writer.rolsuper AND NOT writer.rolbypassrls
                               AND NOT writer.rolcreatedb AND NOT writer.rolcreaterole
                               AND NOT writer.rolreplication
                               AND NOT pg_catalog.has_schema_privilege(
                                   writer.rolname, 'public', 'CREATE'
                               )
                               AND NOT EXISTS (
                                   SELECT 1 FROM pg_catalog.pg_auth_members AS membership
                                   WHERE membership.roleid=writer.oid
                                      OR membership.member=writer.oid
                               ) AS role_isolated,
                           pg_catalog.has_column_privilege(
                               writer.rolname, report.oid, 'report', 'INSERT'
                           ) AND pg_catalog.has_column_privilege(
                               writer.rolname, report.oid, 'canonical_report', 'INSERT'
                           ) AND NOT pg_catalog.has_column_privilege(
                               writer.rolname, report.oid, 'created_at', 'INSERT'
                           ) AND NOT pg_catalog.has_table_privilege(
                               writer.rolname, report.oid, 'SELECT'
                           ) AND NOT pg_catalog.has_table_privilege(
                               writer.rolname, report.oid, 'UPDATE'
                           ) AND NOT pg_catalog.has_table_privilege(
                               writer.rolname, report.oid, 'DELETE'
                           ) AS insert_columns_only,
                           pg_catalog.has_function_privilege(
                               'crypidian_admin_web', write_function.oid, 'EXECUTE'
                           ) AND NOT pg_catalog.has_function_privilege(
                               'crypidian_app', write_function.oid, 'EXECUTE'
                           ) AND NOT pg_catalog.has_function_privilege(
                               'crypidian_trading_runtime', write_function.oid, 'EXECUTE'
                           ) AND NOT pg_catalog.has_function_privilege(
                               'crypidian_learning_paper_performance_evaluator',
                               write_function.oid, 'EXECUTE'
                           ) AND NOT EXISTS (
                               SELECT 1
                                 FROM pg_catalog.aclexplode(write_function.proacl) AS acl
                                WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE'
                           ) AS function_execute_scoped,
                           pg_catalog.has_function_privilege(
                               writer.rolname,
                               'public.wp06_aggregate_paper_performance'
                               '(uuid,uuid,text,uuid,text,timestamp with time zone)',
                               'EXECUTE') AS aggregate_execute,
                           pg_catalog.strpos(
                               pg_catalog.pg_get_functiondef(write_function.oid),
                               'NULLIF(aggregate_row.eligible_decision_count, 0)'
                           ) > 0 AS zero_denominator_safe,
                           pg_catalog.strpos(
                               pg_catalog.pg_get_functiondef(write_function.oid),
                               $g$(report_cohort->>'coverage')::numeric <> 0$g$
                           ) > 0 AS zero_eligible_coverage_guard,
                           owner.rolname AS function_owner
                      FROM pg_catalog.pg_roles AS writer
                      JOIN pg_catalog.pg_class AS report
                        ON report.oid='public.learning_paper_performance_report'::regclass
                      JOIN pg_catalog.pg_proc AS write_function
                        ON write_function.oid =
                           'public.wp06_insert_paper_performance_report'
                           '(uuid,uuid,uuid,uuid,text,text,text,text,text,timestamp with time zone,'
                           'text,uuid,text,jsonb,text)'::regprocedure
                      JOIN pg_catalog.pg_roles AS owner ON owner.oid=write_function.proowner
                     WHERE writer.rolname=
                           'crypidian_learning_paper_performance_report_writer'
                    """
                    )
                )
                .mappings()
                .one()
            )
            execute_admin = connection.execute(
                text(
                    "SELECT pg_catalog.has_function_privilege("
                    "'crypidian_admin_web', 'public.wp06_aggregate_paper_performance"
                    "(uuid,uuid,text,uuid,text,timestamp with time zone)', 'EXECUTE')"
                )
            ).scalar_one()
            execute_public_app = connection.execute(
                text(
                    "SELECT NOT pg_catalog.has_function_privilege("
                    "'crypidian_app', 'public.wp06_aggregate_paper_performance"
                    "(uuid,uuid,text,uuid,text,timestamp with time zone)', 'EXECUTE')"
                    " AND NOT EXISTS ("
                    "SELECT 1 FROM pg_catalog.pg_proc AS function "
                    "CROSS JOIN LATERAL pg_catalog.aclexplode(function.proacl) AS acl "
                    "WHERE function.oid='public.wp06_aggregate_paper_performance"
                    "(uuid,uuid,text,uuid,text,timestamp with time zone)'::regprocedure "
                    "AND acl.grantee=0 AND acl.privilege_type='EXECUTE')"
                )
            ).scalar_one()
        with admin_engine.begin() as connection:
            connection.execute(
                text("SELECT pg_catalog.set_config('app.workspace_id', :workspace, true)"),
                {"workspace": str(workspace_id)},
            )
            denied = False
            try:
                with connection.begin_nested():
                    connection.execute(
                        text(
                            "SELECT * FROM public.wp06_aggregate_paper_performance("
                            ":workspace, :bot, :fingerprint, :baseline_id, :baseline_hash, "
                            "pg_catalog.transaction_timestamp())"
                        ),
                        {
                            "workspace": workspace_id,
                            "bot": bot_id,
                            "fingerprint": "a" * 64,
                            "baseline_id": uuid4(),
                            "baseline_hash": "b" * 64,
                        },
                    )
            except DBAPIError as error:
                denied = "WP06_PAPER_PERFORMANCE_ACTIVE_TARGET_REQUIRED" in str(error.orig)
            assert denied, "Gateway phải từ chối khi workspace chưa có target active."

            as_of = connection.scalar(text("SELECT pg_catalog.transaction_timestamp()"))
            assert as_of is not None
            older_start = as_of - timedelta(days=90)
            older_end = as_of - timedelta(days=60)
            newer_start = older_end
            newer_end = as_of - timedelta(days=30)
            report_id = uuid4()
            baseline_report_id = uuid4()
            target_fingerprint = "a" * 64
            baseline_report_hash = "b" * 64
            older_membership_digest = "c" * 64
            newer_membership_digest = "d" * 64
            policy = PaperPerformancePolicy()
            inconclusive_reasons = [
                "minimum_outcomes_not_met",
                "minimum_class_support_not_met",
                "minimum_mature_coverage_not_met",
            ]
            performance_report = {
                "as_of": as_of.isoformat(timespec="microseconds"),
                "automation": "NO_STATE_CHANGE",
                "baseline": {
                    "description": "TEST baseline đã đóng băng",
                    "report_hash": baseline_report_hash,
                },
                "cohorts": [
                    {
                        "brier_regression": None,
                        "baseline_brier": "0",
                        "baseline_log_loss": "0",
                        "class_support": [
                            {"class": -1, "count": 0},
                            {"class": 0, "count": 0},
                            {"class": 1, "count": 0},
                        ],
                        "coverage": "0",
                        "covered_outcome_count": 0,
                        "eligible_decision_count": 0,
                        "log_loss_regression": None,
                        "membership_digest": older_membership_digest,
                        "observed_brier": None,
                        "observed_log_loss": None,
                        "reasons": inconclusive_reasons,
                        "status": "inconclusive",
                        "window_end": older_end.isoformat(timespec="microseconds"),
                        "window_start": older_start.isoformat(timespec="microseconds"),
                    },
                    {
                        "brier_regression": None,
                        "baseline_brier": "0",
                        "baseline_log_loss": "0",
                        "class_support": [
                            {"class": -1, "count": 0},
                            {"class": 0, "count": 0},
                            {"class": 1, "count": 0},
                        ],
                        "coverage": "0",
                        "covered_outcome_count": 0,
                        "eligible_decision_count": 0,
                        "log_loss_regression": None,
                        "membership_digest": newer_membership_digest,
                        "observed_brier": None,
                        "observed_log_loss": None,
                        "reasons": inconclusive_reasons,
                        "status": "inconclusive",
                        "window_end": newer_end.isoformat(timespec="microseconds"),
                        "window_start": newer_start.isoformat(timespec="microseconds"),
                    },
                ],
                "policy_hash": policy.content_hash,
                "policy_version": policy.version,
                "reasons": [
                    "cohort_0_minimum_outcomes_not_met",
                    "cohort_0_minimum_class_support_not_met",
                    "cohort_0_minimum_mature_coverage_not_met",
                    "cohort_1_minimum_outcomes_not_met",
                    "cohort_1_minimum_class_support_not_met",
                    "cohort_1_minimum_mature_coverage_not_met",
                ],
                "schema_version": "crypidian.learning.paper-performance.v1",
                "selection_bias_note": (
                    "Chỉ gồm intent PAPER được scorer duyệt, risk chấp thuận và có "
                    "giao dịch đóng đúng hạn; không suy diễn kết quả cho quyết định "
                    "không vào lệnh hoặc lợi nhuận tương lai."
                ),
                "scope": "PAPER_ONLY_DIAGNOSTIC",
                "status": "inconclusive",
                "target_fingerprint": target_fingerprint,
                "thresholds": {
                    "cohort_days": 30,
                    "maturity_days": 30,
                    "maximum_brier_regression": "0.01",
                    "maximum_log_loss_regression": "0.01",
                    "minimum_class_support": 100,
                    "minimum_coverage": "0.80",
                    "minimum_outcomes": 1000,
                },
            }
            canonical_report = json.dumps(
                performance_report,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            report_hash = hashlib.sha256(canonical_report.encode("utf-8")).hexdigest()
            db_hashes = (
                connection.execute(
                    text(
                        "SELECT public.wp06_license_canonical_json(CAST(:report AS jsonb)) "
                        "AS canonical_report, pg_catalog.encode(pg_catalog.sha256("
                        "pg_catalog.convert_to(public.wp06_license_canonical_json("
                        "CAST(:report AS jsonb)), "
                        "'UTF8')), 'hex') AS report_hash, pg_catalog.encode(pg_catalog.sha256("
                        "pg_catalog.convert_to(public.wp06_license_canonical_json("
                        "pg_catalog.jsonb_build_object("
                        "'class_order', pg_catalog.jsonb_build_array(-1,0,1), "
                        "'cohort_days',30, 'log_loss_epsilon','1E-12', "
                        "'maturity_days',30, "
                        "'maximum_brier_regression','0.01', 'maximum_log_loss_regression','0.01', "
                        "'minimum_class_support',100, 'minimum_coverage','0.80', "
                        "'minimum_outcomes',1000, 'version','wp06-paper-performance-v1')), "
                        "'UTF8')), 'hex') AS policy_hash"
                    ),
                    {"report": canonical_report},
                )
                .mappings()
                .one()
            )
            assert db_hashes["canonical_report"] == canonical_report
            assert db_hashes["report_hash"] == report_hash
            assert db_hashes["policy_hash"] == policy.content_hash, (
                db_hashes["policy_hash"],
                policy.content_hash,
            )
            report_shape = (
                connection.execute(
                    text(
                        "SELECT ARRAY(SELECT object_key FROM "
                        "pg_catalog.jsonb_object_keys(CAST(:report AS jsonb)) "
                        'AS item(object_key) ORDER BY object_key COLLATE "C") '
                        "= ARRAY['as_of','automation','baseline','cohorts','policy_hash', "
                        "'policy_version','reasons','schema_version','scope', "
                        "'selection_bias_note','status','target_fingerprint','thresholds']::text[] "
                        "AS keys, CAST(:report AS jsonb)->>'selection_bias_note' = :note AS note, "
                        "CAST(:report AS jsonb)->>'as_of' IS NOT NULL AS as_of, "
                        "CAST(:report AS jsonb)->'baseline' = "
                        "pg_catalog.jsonb_build_object('description','TEST baseline đã đóng băng', "
                        "'report_hash',CAST(:baseline_hash AS text)) AS baseline, "
                        "CAST(:report AS jsonb)->'thresholds' = "
                        "pg_catalog.jsonb_build_object('cohort_days',30,'maturity_days',30, "
                        "'minimum_outcomes',1000,'minimum_class_support',100, "
                        "'minimum_coverage','0.80','maximum_brier_regression','0.01', "
                        "'maximum_log_loss_regression','0.01') AS thresholds, "
                        "pg_catalog.jsonb_typeof(CAST(:report AS jsonb)->'cohorts')='array' "
                        "AND pg_catalog.jsonb_array_length(CAST(:report AS jsonb)->'cohorts')=2 "
                        "AS cohorts"
                    ),
                    {
                        "report": canonical_report,
                        "note": performance_report["selection_bias_note"],
                        "baseline_hash": baseline_report_hash,
                    },
                )
                .mappings()
                .one()
            )
            assert all(report_shape.values()), dict(report_shape)
            writer_denied = False
            writer_error = ""
            try:
                with connection.begin_nested():
                    connection.execute(
                        text(
                            "SELECT public.wp06_insert_paper_performance_report("
                            "CAST(:report_id AS uuid), CAST(:workspace_id AS uuid), "
                            "CAST(:bot_id AS uuid), CAST(:baseline_report_id AS uuid), "
                            ":baseline_report_hash, :policy_version, :policy_hash, "
                            ":target_fingerprint, :membership_digest, "
                            "CAST(:as_of AS timestamptz), :status, "
                            "CAST(:idempotency_key AS uuid), :report_hash, "
                            "CAST(:report AS jsonb), :canonical_report)"
                        ),
                        {
                            "report_id": report_id,
                            "workspace_id": workspace_id,
                            "bot_id": bot_id,
                            "baseline_report_id": baseline_report_id,
                            "baseline_report_hash": baseline_report_hash,
                            "policy_version": policy.version,
                            "policy_hash": policy.content_hash,
                            "target_fingerprint": target_fingerprint,
                            "membership_digest": "f" * 64,
                            "as_of": as_of,
                            "status": "inconclusive",
                            "idempotency_key": uuid4(),
                            "report_hash": report_hash,
                            "report": canonical_report,
                            "canonical_report": canonical_report,
                        },
                    )
            except DBAPIError as error:
                writer_error = str(error.orig)
                writer_denied = any(
                    marker in writer_error
                    for marker in (
                        "WP06_PAPER_PERFORMANCE_ACTIVE_TARGET_REQUIRED",
                        "WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED",
                    )
                )
            assert writer_denied, (
                "Writer phải tái kiểm active target/current rights; "
                f"PostgreSQL trả về: {writer_error}"
            )
            report_exists = connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM public.learning_paper_performance_report "
                    "WHERE workspace_id=:workspace AND id=:report_id)"
                ),
                {"workspace": workspace_id, "report_id": report_id},
            )
            assert report_exists is False, "Denial không được lưu report."
        assert acl["evaluator_isolated"] is True
        assert acl["trade_column_scoped"] is True
        assert acl["report_table_access_denied"] is True
        assert acl["report_metadata_select"] is True
        assert acl["report_payload_select_denied"] is True
        assert acl["report_rls"] is True
        assert execute_admin is True
        assert execute_public_app is True
        assert writer_acl["role_isolated"] is True
        assert writer_acl["insert_columns_only"] is True
        assert writer_acl["function_execute_scoped"] is True
        assert writer_acl["aggregate_execute"] is True
        assert writer_acl["zero_denominator_safe"] is True
        assert writer_acl["zero_eligible_coverage_guard"] is True
        assert writer_acl["function_owner"] == "crypidian_learning_paper_performance_report_writer"
        assert report_exists is False
        print(
            json.dumps(
                {
                    "narrow_evaluator_role": True,
                    "aggregate_report_metadata_only_acl": True,
                    "admin_can_execute_gateway": True,
                    "public_and_app_cannot_execute": True,
                    "missing_active_target_denied": True,
                    "report_writer_acl_is_narrow": True,
                    "report_writer_aggregate_execute": True,
                    "report_writer_zero_denominator_safe": True,
                    "report_writer_zero_eligible_coverage_guard": True,
                    "report_writer_denies_missing_target_without_mutation": True,
                    "no_raw_member_fields_returned": True,
                },
                sort_keys=True,
            )
        )
    finally:
        super_engine.dispose()
        admin_engine.dispose()


if __name__ == "__main__":
    main()
