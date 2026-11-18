from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from trading_bot.adapters.database.models import (
    BotDecisionJobSourceBarRecord,
    BotDecisionRecord,
)
from trading_bot.adapters.database.models.postgres_types import Xid8

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/f8e0a2c4d6be_wp06_source_bar_atomic_commit.py"


def _migration():
    assert MIGRATION.exists(), "Thiếu migration đóng source-bar lineage theo transaction."
    spec = importlib.util.spec_from_file_location("wp06_source_bar_atomic_commit", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _statements(operation: str) -> list[str]:
    statements: list[str] = []
    module = _migration()
    module.op = SimpleNamespace(execute=lambda sql: statements.append(str(sql)))
    getattr(module, operation)()
    return statements


def test_source_bar_atomic_commit_migration_chains_after_report_disclosure_gate() -> None:
    migration = _migration()

    assert migration.revision == "f8e0a2c4d6be"
    assert migration.down_revision == "f8e0a2c4d6bd"


def test_decision_insert_stamps_database_transaction_id_and_source_bar_requires_it() -> None:
    sql = "\n".join(_statements("upgrade"))
    decision_start = sql.index("CREATE FUNCTION public.wp06_stamp_bot_decision_source_bar_xid()")
    decision_end = sql.index("CREATE TRIGGER", decision_start)
    decision_stamp = sql[decision_start:decision_end]
    source_start = sql.index("CREATE FUNCTION public.wp06_require_source_bar_parent_transaction()")
    source_end = sql.index("CREATE TRIGGER", source_start)
    source_guard = sql[source_start:source_end]

    assert "RETURNS trigger" in decision_stamp
    assert "pg_catalog.pg_current_xact_id()" in decision_stamp
    assert "NEW.source_bar_created_xid" in decision_stamp
    assert "decision.source_bar_created_xid" in source_guard
    assert "parent_xid IS DISTINCT FROM pg_catalog.pg_current_xact_id()" in source_guard
    assert "WP06_SOURCE_BAR_PARENT_TRANSACTION_MISMATCH" in source_guard
    assert "session_user" not in source_guard


def test_source_bar_writer_check_runs_before_mutation_and_downgrade_serializes_writers() -> None:
    sql = "\n".join(_statements("upgrade"))
    downgrade = "\n".join(_statements("downgrade"))

    assert "ADD COLUMN source_bar_created_xid xid8" in sql
    assert "CREATE TRIGGER zz_wp06_stamp_bot_decision_source_bar_xid" in sql
    assert "BEFORE INSERT ON public.bot_decision" in sql
    assert "CREATE TRIGGER bot_decision_source_bar_parent_transaction" in sql
    assert "BEFORE INSERT ON public.bot_decision_source_bar" in sql
    assert "AFTER INSERT ON public.bot_decision_source_bar" not in sql
    assert "LOCK TABLE public.bot_decision, public.bot_decision_source_bar " in downgrade
    lock_at = downgrade.index("LOCK TABLE public.bot_decision, public.bot_decision_source_bar")
    drop_at = downgrade.index("DROP COLUMN source_bar_created_xid")
    assert lock_at < drop_at
    assert "DROP TRIGGER bot_decision_source_bar_parent_transaction" in downgrade


def test_parent_transaction_trigger_precedes_existing_bar_validation_trigger() -> None:
    migration_sql = "\n".join(_statements("upgrade"))
    source_bar_migration = (
        ROOT / "migrations/versions/d5e7f9a1c3b6_wp06_decision_source_bars.py"
    ).read_text(encoding="utf-8")

    assert "bot_decision_source_bar_validate" in source_bar_migration
    assert "bot_decision_source_bar_parent_transaction" < "bot_decision_source_bar_validate"
    assert (
        "CREATE TRIGGER bot_decision_source_bar_parent_transaction BEFORE INSERT "
        "ON public.bot_decision_source_bar"
    ) in migration_sql


def test_orm_metadata_matches_transaction_marker_and_job_bar_indexes() -> None:
    decision_column = BotDecisionRecord.__table__.c.source_bar_created_xid
    assert isinstance(decision_column.type, Xid8)
    assert decision_column.nullable

    table = BotDecisionJobSourceBarRecord.__table__
    assert set(table.columns.keys()) == {
        "id",
        "created_at",
        "workspace_id",
        "bot_id",
        "job_id",
        "evidence_id",
        "evidence_hash",
        "decision_snapshot_id",
        "decision_time",
        "timeframe",
        "feature_snapshot_id",
        "market_bar_id",
        "source_event_id",
        "source_event_digest",
        "ordinal",
        "timeframe_bar_count",
        "snapshot_timeframe_count",
    }
    assert {index.name for index in table.indexes} == {
        "uq_bot_decision_job_source_bar_ordinal",
        "uq_bot_decision_job_source_bar_market_bar",
        "ix_bot_decision_job_source_bar_job",
    }
    assert {constraint.name for constraint in table.constraints} >= {
        "bot_decision_job_source_bar_pkey",
        "fk_bot_decision_job_source_bar_job",
        "fk_bot_decision_job_source_bar_evidence",
        "fk_bot_decision_job_source_bar_market_bar",
        "ck_bot_decision_job_source_bar_evidence_hash",
        "ck_bot_decision_job_source_bar_snapshot_hash",
        "ck_bot_decision_job_source_bar_feature_hash",
        "ck_bot_decision_job_source_bar_event_id",
        "ck_bot_decision_job_source_bar_event_digest",
        "ck_bot_decision_job_source_bar_ordinal",
        "ck_bot_decision_job_source_bar_counts",
    }
    assert list(table.primary_key.columns.keys()) == ["id"]
