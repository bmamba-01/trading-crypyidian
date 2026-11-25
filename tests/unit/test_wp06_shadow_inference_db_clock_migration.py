from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / ("migrations/versions/f0a2c4e6b8d3_wp06_shadow_inference_db_clock.py")


def _migration():
    assert MIGRATION.is_file(), "Thiếu migration lấy giờ hiện hành từ PostgreSQL cho Shadow."
    spec = importlib.util.spec_from_file_location("wp06_shadow_inference_db_clock", MIGRATION)
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
    )
    module.upgrade()
    return "\n".join(statements)


def test_shadow_inference_clock_migration_chains_after_action_latency() -> None:
    migration = _migration()

    assert migration.revision == "f0a2c4e6b8d3"
    assert migration.down_revision == "f0a2c4e6b8d2"


def test_shadow_authorizer_ignores_caller_clock_after_source_locks() -> None:
    sql = _upgrade_sql()
    wrapper_start = sql.index(
        "CREATE FUNCTION public.wp06_authorize_shadow_inference_source_scope("
    )
    wrapper_end = sql.index(
        "ALTER FUNCTION public.wp06_authorize_shadow_inference_source_scope(uuid,uuid,"
        "timestamptz) OWNER TO",
        wrapper_start,
    )
    wrapper = sql[wrapper_start:wrapper_end]

    lock = wrapper.index("pg_advisory_xact_lock")
    database_clock = wrapper.index("database_checked_at := pg_catalog.clock_timestamp()")
    internal_call = wrapper.index("wp06_authorize_shadow_inference_source_scope_with_caller_clock(")
    assert lock < database_clock < internal_call
    assert "caller_checked_at IS NULL" in wrapper
    assert "target_workspace, target_decision, database_checked_at" in wrapper
    assert "target_workspace, target_decision, caller_checked_at" not in wrapper
    assert "SET search_path = pg_catalog, pg_temp" in wrapper


def test_shadow_authorizer_internal_clock_gate_is_not_directly_executable() -> None:
    sql = _upgrade_sql()

    assert (
        "ALTER FUNCTION public.wp06_authorize_shadow_inference_source_scope(uuid,uuid,"
        "timestamptz) RENAME TO wp06_authorize_shadow_inference_source_scope_with_caller_clock"
    ) in sql
    assert (
        "REVOKE ALL ON FUNCTION "
        "public.wp06_authorize_shadow_inference_source_scope_with_caller_clock"
        "(uuid,uuid,timestamptz) FROM PUBLIC, crypidian_admin_web, "
        "crypidian_learning_shadow_observer"
    ) in sql
    assert (
        "GRANT EXECUTE ON FUNCTION public.wp06_authorize_shadow_inference_source_scope"
        "(uuid,uuid,timestamptz) TO crypidian_admin_web, crypidian_learning_shadow_observer"
    ) in sql


def test_shadow_clock_wrapper_keeps_the_existing_scoped_rights_owner() -> None:
    sql = _upgrade_sql()

    assert (
        "ALTER FUNCTION public.wp06_authorize_shadow_inference_source_scope(uuid,uuid,"
        "timestamptz) OWNER TO crypidian_scorer_deployment_rights_reader"
    ) in sql
