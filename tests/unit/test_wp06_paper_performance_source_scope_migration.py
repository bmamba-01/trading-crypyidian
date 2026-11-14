from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/f8e0a2c4d6ba_wp06_paper_performance_source_scope.py"


def _migration():
    assert MIGRATION.exists(), "Thiếu migration quyền hiện hành cho PAPER performance."
    spec = importlib.util.spec_from_file_location("wp06_paper_performance_source_scope", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_sql() -> str:
    statements: list[str] = []
    module = _migration()
    module.op = SimpleNamespace(
        execute=lambda sql: statements.append(str(sql)),
        f=lambda name: name,
    )
    module.upgrade()
    return "\n".join(statements)


def _downgrade_sql() -> str:
    statements: list[str] = []
    module = _migration()
    module.op = SimpleNamespace(execute=lambda sql: statements.append(str(sql)))
    module.downgrade()
    return "\n".join(statements)


def test_paper_source_scope_migration_chains_after_runtime_rights_gate() -> None:
    migration = _migration()

    assert migration.down_revision == "f8e0a2c4d6b9"


def test_aggregate_source_gate_precedes_metric_projection_and_returns_current_digest() -> None:
    sql = _upgrade_sql()
    gateway_start = sql.index("CREATE FUNCTION public.wp06_aggregate_paper_performance(")
    gateway_end = sql.index(
        "ALTER FUNCTION public.wp06_aggregate_paper_performance(", gateway_start
    )
    gateway = sql[gateway_start:gateway_end]

    assert "rights_digest text" in gateway
    assert gateway.index("wp06_compute_paper_performance_rights_snapshot") < gateway.index(
        "wp06_aggregate_paper_performance_unscoped"
    )
    assert "WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED" in gateway


def test_current_rights_recheck_reconstructs_exact_membership_and_is_boolean_only() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    sql = _upgrade_sql()
    start = sql.index("CREATE FUNCTION public.wp06_recheck_paper_performance_report_rights(")
    end = sql.index(
        "ALTER FUNCTION public.wp06_recheck_paper_performance_report_rights(uuid,uuid)",
        start,
    )
    recheck = sql[start:end]

    assert "RETURNS boolean" in recheck
    for required in (
        "report.membership_digest",
        "older_membership_digest",
        "newer_membership_digest",
        "wp06_compute_paper_performance_rights_snapshot",
        "crypidian.wp06.paper-performance-membership.v2",
        "decision_lineage_fingerprint",
    ):
        assert required in recheck or required in source
    assert "RETURN false" in recheck
    assert "RETURN true" in recheck
    assert "class_probabilities" not in recheck


def test_recheck_reads_only_metadata_and_migration_keeps_report_schema_unchanged() -> None:
    sql = _upgrade_sql()
    start = sql.index("CREATE FUNCTION public.wp06_recheck_paper_performance_report_rights(")
    end = sql.index(
        "ALTER FUNCTION public.wp06_recheck_paper_performance_report_rights(uuid,uuid)",
        start,
    )
    recheck = sql[start:end]
    for column in (
        "workspace_id",
        "id",
        "bot_id",
        "baseline_report_id",
        "baseline_report_hash",
        "target_fingerprint",
        "as_of",
        "membership_digest",
    ):
        assert f"report.{column}" in recheck
    for forbidden in ("report.report", "report.canonical_report", "report.status"):
        assert forbidden not in recheck
    assert (
        "GRANT SELECT (workspace_id,id,bot_id,idempotency_key,target_fingerprint,policy_hash,"
        "as_of,membership_digest,created_at)"
    ) in sql
    assert (
        "GRANT SELECT ON TABLE public.learning_paper_performance_report "
        "TO crypidian_learning_paper_performance_evaluator"
    ) in sql
    assert (
        "GRANT EXECUTE ON FUNCTION public.wp06_license_canonical_json(jsonb) "
        "TO crypidian_learning_paper_performance_evaluator"
    ) in sql
    assert "ALTER TABLE public.learning_paper_performance_report" not in sql
    assert "NEW.rights_digest" not in sql
    assert "NEW.older_membership_digest" not in sql
    assert "NEW.newer_membership_digest" not in sql


def test_insert_guard_uses_report_cohort_windows_and_denies_unverified_rights() -> None:
    sql = _upgrade_sql()
    start = sql.index("CREATE FUNCTION public.wp06_validate_paper_performance_source_rights()")
    end = sql.index(
        "ALTER FUNCTION public.wp06_validate_paper_performance_source_rights()",
        start,
    )
    guard = sql[start:end]

    assert "cohort_name" not in guard
    for expected_window in (
        "interval '90 days'",
        "interval '60 days'",
        "interval '30 days'",
    ):
        assert expected_window in guard
    assert "older_digest IS NULL OR newer_digest IS NULL" in guard
    assert "snapshot.rights_digest IS NULL" in guard
    assert "WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED" in guard
    assert "RETURN NEW" in guard


def test_downgrade_keeps_task3_source_bar_rights_grant_and_blocks_existing_reports() -> None:
    sql = _upgrade_sql()
    downgrade = _downgrade_sql()
    task3 = (ROOT / "migrations/versions/f8e0a2c4d6b9_wp06_scorer_job_source_scope.py").read_text(
        encoding="utf-8"
    )

    assert "market_bar_id,source_event_id,source_event_digest," in task3
    assert (
        "GRANT SELECT (workspace_id,bot_id,bot_decision_id,decision_snapshot_id,timeframe," in task3
    )
    assert "REVOKE SELECT ON TABLE public.bot_decision_source_bar" not in sql
    assert "DROP POLICY wp06_paper_performance_evaluator_report_metadata" in downgrade
    assert (
        "REVOKE SELECT (workspace_id,id,bot_id,baseline_report_id,baseline_report_hash,"
        in downgrade
    )
    assert (
        "REVOKE EXECUTE ON FUNCTION public.wp06_license_canonical_json(jsonb) "
        "FROM crypidian_learning_paper_performance_evaluator"
    ) in downgrade
    assert (
        "REVOKE EXECUTE ON FUNCTION "
        "public.wp06_aggregate_paper_performance_unscoped(uuid,uuid,text,uuid,text,timestamptz) "
        "FROM crypidian_learning_paper_performance_evaluator"
    ) in downgrade
    assert "IF EXISTS (SELECT 1 FROM public.learning_paper_performance_report)" in downgrade
    assert "ALTER TABLE public.learning_paper_performance_report" not in downgrade


def test_decision_scope_rechecks_all_bars_under_source_lock_for_aggregate_operation() -> None:
    sql = _upgrade_sql()
    for required in (
        "wp06_verify_bot_decision_source_scope",
        "decision_lineage_fingerprint",
        "bot_decision_source_bar",
        "market_bar_id",
        "source_event_id",
        "pg_advisory_xact_lock",
        "research-source:",
        "aggregate_report",
        "aggregate_reporting",
        "paper_demo",
        "QA_DENIED",
        "max_retention_days",
        "source_license_evidence",
        "source_policy_revision",
    ):
        assert required in sql


def test_fixed_paper_decision_rights_helper_returns_only_opaque_boolean() -> None:
    sql = _upgrade_sql()
    start = sql.index(
        "CREATE FUNCTION public.wp06_can_aggregate_paper_performance_decision_source("
    )
    end = sql.index(
        "ALTER FUNCTION public.wp06_can_aggregate_paper_performance_decision_source(uuid,uuid)",
        start,
    )
    helper = sql[start:end]

    assert "RETURNS boolean" in helper
    assert "wp06_authorize_paper_performance_decision_source_scope" in helper
    assert "session_user <> 'crypidian_admin_web'" in helper
    assert "app.workspace_id" in helper
    assert "transaction_isolation" in helper
    assert "EXCEPTION WHEN OTHERS THEN" in helper
    assert "RETURN false" in helper
    assert "RETURN true" in helper
    assert "rights_digest" not in helper
    assert "policy_scope" not in helper
    assert "source_id text" not in helper
    assert (
        "GRANT EXECUTE ON FUNCTION "
        "public.wp06_can_aggregate_paper_performance_decision_source(uuid,uuid) "
        "TO crypidian_admin_web"
    ) in sql
    assert "FROM PUBLIC, crypidian_app, crypidian_trading_runtime, " in sql


def test_member_fences_lock_before_writing_and_score_gate_comes_last() -> None:
    sql = _upgrade_sql()
    assert "CREATE TRIGGER zz_wp06_learning_observation_member_set_fence" in sql
    assert sql.count("CREATE TRIGGER zz_wp06_learning_observation_member_set_fence") == 3
    assert sql.count("BEFORE INSERT OR UPDATE OR DELETE ON public.") == 3
    assert "AFTER INSERT OR UPDATE OR DELETE" not in sql


def test_performance_attribution_is_one_decision_intent_and_one_closed_trade() -> None:
    sql = _upgrade_sql()
    paper_migration = (ROOT / "migrations/versions/c42e8f1a7d30_paper_trade_ledger.py").read_text(
        encoding="utf-8"
    )
    attribution_migration = (
        ROOT / "migrations/versions/b5d7f9a1c3e6_wp06_paper_decision_attribution.py"
    ).read_text(encoding="utf-8")

    assert "uq_order_intent_bot_decision" in attribution_migration
    assert "uq_trade_intent" in paper_migration
    assert "intent.bot_decision_id = event.bot_decision_id" in sql
    assert "intent.mode = 'PAPER'" in sql
    assert "trade.intent_id = eligible.intent_id" in sql
    assert "closed_trade_count = 1" in sql


def test_rights_membership_selector_matches_the_active_aggregate_mode() -> None:
    sql = _upgrade_sql()
    start = sql.index("CREATE FUNCTION public.wp06_list_paper_performance_decisions(")
    end = sql.index(
        "ALTER FUNCTION public.wp06_list_paper_performance_decisions(uuid,uuid,text,uuid,text,",
        start,
    )
    selector = sql[start:end]

    assert "wp06_resolve_scorer_runtime_target" in selector
    assert "active_row.mode NOT IN ('SHADOW','PAPER_GATE')" in selector
    assert "AND event.mode = active_row.mode" in selector


def test_rights_policy_projection_uses_only_columns_granted_to_source_rights_reader() -> None:
    sql = _upgrade_sql()
    start = sql.index(
        "CREATE FUNCTION public.wp06_authorize_paper_performance_decision_source_scope("
    )
    end = sql.index(
        "ALTER FUNCTION public.wp06_authorize_paper_performance_decision_source_scope(", start
    )
    authorizer = sql[start:end]

    assert "SELECT policy.*" not in authorizer
    assert "jsonb_populate_record" in authorizer
    for required in (
        "policy.sequence",
        "policy.policy_id",
        "policy.allowed_purposes",
        "policy.approved_by",
        "policy.quota_window_seconds",
        "policy.quota_max_requests",
        "policy.license_evidence_id",
        "policy.license_digest",
        "policy.license_scope_hash",
    ):
        assert required in authorizer


def test_report_payload_is_only_returned_through_current_rights_gateway() -> None:
    sql = _upgrade_sql()
    start = sql.index("CREATE FUNCTION public.wp06_read_paper_performance_report(")
    end = sql.index("ALTER FUNCTION public.wp06_read_paper_performance_report(uuid,uuid)", start)
    gateway = sql[start:end]

    assert "RETURNS SETOF public.learning_paper_performance_report" in gateway
    assert gateway.index("wp06_recheck_paper_performance_report_rights") < gateway.index(
        "RETURN QUERY"
    )
    assert (
        "REVOKE SELECT ON TABLE public.learning_paper_performance_report FROM crypidian_admin_web"
    ) in sql
    assert (
        "GRANT SELECT (workspace_id,id,bot_id,idempotency_key,target_fingerprint,policy_hash,"
        "as_of,membership_digest,created_at)"
    ) in sql
    assert (
        "GRANT SELECT ON TABLE public.learning_paper_performance_report TO "
        "crypidian_learning_paper_performance_evaluator"
    ) in sql


def test_snapshot_fences_then_reuses_one_exact_member_set_for_rights_and_membership() -> None:
    sql = _upgrade_sql()
    start = sql.index("CREATE FUNCTION public.wp06_compute_paper_performance_rights_snapshot(")
    end = sql.index(
        "ALTER FUNCTION public.wp06_compute_paper_performance_rights_snapshot(uuid,uuid,text,uuid,",
        start,
    )
    snapshot = sql[start:end]

    source_locks = snapshot.index("pg_advisory_xact_lock")
    cohort_key = snapshot.index("wp06-learning-observation-member-set:")
    cohort_lock = snapshot.rindex("PERFORM pg_catalog.pg_advisory_xact_lock", 0, cohort_key)
    member_set_compare = snapshot.index("IS DISTINCT FROM frozen_member_set")
    rights_members = snapshot.index("jsonb_to_recordset(frozen_member_set)")
    current_rights = snapshot.index("wp06_authorize_paper_performance_decision_source_scope")
    membership_query = snapshot.index("WITH eligible AS", current_rights)
    membership = snapshot.index("jsonb_to_recordset(frozen_member_set)", membership_query)
    assert (
        source_locks
        < cohort_lock
        < member_set_compare
        < rights_members
        < current_rights
        < membership
    )
    assert snapshot.count("wp06_list_paper_performance_decisions(") == 2
    assert "WP06_PAPER_PERFORMANCE_MEMBER_SET_CHANGED" in snapshot
    for table in (
        "public.bot_decision",
        "public.bot_decision_source_bar",
        "public.learning_score_event",
        "public.order_intent",
        "public.trade",
    ):
        assert table in sql


def test_downgrade_locks_report_table_before_populated_guard() -> None:
    downgrade = _downgrade_sql()
    assert downgrade.index("ACCESS EXCLUSIVE") < downgrade.index(
        "IF EXISTS (SELECT 1 FROM public.learning_paper_performance_report)"
    )


def test_upgrade_restricts_internal_rights_helpers_and_downgrade_restores_old_gateway() -> None:
    sql = _upgrade_sql()

    assert "GRANT EXECUTE ON FUNCTION public.wp06_recheck_paper_performance_report_rights" in sql
    assert "FROM PUBLIC, crypidian_app" in sql
    assert "REVOKE ALL ON FUNCTION public.wp06_aggregate_paper_performance_unscoped" in sql
    assert (
        "wp06_paper_performance_rights_downgrade_populated_denied"
        in MIGRATION.read_text(encoding="utf-8").lower()
    )
