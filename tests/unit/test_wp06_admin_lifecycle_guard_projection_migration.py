from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / ("migrations/versions/f0a2c4e6b8d7_wp06_admin_lifecycle_guard_projection.py")


def _migration():
    assert MIGRATION.is_file(), "Thiếu migration projection lifecycle guard cho admin-web."
    spec = importlib.util.spec_from_file_location(
        "wp06_admin_lifecycle_guard_projection", MIGRATION
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _statements(action: str) -> list[str]:
    statements: list[str] = []
    migration = _migration()
    migration.op = SimpleNamespace(execute=lambda statement: statements.append(str(statement)))
    getattr(migration, action)()
    return statements


def test_lifecycle_projection_revision_and_scope() -> None:
    migration = _migration()

    assert migration.revision == "f0a2c4e6b8d7"
    assert migration.down_revision == "f0a2c4e6b8d6"
    statements = _statements("upgrade")
    sql = "\n".join(statements)
    function_sql = sql[sql.index("CREATE FUNCTION") : sql.index("ALTER FUNCTION")]

    assert "RETURNS TABLE" in function_sql
    assert "bot_exists boolean" in function_sql
    assert "open_positions integer" in function_sql
    assert "open_orders integer" in function_sql
    assert "held_reservations integer" in function_sql
    assert "SECURITY DEFINER" in function_sql
    assert "SET search_path = pg_catalog, pg_temp" in function_sql
    assert "SET row_security = on" in function_sql
    assert "session_user <> 'crypidian_admin_web'" in function_sql
    assert "app.workspace_id" in function_sql
    assert "app.membership_id" in function_sql
    assert "member.status = 'ACTIVE'" in function_sql
    assert "binding.role = 'owner'" in function_sql
    assert "trade.id" not in function_sql
    assert "intent.id" not in function_sql
    assert "reservation.id" not in function_sql
    assert "GRANT SELECT (workspace_id, bot_id, status)" in sql
    assert "GRANT EXECUTE ON FUNCTION" in sql
    assert "TO crypidian_admin_web_safe" in sql
    assert "FOR INSERT TO crypidian_admin_web" in sql
    assert "FOR SELECT TO crypidian_admin_web" in sql
    assert "wp06_admin_safe_outbox_returning_select" in sql
    assert "GRANT INSERT ON TABLE public.outbox_event TO crypidian_admin_web_safe" in sql
    assert "GRANT SELECT (available_at, created_at)" in sql


def test_lifecycle_projection_downgrade_removes_function_policy_and_grant() -> None:
    sql = "\n".join(_statements("downgrade"))

    assert "REVOKE EXECUTE" in sql
    assert "DROP FUNCTION public.wp06_admin_bot_lifecycle_snapshot(uuid,uuid)" in sql
    assert "DROP POLICY wp06_admin_lifecycle_reservation_select" in sql
    assert "REVOKE SELECT (workspace_id, bot_id, status)" in sql
    assert "DROP POLICY wp06_admin_safe_outbox_insert" in sql
    assert "DROP POLICY wp06_admin_safe_outbox_returning_select" in sql
    assert "REVOKE INSERT ON TABLE public.outbox_event" in sql
