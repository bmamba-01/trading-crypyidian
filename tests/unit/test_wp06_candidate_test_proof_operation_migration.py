"""Kiểm migration tách operation tạo TEST proof khỏi evaluator payload access."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/fa2c4e6b8d0f_wp06_candidate_test_proof_operation.py"


def _migration():
    assert MIGRATION.exists(), "Thiếu migration operation tạo TEST proof riêng cho builder."
    spec = importlib.util.spec_from_file_location("wp06_candidate_test_proof_operation", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture(function_name: str) -> str:
    statements: list[str] = []
    module = _migration()
    module.op = SimpleNamespace(execute=lambda sql: statements.append(str(sql)))
    getattr(module, function_name)()
    return "\n".join(statements)


def test_forward_migration_keeps_role_operation_pairs_and_proof_report_scope() -> None:
    migration = _migration()
    assert migration.revision == "fa2c4e6b8d0f"
    assert migration.down_revision == "f8e0a2c4d6be"
    sql = _capture("upgrade")

    assert "target_operation IN ('training', 'candidate_test_proof')" in sql
    assert "session_user <> 'crypidian_candidate_builder'" in sql
    assert "session_user <> 'crypidian_learning_evaluator'" in sql
    assert "target_operation = 'training'" in sql
    assert "target_operation = 'evaluation'" in sql
    assert "target_operation NOT IN ('training', 'evaluation', 'candidate_test_proof')" in sql
    assert "IF target_operation IN ('evaluation', 'candidate_test_proof') THEN" in sql
    assert "aggregate_report" in sql
    assert "aggregate_reporting" in sql
    assert "use_outputs := '[\"aggregate_report\"]'::jsonb;" in migration._NEW_OUTPUT_SCOPE
    assert "use_derived := '[\"aggregate_reporting\"]'::jsonb;" in migration._NEW_OUTPUT_SCOPE
    proof_scope = migration._NEW_OUTPUT_SCOPE.split("ELSE", maxsplit=1)[0]
    assert "derived_features" not in proof_scope
    assert "trained_model" not in proof_scope
    assert "GRANT INSERT" not in sql
    assert "ALTER FUNCTION" not in sql


def test_downgrade_restores_only_the_previous_operation_guard_and_scope_branch() -> None:
    sql = _capture("downgrade")

    assert "candidate_test_proof" in sql
    assert "pg_catalog.strpos(v_definition, 'candidate_test_proof') > 0" in sql
    assert "training', 'evaluation'" in sql
    assert "IF target_operation = 'evaluation' THEN" in sql
    assert "aggregate_report" in sql
    assert "IF target_operation = 'evaluation' THEN" in sql
    assert "GRANT" not in sql
    assert "REVOKE" not in sql
