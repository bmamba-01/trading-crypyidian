from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from tests.unit.test_learning_scorer_runtime import _base_artifact, _scorer_artifact, _vector

from trading_bot.adapters.database.repositories.learning_scorer_runtime_targets import (
    PostgresScorerRuntimeTargetRepository,
)
from trading_bot.domain.learning.scoring_deployment import ScorerDeploymentMode

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


class _Mappings:
    def __init__(self, row: object) -> None:
        self._row = row

    def mappings(self) -> _Mappings:
        return self

    def one_or_none(self) -> object:
        return self._row


async def test_runtime_target_repository_reads_active_target_only_through_gateway() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[SimpleNamespace(), _Mappings(None)])

    resolved = await PostgresScorerRuntimeTargetRepository(session).resolve_active(
        workspace_id=WORKSPACE_ID
    )

    assert resolved is None
    assert session.execute.await_count == 2
    resolver_sql = str(session.execute.await_args_list[1].args[0])
    assert "wp06_resolve_scorer_runtime_target" in resolver_sql
    assert "NULL::jsonb" in resolver_sql
    assert "learning_scoring_deployment_event" not in resolver_sql
    assert "learning_scoring_deployment_pointer" not in resolver_sql


async def test_runtime_target_repository_parses_authoritative_gateway_artifacts() -> None:
    vector = _vector()
    base = _base_artifact(vector)
    scorer = _scorer_artifact(base, vector)
    values = {
        "workspace_id": WORKSPACE_ID,
        "mode": ScorerDeploymentMode.SHADOW.value,
        "deployment_event_id": uuid4(),
        "deployment_revision": 2,
        "registry_id": uuid4(),
        "registry_revision": 1,
        "active_pointer_version": 3,
        "scorer_artifact_id": uuid4(),
        "scorer_artifact_hash": scorer.artifact_hash,
        "base_artifact_id": uuid4(),
        "base_artifact_hash": base.artifact_hash,
        "base_artifact_canonical": base.canonical_json,
        "scorer_artifact_canonical": scorer.canonical_json,
        "evaluation_report_id": uuid4(),
        "evaluation_report_hash": "a" * 64,
        "evaluation_status": "ACCEPTED_SHADOW_ONLY",
        "candidate_completeness_verified": False,
        "scored_candidate_coverage_verified": True,
        "report_scope_hash": "b" * 64,
        "proof_id": uuid4(),
        "proof_hash": "c" * 64,
        "proof_scope_hash": "b" * 64,
        "policy_id": "test-policy-v1",
        "policy_hash": "d" * 64,
        "current_rights_receipt_hash": "e" * 64,
        "source_rights_fingerprint": "f" * 64,
    }
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[SimpleNamespace(), _Mappings(values)])

    resolved = await PostgresScorerRuntimeTargetRepository(session).resolve_active(
        workspace_id=WORKSPACE_ID
    )

    assert resolved is not None
    assert resolved.mode is ScorerDeploymentMode.SHADOW
    assert resolved.deployment_event_id == values["deployment_event_id"]
    assert resolved.deployment_revision == 2
    assert resolved.target.evaluation_report_id == values["evaluation_report_id"]
    assert resolved.base_artifact == base
    assert resolved.scorer_artifact == scorer
