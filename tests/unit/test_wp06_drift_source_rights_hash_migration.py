from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/c6e8a0b2d4f9_wp06_drift_source_rights_hash.py"


def _migration_module():
    spec = importlib.util.spec_from_file_location("wp06_drift_source_rights_hash", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _statements(action: str) -> str:
    module = _migration_module()
    statements: list[str] = []
    module.op = SimpleNamespace(execute=lambda sql: statements.append(str(sql)))
    getattr(module, action)()
    return "\n".join(statements)


def test_content_and_idempotency_use_lexical_json_in_python_key_order() -> None:
    sql = _statements("upgrade")
    assert "wp06_drift_assessment_idempotency_key" in sql
    assert "wp06_drift_assessment_content_hash" in sql
    assert "document := pg_catalog.json_build_object(" in sql
    assert "RETURN public.wp06_sha256_canonical_json(document);" in sql
    assert (
        "'source_rights_digest', row_value.source_rights_digest,\n                'window_end'"
        in sql
    )
    assert (
        "'source_rights_digest', row_value.source_rights_digest,\n                'workspace_id'"
        in sql
    )
    assert "GRANT EXECUTE" not in sql


def test_hash_downgrade_guards_all_ledgers_before_restoring_functions() -> None:
    sql = _statements("downgrade")
    for table in (
        "learning_shadow_observation",
        "learning_drift_assessment",
        "learning_drift_assessment_member",
    ):
        assert f"EXISTS (SELECT 1 FROM public.{table})" in sql
    guard = sql.index("WP06_DRIFT_SOURCE_RIGHTS_HASH_DOWNGRADE_POPULATED_DENIED")
    assert sql.index("IN ACCESS EXCLUSIVE MODE") < guard < sql.index("pg_get_functiondef")
