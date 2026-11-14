from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/f8e0a2c4d6bc_wp06_shadow_drift_admin_read_gate.py"


def _migration():
    assert MIGRATION.exists(), "Thiếu migration admin-read gateway cho Shadow/Drift."
    spec = importlib.util.spec_from_file_location("wp06_shadow_drift_admin_read_gate", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_sql() -> str:
    statements: list[str] = []
    module = _migration()
    module.op = SimpleNamespace(execute=lambda sql: statements.append(str(sql)))
    module.upgrade()
    return "\n".join(statements)


def test_admin_read_gateway_chains_after_exact_source_consumer_gate() -> None:
    migration = _migration()
    assert migration.revision == "f8e0a2c4d6bc"
    assert migration.down_revision == "f8e0a2c4d6bb"


def test_raw_admin_select_is_revoked_and_feed_gateway_locks_before_projection() -> None:
    migration = _migration()
    sql = _upgrade_sql()
    start = sql.index("CREATE FUNCTION public.wp06_read_learning_observations_admin_feed(")
    end = sql.index("ALTER FUNCTION public.wp06_read_learning_observations_admin_feed(", start)
    gateway = sql[start:end]

    for table in (
        "learning_shadow_observation",
        "learning_drift_assessment",
        "learning_drift_assessment_member",
        "learning_retrain_proposal",
    ):
        assert f"REVOKE SELECT ON TABLE public.{table} FROM crypidian_admin_web" in sql
    assert gateway.index("research-source:") < gateway.index(
        "wp06-learning-observation-member-set:"
    )
    assert gateway.index("member-set:") < gateway.index(
        "IF identity_after IS DISTINCT FROM identity_before"
    )
    assert gateway.index("IF identity_after IS DISTINCT FROM identity_before") < gateway.index(
        "wp06_recheck_learning_shadow_observation_source_rights"
    )
    assert "wp06_recheck_learning_drift_assessment_source_rights" in gateway
    assert "session_user <> 'crypidian_admin_web'" in gateway
    assert "transaction_isolation" in gateway
    assert "LIMIT target_limit" in gateway
    assert "source_scope_fingerprint" not in gateway
    assert "source_event_id" not in gateway
    assert "license_scope" not in gateway
    assert f"OWNER TO {migration.RIGHTS_ROLE}" in sql


def test_rights_reader_can_call_rechecks_and_incident_context_stays_gated() -> None:
    sql = _upgrade_sql()
    member_grant = next(
        statement
        for statement in sql.splitlines()
        if "GRANT SELECT (" in statement and "learning_drift_assessment_member" in statement
    )
    for column in (
        "current_rights_eligible",
        "current_rights_fingerprint",
        "current_source_ids",
        "current_eligible",
        "baseline_available",
        "schema_compatible",
    ):
        assert column in member_grant
    assert "GRANT CREATE ON SCHEMA public" not in sql
    assert (
        "GRANT EXECUTE ON FUNCTION public.wp06_recheck_learning_shadow_observation_source_rights"
        "(uuid,uuid) TO crypidian_scorer_deployment_rights_reader"
    ) in sql
    assert (
        "GRANT EXECUTE ON FUNCTION public.wp06_recheck_learning_drift_assessment_source_rights"
        "(uuid,uuid) TO crypidian_scorer_deployment_rights_reader"
    ) in sql
    start = sql.index("CREATE FUNCTION public.wp06_read_learning_incident_context(")
    end = sql.index("ALTER FUNCTION public.wp06_read_learning_incident_context(", start)
    context = sql[start:end]
    assert "wp06_learning_incident_eligible_target" in context
    assert "session_user <> 'crypidian_admin_web'" in context
    assert "transaction_isolation" in context
    assert "LearningIncident" not in context
    for name in ("source_rights_digest", "member_ids", "reason_codes", "snapshot_coverage_rate"):
        assert name in context
    assert "source_ids" not in context
    assert "source_event_id" not in context


def test_admin_feed_definer_can_execute_its_nested_source_identity_helper() -> None:
    migration = _migration()
    sql = _upgrade_sql()
    helper = "public.wp06_learning_observation_admin_feed_source_ids(uuid,jsonb)"
    assert f"OWNER TO {migration.RIGHTS_ROLE}" in sql
    assert f"GRANT EXECUTE ON FUNCTION {helper} TO {migration.RIGHTS_ROLE}" in sql
    assert f"FROM PUBLIC, crypidian_app, crypidian_trading_runtime, {migration.ADMIN}" in sql

    actions: list[str] = []
    migration.op = SimpleNamespace(execute=lambda statement: actions.append(str(statement)))
    migration.downgrade()
    downgrade = "\n".join(actions)
    assert f"DROP FUNCTION {helper}" in downgrade


def test_downgrade_restores_admin_table_reads_and_revokes_nested_gate_access() -> None:
    actions: list[str] = []
    module = _migration()
    module.op = SimpleNamespace(execute=lambda sql: actions.append(str(sql)))
    module.downgrade()
    script = "\n".join(actions)

    for table in (
        "learning_shadow_observation",
        "learning_drift_assessment",
        "learning_drift_assessment_member",
        "learning_retrain_proposal",
    ):
        assert f"GRANT SELECT ON TABLE public.{table} TO crypidian_admin_web" in script
        assert (
            f"REVOKE SELECT ON TABLE public.{table} FROM crypidian_scorer_deployment_rights_reader"
        ) in script
    assert (
        "REVOKE EXECUTE ON FUNCTION public.wp06_recheck_learning_shadow_observation_source_rights"
    ) in script
    assert (
        "REVOKE EXECUTE ON FUNCTION public.wp06_recheck_learning_drift_assessment_source_rights"
    ) in script
    assert "DROP FUNCTION public.wp06_read_learning_observations_admin_feed" in script
    assert "DROP FUNCTION public.wp06_read_learning_incident_context" in script
    assert "GRANT CREATE ON SCHEMA public" not in script
