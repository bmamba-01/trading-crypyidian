"""Kiểm contract migration proof TEST v2/v3 mà không chạy database giả."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/f0a2c4e6b8d1_wp06_scorer_test_proof_v3.py"
SOURCE_MIGRATION = ROOT / "migrations/versions/b2d4f6a8c0e1_wp06_decision_opportunity_universe.py"


def _migration():
    assert MIGRATION.exists(), "Thiếu migration proof TEST v3."
    spec = importlib.util.spec_from_file_location("wp06_scorer_test_proof_v3", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture(direction: str) -> tuple[list[tuple[str, tuple, dict]], str]:
    module = _migration()
    calls: list[tuple[str, tuple, dict]] = []

    def capture(name: str):
        def operation(*args, **kwargs):
            calls.append((name, args, kwargs))
            return None

        return operation

    module.op = SimpleNamespace(
        add_column=capture("add_column"),
        drop_column=capture("drop_column"),
        drop_constraint=capture("drop_constraint"),
        create_check_constraint=capture("create_check_constraint"),
        execute=lambda sql: calls.append(("execute", (str(sql),), {})),
    )
    getattr(module, direction)()
    sql = "\n".join(args[0] for name, args, _ in calls if name == "execute")
    return calls, sql


def test_migration_adds_nullable_count_after_current_head_without_acl_expansion() -> None:
    module = _migration()
    calls, sql = _capture("upgrade")

    assert module.revision == "f0a2c4e6b8d1"
    assert module.down_revision == "c6e8a0b2d4f9"
    add_column = next(args for name, args, _ in calls if name == "add_column")
    column = add_column[1]
    assert column.name == "not_run_count"
    assert column.nullable is True
    assert "CREATE OR REPLACE FUNCTION" in sql
    assert "wp06-scorer-test-opportunity-proof-v2" in sql
    assert "wp06-scorer-test-opportunity-proof-v3" in sql
    assert "not_run_count" in sql
    assert "jsonb_typeof(v_entry->'reason_code') = 'string'" in sql
    assert "input_excluded" in sql
    assert "not_run" in sql
    assert "reason_code" in sql
    assert "GRANT" not in sql
    assert "REVOKE" not in sql


def test_existing_builder_metadata_select_is_reused_without_changing_historical_grants() -> None:
    source = SOURCE_MIGRATION.read_text(encoding="utf-8")

    assert "GRANT SELECT ON TABLE public.{TABLE} TO {BUILDER}" in source
    assert "reason_code" in source
    assert "GRANT" not in _capture("upgrade")[1]


def test_downgrade_guards_v3_before_restoring_v2_validator_and_removing_count() -> None:
    calls, sql = _capture("downgrade")
    lock_index = sql.index("LOCK TABLE")
    guard_index = sql.index("WP06_SCORER_TEST_PROOF_V3_DOWNGRADE_POPULATED_DENIED")
    validator_index = sql.index("CREATE OR REPLACE FUNCTION")
    drop_column_index = next(
        index for index, (name, args, _) in enumerate(calls) if name == "drop_column"
    )
    execute_indices = [index for index, (name, _, _) in enumerate(calls) if name == "execute"]

    assert lock_index < guard_index < validator_index
    assert drop_column_index > max(execute_indices)
    assert "wp06-scorer-test-opportunity-proof-v2" in sql
    assert "not_run_count IS NOT NULL" in sql
