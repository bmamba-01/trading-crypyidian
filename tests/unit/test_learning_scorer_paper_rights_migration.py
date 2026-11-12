"""Khóa quyền PAPER/SHADOW sau verifier TRAINING, không sao chép lineage."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.unit.test_learning_scorer_receipt_projection_migration import (
    _migration as _projection_migration,
)
from tests.unit.test_learning_scorer_receipt_projection_migration import (
    _original_definition,
    _source,
)

MIGRATION = (
    Path(__file__).parents[2] / "migrations/versions/f6b8d0e2a4c7_wp06_scorer_paper_rights.py"
)


def _migration():
    assert MIGRATION.exists(), "Thiếu migration quyền TRAINING + PAPER_DEMO cho scorer active"
    spec = importlib.util.spec_from_file_location("scorer_paper_rights_migration", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _definition():
    definition, source = _original_definition()
    return _projection_migration()._rewrite_definition(definition, source, forward=True)


def test_deployment_preflights_all_canonical_sources_and_bundle_before_report_lookup() -> None:
    migration = _migration()
    definition = _definition()
    fixed = migration._rewrite_definition(definition, _source(definition), forward=True)
    assert (
        fixed.index("wp06_require_scorer_paper_source_rights(")
        < fixed.index("SELECT report.id, report.evaluation_run_id")
        < fixed.index("wp06_recheck_scorer_current_rights(")
    )
    assert fixed.index("pg_advisory_xact_lock") < fixed.index(
        "wp06_require_scorer_paper_source_rights("
    )
    assert "source_proof.attempt_id = artifact_row.attempt_id" in fixed
    assert "source_proof.fold_id = artifact_row.fold_id" in fixed
    assert "ARRAY['BINANCE_USDM_REST','CRYPIDIAN_FIRST_PARTY_QA']" in fixed
    assert "public.wp06_scorer_manifest_source_refs(" in fixed
    assert "manifest.manifest_hash = artifact_row.manifest_hash" in fixed
    assert "bundle.bundle_hash = source_proof.bundle_hash" in fixed
    assert "SELECT bundle.rights_provider" in fixed
    assert "ORDER BY paper_source.source_id" in fixed
    assert "WP06_SCORER_DEPLOYMENT_QA_SOURCE_DENIED" in fixed
    assert "CREATE FUNCTION public.wp06_recheck_scorer_current_rights(" not in fixed
    assert migration.down_revision == "f5b7d9e1a3c6"
    assert migration._rewrite_definition(fixed, _source(fixed), forward=False) == definition


@pytest.mark.parametrize("forward", [True, False])
def test_deployment_rewrite_rejects_unrelated_drift(forward: bool) -> None:
    migration = _migration()
    definition = _definition()
    if not forward:
        definition = migration._rewrite_definition(definition, _source(definition), forward=True)
    definition = definition.replace("WP06_SCORER_DEPLOYMENT_QA_SOURCE_DENIED", "QA_GUARD_DRIFT")
    with pytest.raises(RuntimeError, match="PREIMAGE_UNEXPECTED"):
        migration._rewrite_definition(definition, _source(definition), forward=forward)


def test_private_paper_gate_requires_both_purposes_and_rechecks_after_writer_lock() -> None:
    migration = _migration()
    statements = []
    migration.op = SimpleNamespace(execute=lambda sql: statements.append(str(sql)))
    migration._create_paper_source_gate()
    sql = "\n".join(statements)
    assert '@> \'["training","paper_demo"]\'::jsonb' in sql
    assert sql.index("pg_advisory_xact_lock") < sql.index("clock_timestamp()")
    assert sql.index("pg_advisory_xact_lock") < sql.index("ORDER BY policy.sequence DESC LIMIT 1")
    assert "policy_row.state IS DISTINCT FROM 'verified'" in sql
    assert "policy_row.valid_until <= checked_at" in sql
    assert "policy_row.reviewed_at > checked_at" in sql
    assert "source_policy_observation" in sql
    assert "CRYPIDIAN_FIRST_PARTY_QA" in sql
    assert "SET search_path = pg_catalog, pg_temp" in sql
    assert "OWNER TO crypidian_scorer_deployment_rights_reader" in sql
    assert "FROM PUBLIC" in sql
    assert "TO crypidian_admin_web" not in sql


def test_deployment_rechecks_report_bound_sources_after_current_rights_snapshot() -> None:
    """Proof/report xuất hiện sau preflight không được dùng cache source để vượt gate cuối."""
    migration = _migration()
    definition = _definition()
    fixed = migration._rewrite_definition(definition, _source(definition), forward=True)
    late = fixed.split("wp06_recheck_scorer_current_rights(", 1)[1]
    assert "wp06_require_scorer_paper_source_rights(" in late
    assert "bundle.bundle_hash = proof_row.bundle_hash" in late
    assert "public.wp06_scorer_manifest_source_refs(" in late
    assert "manifest.manifest_hash = artifact_row.manifest_hash" in late
    assert "unnest(paper_source_ids)" not in late
    assert late.index("wp06_require_scorer_paper_source_rights(") < late.index("accepted_shadow :=")
