"""Kiểm migration current-rights chỉ đọc policy qua cột metadata được cấp."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/e2f4a6c8b0d3_wp06_scorer_policy_metadata_fingerprint.py"


def _migration():
    assert MIGRATION.exists(), "Thiếu migration dùng policy fingerprint qua các cột được cấp."
    spec = importlib.util.spec_from_file_location(
        "wp06_scorer_policy_metadata_fingerprint", MIGRATION
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_current_rights_policy_fingerprint_uses_only_explicit_metadata_columns() -> None:
    migration = _migration()

    updated = migration._rewrite_source(migration._WHOLE_ROW_LICENSED_POLICY, forward=True)

    assert migration._SCOPED_LICENSED_POLICY in updated
    assert "fingerprint(policy)" not in updated
    for field in (
        "workspace_id",
        "source_id",
        "sequence",
        "policy_id",
        "state",
        "allowed_purposes",
        "license_digest",
        "license_evidence_id",
        "license_scope_hash",
    ):
        assert f"policy.{field}" in updated
    assert migration._rewrite_source(updated, forward=False) == migration._WHOLE_ROW_LICENSED_POLICY


def test_current_rights_policy_rewrite_rejects_unexpected_function_body() -> None:
    migration = _migration()

    with pytest.raises(RuntimeError, match="PREIMAGE_UNEXPECTED"):
        migration._rewrite_source("unexpected", forward=True)


def test_policy_metadata_fingerprint_migration_is_forward_only_over_v5_head() -> None:
    migration = _migration()

    assert migration.revision == "e2f4a6c8b0d3"
    assert migration.down_revision == "e2f4a6c8b0d2"


def test_policy_metadata_fingerprint_migration_does_not_expand_table_grants() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    upgrade = source.split("def upgrade() -> None:", 1)[1].split("def downgrade() -> None:", 1)[0]
    downgrade = source.split("def downgrade() -> None:", 1)[1]

    assert "GRANT SELECT ON public.source_policy_revision" not in upgrade
    assert "GRANT SELECT (" not in upgrade
    assert "ALTER FUNCTION" not in upgrade
    assert "ALTER FUNCTION" not in downgrade
