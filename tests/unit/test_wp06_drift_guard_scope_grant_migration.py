from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/b4c6d8e0f2a9_wp06_drift_guard_scope_grant.py"
GATE = "public.wp06_authorize_learning_drift_source_scope_current(uuid,uuid)"
RIGHTS_DIGEST = "public.wp06_learning_drift_current_rights_digest(uuid,uuid,timestamptz)"
GUARD_ROLE = "crypidian_learning_shadow_integrity_guard"


def _migration_module():
    spec = importlib.util.spec_from_file_location("wp06_drift_guard_scope_grant", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_drift_trigger_guard_gets_only_internal_scope_gate_execution() -> None:
    module = _migration_module()
    statements: list[str] = []
    module.op = SimpleNamespace(execute=lambda sql: statements.append(str(sql)))

    module.upgrade()

    sql = "\n".join(statements)
    assert f"GRANT EXECUTE ON FUNCTION {GATE} TO {GUARD_ROLE}" in sql
    assert f"GRANT EXECUTE ON FUNCTION {RIGHTS_DIGEST} TO {GUARD_ROLE}" in sql
    assert "TO crypidian_learning_shadow_observer" not in sql
    assert "wp06_authorize_learning_drift_source_scope_current(" in sql
    assert "pg_catalog.clock_timestamp())" in sql


def test_downgrade_revokes_the_internal_scope_gate_execution() -> None:
    module = _migration_module()
    statements: list[str] = []
    module.op = SimpleNamespace(execute=lambda sql: statements.append(str(sql)))

    module.downgrade()

    sql = "\n".join(statements)
    for table in (
        "learning_shadow_observation",
        "learning_drift_assessment",
        "learning_drift_assessment_member",
    ):
        assert f"EXISTS (SELECT 1 FROM public.{table})" in sql
    guard = sql.index("WP06_DRIFT_GUARD_SCOPE_GRANT_DOWNGRADE_POPULATED_DENIED")
    assert sql.index("IN ACCESS EXCLUSIVE MODE") < guard < sql.index("REVOKE EXECUTE")
    assert f"REVOKE EXECUTE ON FUNCTION {RIGHTS_DIGEST} FROM {GUARD_ROLE}" in sql
    assert f"REVOKE EXECUTE ON FUNCTION {GATE} FROM {GUARD_ROLE}" in sql
