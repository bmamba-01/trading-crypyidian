from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from trading_bot.adapters.database.repositories.learning_scorer_current_rights import (
    LearningScorerCurrentRightsRepository,
    ScorerDeploymentRightsEvidence,
)


def _evidence(**overrides: object) -> ScorerDeploymentRightsEvidence:
    values: dict[str, object] = {
        "workspace_id": uuid4(),
        "scorer_artifact_id": uuid4(),
        "evaluation_report_id": uuid4(),
        "shadow_eligible": True,
        "paper_gate_eligible": False,
        "evaluation_status": "ACCEPTED_SHADOW_ONLY",
        "candidate_completeness_verified": False,
        "scored_candidate_coverage_verified": False,
        "fingerprint": "a" * 64,
        "checked_at": datetime.now(UTC),
        "artifact_hash": "b" * 64,
        "report_hash": "c" * 64,
        "proof_hash": "d" * 64,
        "policy_hash": "e" * 64,
        "bundle_hash": "f" * 64,
        "source_scope_hash": "1" * 64,
        "training_membership_digest": "2" * 64,
        "validation_membership_digest": "3" * 64,
    }
    values.update(overrides)
    return ScorerDeploymentRightsEvidence(**values)  # type: ignore[arg-type]


def test_shadow_can_be_eligible_without_complete_candidate_universe() -> None:
    evidence = _evidence()

    assert evidence.shadow_eligible is True
    assert evidence.paper_gate_eligible is False
    assert evidence.candidate_completeness_verified is False


def test_paper_gate_requires_accepted_complete_report() -> None:
    evidence = _evidence(
        shadow_eligible=True,
        paper_gate_eligible=True,
        evaluation_status="ACCEPTED_PAPER_GATE_ELIGIBLE",
        candidate_completeness_verified=True,
        scored_candidate_coverage_verified=True,
    )

    assert evidence.shadow_eligible is True
    assert evidence.paper_gate_eligible is True

    with pytest.raises(ValueError, match="PAPER_GATE"):
        _evidence(
            shadow_eligible=True,
            paper_gate_eligible=True,
            evaluation_status="ACCEPTED_PAPER_GATE_ELIGIBLE",
            candidate_completeness_verified=False,
            scored_candidate_coverage_verified=True,
        )

    with pytest.raises(ValueError, match="PAPER_GATE"):
        _evidence(
            shadow_eligible=True,
            paper_gate_eligible=True,
            evaluation_status="ACCEPTED_PAPER_GATE_ELIGIBLE",
            candidate_completeness_verified=True,
            scored_candidate_coverage_verified=False,
        )


def test_inconclusive_report_cannot_be_eligible_for_either_mode() -> None:
    with pytest.raises(ValueError, match="INCONCLUSIVE"):
        _evidence(
            shadow_eligible=True,
            evaluation_status="INCONCLUSIVE",
            candidate_completeness_verified=False,
        )

    evidence = _evidence(
        shadow_eligible=False,
        evaluation_status="INCONCLUSIVE",
        candidate_completeness_verified=False,
    )
    assert evidence.shadow_eligible is False
    assert evidence.paper_gate_eligible is False


def test_gateway_passes_only_authoritative_lineage_identifiers() -> None:
    workspace_id, artifact_id, report_id = uuid4(), uuid4(), uuid4()
    row = {
        "workspace_id": workspace_id,
        "scorer_artifact_id": artifact_id,
        "evaluation_report_id": report_id,
        "shadow_eligible": True,
        "paper_gate_eligible": False,
        "evaluation_status": "ACCEPTED_SHADOW_ONLY",
        "candidate_completeness_verified": False,
        "scored_candidate_coverage_verified": False,
        "fingerprint": "a" * 64,
        "checked_at": datetime.now(UTC),
        "artifact_hash": "b" * 64,
        "report_hash": "c" * 64,
        "proof_hash": "d" * 64,
        "policy_hash": "e" * 64,
        "bundle_hash": "f" * 64,
        "source_scope_hash": "1" * 64,
        "training_membership_digest": "2" * 64,
        "validation_membership_digest": "3" * 64,
    }
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(),
                SimpleNamespace(mappings=lambda: SimpleNamespace(one=lambda: row)),
            ]
        ),
        scalar=AsyncMock(return_value="read committed"),
    )

    evidence = asyncio.run(
        LearningScorerCurrentRightsRepository(session).verify_deployment_rights(
            workspace_id=workspace_id,
            scorer_artifact_id=artifact_id,
            evaluation_report_id=report_id,
        )
    )

    statement, parameters = session.execute.await_args.args
    assert "wp06_verify_scorer_deployment_rights" in str(statement)
    assert set(parameters) == {"workspace_id", "scorer_artifact_id", "evaluation_report_id"}
    assert parameters == {
        "workspace_id": str(workspace_id),
        "scorer_artifact_id": str(artifact_id),
        "evaluation_report_id": str(report_id),
    }
    assert evidence.workspace_id == workspace_id
    assert evidence.fingerprint == "a" * 64


def test_scorer_source_locks_use_default_database_collation_across_readers_and_writers() -> None:
    root = Path(__file__).parents[2]
    migration = root / "migrations/versions/d7f9a1b3c5e8_wp06_scorer_current_rights.py"
    candidate_seal = root / "src/trading_bot/application/learning_split_candidate.py"
    migration_source = migration.read_text(encoding="utf-8")
    candidate_source = candidate_seal.read_text(encoding="utf-8")

    assert "ORDER BY lock_source.source_id\n" in migration_source
    assert "ORDER BY reference.source_id\n" in migration_source
    assert 'ORDER BY lock_source.source_id COLLATE "C"' not in migration_source
    assert 'ORDER BY reference.source_id COLLATE "C"' not in migration_source
    assert 'ORDER BY source_id"' in candidate_source
    assert 'ORDER BY source_id COLLATE "C"' not in candidate_source


def test_c3_evaluation_policy_is_bound_to_report_not_calibration_policy() -> None:
    migration = (
        Path(__file__).parents[2] / "migrations/versions/d7f9a1b3c5e8_wp06_scorer_current_rights.py"
    ).read_text(encoding="utf-8")

    assert "access_row.policy_hash IS DISTINCT FROM artifact_row.calibration_policy_hash" not in (
        migration
    )
    assert "access_row.policy_hash !~ '^[0-9a-f]{64}$'" in migration
    assert "receipt_row.policy_hash IS DISTINCT FROM report_row.policy_hash" in migration


def test_manifest_source_projection_checks_canonical_v1_v2_and_v3_without_extra_grants() -> None:
    migration = (
        Path(__file__).parents[2] / "migrations/versions/d7f9a1b3c5e8_wp06_scorer_current_rights.py"
    ).read_text(encoding="utf-8")

    assert "manifest_version := canonical_manifest->>'manifest_version'" in migration
    assert "IF canonical_manifest IS NULL OR manifest_version NOT IN" in migration
    assert "WP06_SCORER_RIGHTS_LEGACY_SOURCE_LINEAGE_DENIED" in migration
    assert "WP06_SCORER_RIGHTS_TYPED_SOURCE_LINEAGE_INVALID" in migration
    assert "manifest_version NOT IN (" in migration
    assert (
        "'manifest_version'"
        not in migration.split("SELECT manifest.manifest,", 1)[1].split(
            "FROM public.dataset_manifest", 1
        )[0]
    )


def test_training_sample_binds_outcome_by_workspace_id_and_hash_not_dataset_manifest_hash() -> None:
    migration = (
        Path(__file__).parents[2] / "migrations/versions/d7f9a1b3c5e8_wp06_scorer_current_rights.py"
    ).read_text(encoding="utf-8")
    current_rights_function = migration.split(
        "CREATE FUNCTION public.wp06_recheck_scorer_current_rights(", 1
    )[1].split("END $$", 1)[0]

    assert "outcome.id::text = sample.outcome_id" in current_rights_function
    assert "outcome.outcome_hash = sample.outcome_hash" in current_rights_function
    assert "outcome.manifest_hash = sample.manifest_hash" not in current_rights_function


def test_market_signal_worker_has_only_outer_scorer_rights_execute_boundary() -> None:
    migration = (
        Path(__file__).parents[2] / "migrations/versions/d7f9a1b3c5e8_wp06_scorer_current_rights.py"
    ).read_text(encoding="utf-8")

    assert 'MARKET_SIGNAL_WORKER = "crypidian_market_signal_worker"' in migration
    assert "session_user NOT IN ('{ADMIN}','{RUNTIME}','{MARKET_SIGNAL_WORKER}')" in migration
    assert 'f"TO {ADMIN}, {RUNTIME}, {MARKET_SIGNAL_WORKER}"' in migration
    assert "has_table_privilege(session_user, 'public.' || table_name, 'INSERT')" in (
        Path(__file__).parents[2] / "tests/e2e/probes/wp06_source_linked_scorer_test.py"
    ).read_text(encoding="utf-8")


def test_rights_gateway_selects_only_source_linkage_columns_needed_for_exact_run_binding() -> None:
    migration = (
        Path(__file__).parents[2] / "migrations/versions/d7f9a1b3c5e8_wp06_scorer_current_rights.py"
    ).read_text(encoding="utf-8")

    assert '"workspace_id,id,run_id,trial_record_id,manifest_hash,outcome_hash,"' in migration
    assert (
        '"workspace_id,run_id,trial_record_id,manifest_hash,event_id,schema_version,input_state,"'
        in migration
    )
    assert "opportunity.run_id = outcome.run_id" in migration
    assert "opportunity.manifest_hash = outcome.manifest_hash" in migration
    probe = (
        Path(__file__).parents[2] / "tests/e2e/probes/wp06_source_linked_scorer_test.py"
    ).read_text(encoding="utf-8")
    assert "'run_id','SELECT'" in probe
    assert "'outcome','SELECT'" in probe
    assert "'source_event_digests','SELECT'" in probe


def test_bundle_backfill_purpose_is_distinct_from_current_training_permission() -> None:
    migration = (
        Path(__file__).parents[2] / "migrations/versions/d7f9a1b3c5e8_wp06_scorer_current_rights.py"
    ).read_text(encoding="utf-8")

    assert "bundle_row.rights_purpose <> 'BACKTEST_RESEARCH'" in migration
    assert "current_policy_purposes @> '[\"training\"]'::jsonb" in migration
    assert "policy_row.allowed_purposes @> '[\"training\"]'::jsonb" in migration
    assert "child.policy_fingerprint = bundle_row.policy_fingerprint" in migration
    assert "child.manifest_json->>'policy_fingerprint' = child.policy_fingerprint" in migration
    assert "child.manifest_json->>'terms_url' = policy_row.evidence_ref" not in migration
    assert "child.manifest_json->>'rights_verified_at' =" not in migration
    assert "c3_current_rights_hash IS DISTINCT FROM" in migration
    assert "access_row.current_rights_receipt_hash" in migration


def test_bundle_child_hash_recomputes_manifest_preimage_without_embedded_hash() -> None:
    migration = (
        Path(__file__).parents[2] / "migrations/versions/d7f9a1b3c5e8_wp06_scorer_current_rights.py"
    ).read_text(encoding="utf-8")

    assert "pg_catalog.replace(child.canonical_manifest" in migration
    assert ',"dataset_hash":"' in migration
    assert "child.dataset_hash || '\"'" in migration
    assert "pg_catalog.length(child.canonical_manifest) - pg_catalog.length(" in migration
    assert "child.dataset_hash" in migration


def test_admin_web_report_gate_does_not_read_test_payload_for_no_write_assertion() -> None:
    probe = (
        Path(__file__).parents[2] / "tests/e2e/probes/wp06_source_linked_scorer_test.py"
    ).read_text(encoding="utf-8")
    report_gate = probe.split("async def verify_inconclusive_denial(", 1)[1].split(
        "async def verify_deployment_audit(", 1
    )[0]
    audit_gate = probe.split("async def verify_deployment_audit(", 1)[1].split(
        "async def verify_market_signal_worker_deployment_rights(", 1
    )[0]

    assert "learning_scorer_test_payload_consumption" not in report_gate
    assert "learning_scorer_test_payload_consumption" in audit_gate
    assert "rights_integrity_counts" in audit_gate
