"""Kiểm capability boundary của market producer migration mà không cần PostgreSQL."""

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

MIGRATION = (
    Path(__file__).parents[2]
    / "migrations"
    / "versions"
    / "d4f6a8c0e2b1_wp07_market_signal_producer.py"
)
WORKSPACE_POLICY = (
    "USING (workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid)"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("wp07_market_signal_migration", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_market_producer_migration_follows_telegram_delivery_role() -> None:
    migration = _load()

    assert migration.revision == "d4f6a8c0e2b1"
    assert migration.down_revision == "a1c3e5f7b9d2"


def test_market_producer_role_can_write_signals_but_not_execution_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load()
    statements: list[str] = []
    monkeypatch.setattr(
        migration.op,
        "execute",
        lambda statement: statements.append(str(statement)),
    )

    migration.upgrade()

    canonical = [re.sub(r"\s+", " ", item).strip() for item in statements]
    assert any("CREATE ROLE crypidian_market_signal_worker NOLOGIN" in item for item in canonical)
    assert any(
        "CREATE POLICY market_signal_account_select ON public.trading_account FOR SELECT" in item
        and "TO crypidian_market_signal_worker" in item
        and WORKSPACE_POLICY in item
        for item in canonical
    )
    for policy in (
        "market_signal_workspace_settings_select",
        "market_signal_risk_profile_select",
        "market_signal_strategy_release_select",
    ):
        assert any(
            f"CREATE POLICY {policy}" in item
            and "TO crypidian_market_signal_worker" in item
            and WORKSPACE_POLICY in item
            for item in canonical
        )
    assert "GRANT SELECT ON TABLE public.market_bar TO crypidian_market_signal_worker" in canonical
    assert (
        "GRANT SELECT, INSERT ON TABLE public.shared_signal_evidence "
        "TO crypidian_market_signal_worker" in canonical
    )
    assert (
        "GRANT SELECT, INSERT ON TABLE public.bot_decision TO crypidian_market_signal_worker"
        in canonical
    )
    assert any("event_type = 'signal.evidence.recorded.v1'" in item for item in canonical)
    assert any(
        "market_signal_decision_select" in item and "status = 'CANDIDATE'" not in item
        for item in canonical
    )
    assert any("WP07_MARKET_SIGNAL_WORKER_HAS_EXECUTION_PRIVILEGE" in item for item in canonical)
    assert not any(
        item.startswith("GRANT")
        and "crypidian_market_signal_worker" in item
        and any(
            table in item
            for table in ("order_intent", "exchange_order", "fill", "trade", "risk_reservation")
        )
        for item in canonical
    )
