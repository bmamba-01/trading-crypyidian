"""Kiểm hợp đồng bảo mật của migration bảo vệ trigger reservation WP-03."""

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

MIGRATION = (
    Path(__file__).parents[2]
    / "migrations"
    / "versions"
    / "f4a6c8d0e2b5_wp03_risk_reservation_guard.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("wp03_risk_reservation_guard", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _statements(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    recorded: list[str] = []
    monkeypatch.setattr(module.op, "execute", lambda statement: recorded.append(str(statement)))
    module.upgrade()
    return [re.sub(r"\s+", " ", statement).strip() for statement in recorded]


def test_upgrade_is_linear_and_keeps_the_trigger_function_invoker_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()

    assert module.revision == "f4a6c8d0e2b5"
    assert module.down_revision == "e0b2d4f6a8c1"

    statements = _statements(module, monkeypatch)
    function = next(
        statement for statement in statements if "CREATE OR REPLACE FUNCTION" in statement
    )
    acl_cleanup = next(statement for statement in statements if "aclexplode" in statement)
    assert "CREATE OR REPLACE FUNCTION public.protect_risk_reservation_transition()" in function
    assert "SECURITY INVOKER" in function
    assert "search_path = pg_catalog, pg_temp" in function
    assert function.count("FROM public.venue_order_intent") == 4
    assert "FROM venue_order_intent" not in function
    assert any("OWNER TO crypidian_risk_reservation_guard" in statement for statement in statements)
    assert "privilege.grantee <> function.proowner" in acl_cleanup
    assert "REVOKE EXECUTE ON FUNCTION public.protect_risk_reservation_transition()" in acl_cleanup
    assert "CASCADE" in acl_cleanup
    assert statements.index(acl_cleanup) < next(
        index
        for index, statement in enumerate(statements)
        if "ALTER FUNCTION public.protect_risk_reservation_transition() OWNER TO" in statement
    )


def test_downgrade_fails_closed_without_reinstalling_the_unsafe_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    statements: list[str] = []
    monkeypatch.setattr(module.op, "execute", lambda statement: statements.append(str(statement)))

    with pytest.raises(RuntimeError, match="WP03_RISK_GUARD_DOWNGRADE_BLOCKED"):
        module.downgrade()

    assert statements == []
