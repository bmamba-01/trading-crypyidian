from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / ("migrations/versions/e2f4a6c8b0d7_wp06_paper_performance_report_writer.py")


def _migration():
    assert MIGRATION.exists(), "Thiếu writer SECURITY DEFINER cho report PAPER."
    spec = importlib.util.spec_from_file_location("wp06_paper_performance_report_writer", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql_for(operation_name: str) -> str:
    statements: list[str] = []
    module = _migration()
    module.op = SimpleNamespace(
        execute=lambda sql: statements.append(str(sql)),
        f=lambda name: name,
    )
    getattr(module, operation_name)()
    return "\n".join(statements)


def test_writer_migration_follows_admin_insert_revocation() -> None:
    migration = _migration()

    assert migration.down_revision == "e2f4a6c8b0d6"


def test_writer_role_has_only_scoped_insert_path_and_admin_execute() -> None:
    sql = _sql_for("upgrade")

    assert "CREATE ROLE crypidian_learning_paper_performance_report_writer NOLOGIN" in sql
    assert "NOSUPERUSER NOBYPASSRLS NOINHERIT" in sql
    assert "role.rolcanlogin OR role.rolsuper OR role.rolbypassrls OR role.rolinherit" in sql
    assert "wp06_paper_performance_report_writer_insert" in sql
    assert "FOR INSERT TO crypidian_learning_paper_performance_report_writer" in sql
    assert "workspace_id = NULLIF(" in sql
    assert "GRANT CREATE ON SCHEMA public TO " in sql
    assert "REVOKE CREATE ON SCHEMA public FROM " in sql
    assert "REVOKE ALL ON FUNCTION public.wp06_insert_paper_performance_report(" in sql
    assert "FROM PUBLIC, crypidian_app, crypidian_trading_runtime" in sql
    assert (
        "GRANT EXECUTE ON FUNCTION public.wp06_insert_paper_performance_report("
        "uuid,uuid,uuid,uuid,text,text,text,text,text,timestamptz,text,uuid,text,jsonb,text) "
        "TO crypidian_admin_web"
    ) in sql


def test_writer_checks_session_workspace_and_canonical_hash_before_insert() -> None:
    sql = _sql_for("upgrade")
    start = sql.index("CREATE FUNCTION public.wp06_insert_paper_performance_report(")
    end = sql.index("ALTER FUNCTION public.wp06_insert_paper_performance_report(", start)
    function = sql[start:end]

    assert "SECURITY DEFINER" in function
    assert "SET search_path = pg_catalog, pg_temp" in function
    assert "session_user <> 'crypidian_admin_web'" in function
    assert "app.workspace_id" in function
    assert "transaction_isolation" in function
    assert "wp06_license_canonical_json(target_report)" in function
    assert "pg_catalog.sha256" in function
    assert function.index("canonical_report") < function.index(
        "INSERT INTO public.learning_paper_performance_report"
    )
    assert "target_report_hash" in function


def test_writer_binds_report_payload_to_current_aggregate_and_fixed_policy() -> None:
    sql = _sql_for("upgrade")
    start = sql.index("CREATE FUNCTION public.wp06_insert_paper_performance_report(")
    end = sql.index("ALTER FUNCTION public.wp06_insert_paper_performance_report(", start)
    function = sql[start:end]

    assert "wp06_aggregate_paper_performance(" in function
    for field in (
        "eligible_decision_count",
        "covered_outcome_count",
        "class_minus_one_count",
        "class_zero_count",
        "class_plus_one_count",
        "observed_brier",
        "observed_log_loss",
        "baseline_brier",
        "baseline_log_loss",
        "membership_digest",
        "window_start",
        "window_end",
    ):
        assert field in function
    assert "minimum_outcomes_not_met" in function
    assert "minimum_class_support_not_met" in function
    assert "minimum_mature_coverage_not_met" in function
    assert "aggregate_row.eligible_decision_count = 0 AND" in function
    assert "aggregate_row.covered_outcome_count::numeric * 5 <" in function
    assert "joint_metric_regression_in_both_cohorts" in function
    assert "wp06_license_canonical_json" in function
    assert "PAPER_PERFORMANCE_REPORT_AGGREGATE_MISMATCH" in function


def test_writer_can_call_aggregate_gateway_and_revokes_it_on_downgrade() -> None:
    upgrade_sql = _sql_for("upgrade")
    downgrade_sql = _sql_for("downgrade")
    aggregate = "public.wp06_aggregate_paper_performance(uuid,uuid,text,uuid,text,timestamptz)"

    assert f"GRANT EXECUTE ON FUNCTION {aggregate}" in upgrade_sql
    assert f"REVOKE EXECUTE ON FUNCTION {aggregate}" in downgrade_sql
    assert downgrade_sql.index(f"REVOKE EXECUTE ON FUNCTION {aggregate}") < downgrade_sql.index(
        "DROP ROLE crypidian_learning_paper_performance_report_writer"
    )


def test_writer_downgrade_restores_d6_admin_execute_state_and_removes_writer() -> None:
    sql = _sql_for("downgrade")

    assert "DROP FUNCTION public.wp06_insert_paper_performance_report(" in sql
    assert "DROP POLICY wp06_paper_performance_report_writer_insert" in sql
    assert "REVOKE INSERT (id,workspace_id,bot_id,baseline_report_id," in sql
    assert "ON public.learning_paper_performance_report FROM " in sql
    assert "FROM crypidian_learning_paper_performance_report_writer" in sql
    assert "REVOKE EXECUTE ON FUNCTION public.wp06_license_canonical_json(jsonb)" in sql
    revoke_schema_usage = sql.index(
        "REVOKE USAGE ON SCHEMA public FROM crypidian_learning_paper_performance_report_writer"
    )
    drop_role = sql.index("DROP ROLE crypidian_learning_paper_performance_report_writer")
    assert revoke_schema_usage < drop_role
    assert "DROP ROLE crypidian_learning_paper_performance_report_writer" in sql
