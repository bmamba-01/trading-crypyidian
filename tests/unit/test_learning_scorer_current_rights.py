from __future__ import annotations

import asyncio
from datetime import UTC, datetime
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
