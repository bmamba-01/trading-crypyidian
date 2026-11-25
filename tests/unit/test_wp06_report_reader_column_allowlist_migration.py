"""Hợp đồng column ACL cho report-reader WP06.15."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[2]
MIGRATION_PATH = ROOT / "migrations/versions/f0a2c4e6b8e0_wp06_report_reader_column_allowlist.py"


def _migration():
    spec = importlib.util.spec_from_file_location(
        "wp06_report_reader_column_allowlist", MIGRATION_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_report_reader_column_allowlist_covers_scoped_consumers_only() -> None:
    migration = _migration()

    assert migration.revision == "f0a2c4e6b8e0"
    assert migration.down_revision == "f0a2c4e6b8d9"
    assert migration.READER == "crypidian_admin_report_reader"
    assert set(migration.COLUMN_ALLOWLISTS) == {
        "bot_instance",
        "paper_account_seed",
        "trade",
        "trade_cost",
        "trade_plan",
        "order_intent",
        "fill",
        "position_event",
        "risk_reservation",
        "bot_decision",
        "proposal_backtest",
        "shared_signal_evidence",
        "analyst_run",
        "run_report",
        "bot_runtime_state",
        "outbox_event",
        "market_instrument",
        "market_bar",
        "market_mark_price",
        "market_live_quote",
        "ingestion_gap",
        "paper_execution_checkpoint",
        "signal_outcome",
        "telegram_signal_delivery",
    }
    assert "id" in migration.COLUMN_ALLOWLISTS["trade_plan"]
    assert "id" in migration.COLUMN_ALLOWLISTS["ingestion_gap"]
    assert "instrument_id" in migration.COLUMN_ALLOWLISTS["market_live_quote"]
    assert "received_at" in migration.COLUMN_ALLOWLISTS["market_mark_price"]
    assert {
        "strategy_release_id",
        "strategy_release_hash",
        "feature_snapshot_id",
        "candidate_id",
        "created_at",
    } <= set(migration.COLUMN_ALLOWLISTS["order_intent"])
    assert "canonical_json" not in migration.COLUMN_ALLOWLISTS["shared_signal_evidence"]
    assert "source_refs" not in migration.COLUMN_ALLOWLISTS["shared_signal_evidence"]
    assert "id" not in migration.COLUMN_ALLOWLISTS["paper_execution_checkpoint"]


def test_report_reader_migration_fails_closed_on_effective_acl_leaks() -> None:
    migration = _migration()
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert callable(migration._assert_no_effective_table_select)
    assert callable(migration._assert_no_effective_column_select_outside_allowlist)
    assert "_assert_no_effective_table_select(connection)" in source
    assert "_assert_no_effective_column_select_outside_allowlist(connection)" in source
    assert "pg_catalog.has_table_privilege" in source
    assert "pg_catalog.has_column_privilege" in source
    upgrade = source[source.index("def upgrade()") : source.index("def downgrade()")]
    assert upgrade.index("_revoke_reader_select(connection)") < upgrade.index(
        "_assert_no_effective_table_select(connection)"
    )
    assert upgrade.index("_grant_reader_columns(connection)") < upgrade.index(
        "_assert_no_effective_column_select_outside_allowlist(connection)"
    )
