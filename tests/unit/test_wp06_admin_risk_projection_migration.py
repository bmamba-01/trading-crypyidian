"""Hợp đồng migration cho projection SettingsAdmin không đọc raw ledger."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).parents[2]
MIGRATION_PATH = ROOT / "migrations/versions/f0a2c4e6b8d9_wp06_admin_risk_projection.py"


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("wp06_admin_risk_projection", MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_admin_risk_projection_follows_signal_reader_revision() -> None:
    migration = _migration()

    assert migration.revision == "f0a2c4e6b8d9"
    assert migration.down_revision == "f0a2c4e6b8d8"


def test_risk_capacity_projection_checks_active_workspace_role_and_returns_aggregates_only() -> (
    None
):
    migration = _migration()
    scope_sql = migration._ASSERT_SCOPE_SQL
    capacity_sql = migration._CAPACITY_SNAPSHOT_SQL

    assert "session_user <> 'crypidian_admin_web'" in scope_sql
    assert "app.workspace_id" in scope_sql
    assert "app.membership_id" in scope_sql
    assert "member.status = 'ACTIVE'" in scope_sql
    assert "('owner', 'strategy_admin', 'risk_admin')" in scope_sql
    assert "workspace_account_risk_state" in capacity_sql
    assert "has_unknown_equity" in capacity_sql
    assert "has_unknown_exposure" in capacity_sql
    assert "trade_id" not in capacity_sql
    assert "fill_id" not in capacity_sql
    assert "SECURITY DEFINER" in capacity_sql
    assert "SET row_security = on" in capacity_sql
    assert "SET search_path = pg_catalog, pg_temp" in capacity_sql


def test_paper_policy_projection_returns_only_active_counts() -> None:
    migration = _migration()
    sql = migration._PAPER_POLICY_SNAPSHOT_SQL

    assert "active_intents integer" in sql
    assert "open_positions integer" in sql
    assert "active_reservations integer" in sql
    assert "intent.status IN ('ACCEPTED', 'PARTIALLY_FILLED', 'UNKNOWN')" in sql
    assert "trade.status = 'OPEN'" in sql
    assert "reservation.status IN ('RESERVED', 'UNKNOWN')" in sql
    assert "SELECT count(*)::integer" in sql
    assert "intent.id" not in sql
    assert "trade.id" not in sql
    assert "reservation.id" not in sql


def test_admin_safe_only_executes_projections_and_aggregator_reads_scoped_columns() -> None:
    migration = _migration()

    source = Path(MIGRATION_PATH).read_text(encoding="utf-8")
    assert len(migration._functions()) == 3
    assert "GRANT SELECT (workspace_id, id, status) ON TABLE public.user_membership " in source
    assert (
        "GRANT SELECT (workspace_id, membership_id, role) ON TABLE public.role_binding " in source
    )
    assert "GRANT SELECT (workspace_id, mode, status) ON TABLE public.order_intent " in source
    assert "TO {ADMIN_SAFE}" in source
    assert "GRANT SELECT ON TABLE public.trade" not in source
    assert "GRANT SELECT ON TABLE public.fill" not in source
    assert "GRANT EXECUTE ON FUNCTION public.{function}({signature}) TO {ADMIN_SAFE}" in source
    assert "DROP FUNCTION public.{CAPACITY_SNAPSHOT}" in source
