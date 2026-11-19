from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/a6c8e0b2d4f7_wp06_drift_observer_scope.py"


def _upgrade_sql() -> str:
    spec = importlib.util.spec_from_file_location("wp06_drift_observer_scope", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []
    module.op = SimpleNamespace(execute=lambda sql: statements.append(str(sql)))
    module.upgrade()
    return "\n".join(statements)


def test_observer_gate_uses_database_clock_only_after_source_locks() -> None:
    sql = _upgrade_sql()
    start = sql.index("CREATE FUNCTION public.wp06_authorize_learning_drift_source_scope_current(")
    end = sql.index(
        "ALTER FUNCTION public.wp06_authorize_learning_drift_source_scope_current(", start
    )
    gate = sql[start:end]

    signature = gate[gate.index("(") : gate.index(") RETURNS")]
    assert "target_workspace uuid, target_decision uuid" in signature
    assert "checked_at" not in signature
    assert gate.index("research-source:") < gate.index("clock_timestamp()")
    assert "wp06_authorize_learning_drift_source_scope(" in gate
    assert "GRANT EXECUTE ON FUNCTION " in sql
    assert (
        "GRANT EXECUTE ON FUNCTION "
        "public.wp06_authorize_learning_drift_source_scope_current(uuid,uuid) "
        "TO crypidian_learning_shadow_observer"
    ) in sql
    assert (
        "GRANT EXECUTE ON FUNCTION "
        "public.wp06_authorize_learning_drift_source_scope(uuid,uuid,timestamptz) "
        "TO crypidian_learning_shadow_observer"
    ) not in sql
