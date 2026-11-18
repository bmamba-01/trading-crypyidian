"""Kiểm migration current-rights dựng cùng scope payload với receipt license-aware."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/e2f4a6c8b0d5_wp06_scorer_rights_scope_fingerprint.py"


def _migration():
    assert MIGRATION.exists(), "Thiếu migration đồng bộ current-rights scope fingerprint."
    spec = importlib.util.spec_from_file_location("wp06_scorer_rights_scope_fingerprint", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_current_rights_hash_uses_license_aware_bundle_gate_scope() -> None:
    migration = _migration()

    updated = migration._rewrite_current_rights_source(
        migration._LEGACY_TRAINING_SCOPE, forward=True
    )

    assert migration._LICENSED_TRAINING_SCOPE in updated
    assert "wp06_authorize_replay_training_bundle" in updated
    assert "'evaluation'" in updated
    for field in (
        "product",
        "venue",
        "contract",
        "license_evidence_id",
        "evidence_digest",
        "license_scope_hash",
        "scope_hash",
        "operation",
    ):
        assert f"'{field}'" in updated
    assert migration._rewrite_current_rights_source(updated, forward=False) == (
        migration._LEGACY_TRAINING_SCOPE
    )


def test_license_scope_json_key_order_matches_python_canonical_serializer() -> None:
    migration = _migration()
    keys = tuple(re.findall(r"(?m)^\s+'([a-z_]+)',", migration._LICENSED_TRAINING_SCOPE))

    assert keys == tuple(sorted(keys))
    assert keys == (
        "bars_content_hash",
        "bundle_hash",
        "bundle_policy_fingerprint",
        "child_dataset_hash",
        "contract",
        "current_policy_fingerprint",
        "data_end",
        "data_start",
        "evidence_digest",
        "interval",
        "license_evidence_id",
        "license_scope_hash",
        "operation",
        "policy_fingerprint",
        "policy_id",
        "product",
        "provider",
        "purpose",
        "scope_hash",
        "symbol",
        "terms_url",
        "venue",
        "verified_at",
        "workspace_id",
    )


def test_bundle_gate_allows_only_the_nested_admin_current_rights_identity() -> None:
    migration = _migration()

    updated = migration._rewrite_bundle_gate_source(
        migration._LEGACY_EVALUATION_CALLER_GUARD, forward=True
    )

    assert (
        "session_user NOT IN ('crypidian_learning_evaluator', 'crypidian_admin_web', "
        "'crypidian_trading_runtime', 'crypidian_market_signal_worker')" in updated
    )
    assert migration._rewrite_bundle_gate_source(updated, forward=False) == (
        migration._LEGACY_EVALUATION_CALLER_GUARD
    )


def test_migration_grants_nested_gate_only_to_the_current_rights_definer() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    upgrade = source.split("def upgrade() -> None:", 1)[1].split("def downgrade() -> None:", 1)[0]
    downgrade = source.split("def downgrade() -> None:", 1)[1]

    assert "TO {RIGHTS_ROLE}" in upgrade
    assert "REVOKE EXECUTE ON FUNCTION {BUNDLE_GATE_SIGNATURE}" in downgrade
    assert "TO PUBLIC" not in upgrade
    assert "TO crypidian_admin_web" not in upgrade


def test_migration_is_a_forward_child_of_exact_scope_head() -> None:
    migration = _migration()

    assert migration.revision == "e2f4a6c8b0d5"
    assert migration.down_revision == "e2f4a6c8b0d4"


def test_rewriters_reject_unexpected_function_bodies() -> None:
    migration = _migration()

    with pytest.raises(RuntimeError, match="PREIMAGE_UNEXPECTED"):
        migration._rewrite_current_rights_source("unexpected", forward=True)
    with pytest.raises(RuntimeError, match="PREIMAGE_UNEXPECTED"):
        migration._rewrite_bundle_gate_source("unexpected", forward=True)


def test_current_rights_rewriter_accepts_postgresql_whitespace_normalization() -> None:
    migration = _migration()
    formatted = migration._LEGACY_TRAINING_SCOPE.replace(
        "SELECT COALESCE(pg_catalog.json_agg",
        "SELECT  COALESCE(pg_catalog.json_agg",
        1,
    )

    updated = migration._rewrite_current_rights_source(formatted, forward=True)

    assert updated == migration._LICENSED_TRAINING_SCOPE
