"""Kiểm migration cấp lại quyền EXECUTE cho trigger incident."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/f6c8e0a2b4d8_wp06_incident_guard_lock_execute.py"


def _statements(*, downgrade: bool = False) -> tuple[object, list[str]]:
    spec = importlib.util.spec_from_file_location("wp06_incident_guard_lock_execute", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    statements: list[str] = []
    migration.op = SimpleNamespace(execute=lambda statement: statements.append(str(statement)))
    getattr(migration, "downgrade" if downgrade else "upgrade")()
    return migration, statements


def test_incident_guard_execute_is_regranted_after_owner_transfer() -> None:
    migration, statements = _statements()

    assert migration.revision == "f6c8e0a2b4d8"
    assert migration.down_revision == "0094c0fdba7f"
    assert statements == [
        "GRANT EXECUTE ON FUNCTION public.wp06_lock_learning_incident_scope(uuid,uuid) "
        "TO crypidian_learning_incident_guard",
        "GRANT EXECUTE ON FUNCTION public.wp06_learning_incident_eligible_target(uuid,uuid) "
        "TO crypidian_learning_incident_guard",
    ]


def test_downgrade_revokes_only_the_incident_guard_execute_grant() -> None:
    _, statements = _statements(downgrade=True)

    assert statements == [
        "REVOKE EXECUTE ON FUNCTION public.wp06_lock_learning_incident_scope(uuid,uuid) "
        "FROM crypidian_learning_incident_guard",
        "REVOKE EXECUTE ON FUNCTION public.wp06_learning_incident_eligible_target(uuid,uuid) "
        "FROM crypidian_learning_incident_guard",
    ]
