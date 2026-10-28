"""MB-04 fix: migration aggregate hardening (I1, I2, C3, minor) — kiểm tĩnh SQL được thực thi.

Hành vi thật (owner, grant, RLS, độ tươi mark, vốn âm, as_of tương lai) được kiểm trên
PostgreSQL ở `tests/e2e/test_workspace_risk_concurrency_e2e.py`.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
VERSIONS = ROOT / "migrations" / "versions"


def _module() -> ModuleType:
    path = next(VERSIONS.glob("f7a9c1e3b5d8_*.py"))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _executed(module: ModuleType, function: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    statements: list[str] = []

    class Op:
        def execute(self, statement: Any) -> None:
            statements.append(" ".join(str(statement).split()))

    monkeypatch.setattr(module, "op", Op())
    getattr(module, function)()
    return statements


def test_migration_chains_after_mb05_head_as_single_head() -> None:
    module = _module()
    assert module.revision == "f7a9c1e3b5d8"
    assert module.down_revision == "f7b9d1e3a5c8"
    parents = set()
    for path in VERSIONS.glob("*.py"):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        assert spec is not None
        assert spec.loader is not None
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        parents.add(loaded.down_revision)
    # Fix round 2 nối migration mark policy ngay sau; head mới là b3d5f7a9c1e4.
    assert "f7a9c1e3b5d8" in parents
    assert "b3d5f7a9c1e4" not in parents


def test_upgrade_owns_definer_function_by_guarded_nologin_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = "\n".join(_executed(_module(), "upgrade", monkeypatch))

    assert "CREATE ROLE crypidian_risk_aggregator NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT" in sql
    assert "MB04_RISK_AGGREGATOR_ROLE_UNSAFE" in sql
    for attribute in ("rolcanlogin", "rolsuper", "rolbypassrls", "rolinherit", "pg_auth_members"):
        assert attribute in sql
    for signature in (
        "public.workspace_account_risk_state(uuid, timestamptz, integer)",
        "public.get_workspace_risk_equity_aggregate(uuid, timestamptz, integer)",
    ):
        assert f"ALTER FUNCTION {signature} OWNER TO crypidian_risk_aggregator" in sql
        assert f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC" in sql
    # Aggregate definer chỉ cho runtime; hàm invoker (dưới RLS của role gọi) cho runtime + admin.
    assert (
        "GRANT EXECUTE ON FUNCTION public.get_workspace_risk_equity_aggregate(uuid, timestamptz, "
        "integer) TO crypidian_trading_runtime" in sql
    )
    assert "TO crypidian_trading_runtime, crypidian_admin_web" in sql
    assert "DROP FUNCTION public.get_workspace_risk_aggregate(uuid, timestamptz)" in sql
    assert sql.count("SET search_path = pg_catalog, pg_temp") == 2
    assert "SECURITY DEFINER" in sql
    assert "SECURITY INVOKER" in sql
    # Role aggregator chỉ đọc; policy SELECT giới hạn đúng workspace của transaction.
    assert "GRANT INSERT" not in sql
    assert "GRANT UPDATE" not in sql
    assert "GRANT DELETE" not in sql
    policies = re.findall(r"CREATE POLICY risk_aggregate_read ON public\.\"\w+\" FOR SELECT", sql)
    assert len(policies) == 8


def test_account_state_sql_fixes_review_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    sql = "\n".join(_executed(_module(), "upgrade", monkeypatch))
    state = sql[sql.index("CREATE FUNCTION public.workspace_account_risk_state") :]
    state = state[: state.index("CREATE FUNCTION public.get_workspace_risk_equity_aggregate")]

    # I2: độ tươi đo theo close_time (thời điểm quan sát), không theo available_at.
    assert "p_as_of - mk.close_time > (mk.close_time - mk.open_time)" in state
    # C3b: exposure tính mọi trade OPEN, không lọc opened_at.
    exposure = state[state.index("open_exposure AS") : state.index("pending AS")]
    assert "opened_at" not in exposure
    # C3c: RESERVED giữ budget theo đồng hồ thật khi as_of ở tương lai.
    assert "r.expires_at > least(p_as_of, now())" in state
    # Minor: vốn âm từng account là unknown; kết quả numeric không làm tròn half-up.
    assert "c.equity < 0" in state
    assert "numeric(38" not in state


def test_downgrade_restores_previous_functions_and_drops_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = "\n".join(_executed(_module(), "downgrade", monkeypatch))

    assert sql.index("DROP FUNCTION public.get_workspace_risk_equity_aggregate") < sql.index(
        "CREATE FUNCTION public.get_workspace_risk_equity_aggregate"
    )
    assert "CREATE OR REPLACE FUNCTION public.get_workspace_risk_aggregate" in sql
    assert "TO crypidian_trading_runtime, crypidian_admin_web" in sql
    assert sql.count("DROP POLICY risk_aggregate_read") == 8
    assert "REVOKE USAGE ON SCHEMA public FROM crypidian_risk_aggregator" in sql
    assert "DROP ROLE IF EXISTS crypidian_risk_aggregator" in sql


def _round2_module() -> ModuleType:
    path = next(VERSIONS.glob("b3d5f7a9c1e4_*.py"))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_round2_mark_policy_filters_source_timeframe_and_drops_bar_allowance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix round 2 (I2): LATERAL lọc nguồn/timeframe; tươi chỉ theo max_mark_age; nến đúng hình."""
    module = _round2_module()
    assert module.down_revision == "f7a9c1e3b5d8"
    sql = "\n".join(_executed(module, "upgrade", monkeypatch))

    assert "mb.source = p_mark_source AND mb.timeframe = ANY (p_mark_timeframes)" in sql
    assert "p_as_of - mk.close_time > make_interval(secs => p_max_mark_age_seconds)" in sql
    assert "(mk.close_time - mk.open_time) + make_interval" not in sql
    assert "WHEN '1d' THEN interval '1 day'" in sql
    assert "cardinality(p_mark_timeframes) = 0" in sql
    assert sql.count("OWNER TO crypidian_risk_aggregator") == 2
    assert sql.count("SET search_path = pg_catalog, pg_temp") == 2
    assert (
        "GRANT EXECUTE ON FUNCTION public.get_workspace_risk_equity_aggregate(uuid, timestamptz, "
        "integer, text, text[]) TO crypidian_trading_runtime" in sql
    )


def test_round2_downgrade_restores_round1_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    sql = "\n".join(_executed(_round2_module(), "downgrade", monkeypatch))

    previous = _module()
    assert " ".join(previous.ACCOUNT_STATE_FUNCTION.split()) in sql
    assert " ".join(previous.AGGREGATE_FUNCTION.split()) in sql
    assert (
        "GRANT EXECUTE ON FUNCTION public.get_workspace_risk_equity_aggregate(uuid, timestamptz, "
        "integer) TO crypidian_trading_runtime" in sql
    )
