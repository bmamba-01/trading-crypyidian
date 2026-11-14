"""Kiểm migration chuyển current-rights scorer sang fingerprint có license evidence."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/fb4d6e8a0c2f_wp06_scorer_license_fingerprint.py"


def _migration():
    assert MIGRATION.exists(), "Thiếu migration current-rights dùng fingerprint license-aware."
    spec = importlib.util.spec_from_file_location("wp06_scorer_license_fingerprint", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gateway_rewrite_uses_license_evidence_and_downgrades_exactly() -> None:
    migration = _migration()
    legacy = migration._LEGACY_PER_ITEM + "\n" + migration._LEGACY_CURRENT_POLICY

    updated = migration._rewrite_source(legacy, forward=True)

    assert migration._LICENSED_PER_ITEM in updated
    assert migration._LICENSED_CURRENT_POLICY in updated
    assert "wp06_scorer_source_policy_fingerprint(" not in updated
    assert migration._rewrite_source(updated, forward=False) == legacy


def test_gateway_rewrite_rejects_unexpected_function_body() -> None:
    migration = _migration()

    with pytest.raises(RuntimeError, match="PREIMAGE_UNEXPECTED"):
        migration._rewrite_source("unexpected", forward=True)


def test_migration_is_forward_only_over_the_current_wp06_head() -> None:
    migration = _migration()

    assert migration.revision == "fb4d6e8a0c2f"
    assert migration.down_revision == "fa2c4e6b8d0f"


def test_existing_license_fingerprint_keeps_legacy_hash_fallback() -> None:
    license_migration = (
        ROOT / "migrations/versions/a4c6e8f0b2d5_wp06_license_evidence.py"
    ).read_text(encoding="utf-8")

    assert "SELECT CASE WHEN policy.license_evidence_id IS NULL THEN" in license_migration
    assert "public.wp06_scorer_source_policy_fingerprint(" in license_migration
    assert "'license_evidence_id', policy.license_evidence_id::text" in license_migration
    assert "'license_digest', policy.license_digest" in license_migration
    assert "'license_scope_hash', policy.license_scope_hash" in license_migration
