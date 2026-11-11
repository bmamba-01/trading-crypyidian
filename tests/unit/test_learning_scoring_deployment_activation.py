from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from tests.unit.test_learning_scorer_runtime import _policy

from trading_bot.adapters.database.models.learning_scoring_deployment_entities import (
    LearningScoringDeploymentEventRecord,
)
from trading_bot.adapters.database.repositories.learning_scoring_deployments import (
    LearningScoringDeploymentRepository,
    ScorerDeploymentConflict,
    ScorerDeploymentRightsUnavailable,
    _active_target_from_resolved,
)
from trading_bot.application.scorer_runtime_policy import ConfiguredScorerRuntimePolicyProvider
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentEvent,
    ScorerDeploymentMode,
)

WORKSPACE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
REGISTRY_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
BASE_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
SCORER_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
REPORT_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
PROOF_ID = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
SCOPE_HASH = "1" * 64
ARTIFACT_HASH = "2" * 64
REPORT_HASH = "3" * 64
PROOF_HASH = "4" * 64
POLICY_HASH = "5" * 64
RECEIPT_HASH = "6" * 64


def _activation_rows(**report_overrides: object):
    policy = _policy(policy_version="scorer-runtime-v1")
    registry = SimpleNamespace(
        workspace_id=WORKSPACE,
        id=REGISTRY_ID,
        artifact_id=BASE_ID,
        artifact_hash="7" * 64,
        state="paper_active",
        revision=5,
    )
    artifact = SimpleNamespace(
        workspace_id=WORKSPACE,
        id=SCORER_ID,
        base_artifact_id=BASE_ID,
        base_artifact_hash="7" * 64,
        artifact_hash=ARTIFACT_HASH,
        state="ready_for_evaluation",
    )
    report = SimpleNamespace(
        workspace_id=WORKSPACE,
        id=REPORT_ID,
        report_hash=REPORT_HASH,
        scorer_artifact_hash=ARTIFACT_HASH,
        base_artifact_hash="7" * 64,
        evaluation_status="ACCEPTED_SHADOW_ONLY",
        candidate_completeness_verified=False,
        scored_candidate_coverage_verified=True,
        proof_id=PROOF_ID,
        proof_hash=PROOF_HASH,
        policy_version=policy.policy_version,
        policy_hash=policy.policy_hash,
        current_rights_receipt_hash=RECEIPT_HASH,
        report={"source_scope_hash": SCOPE_HASH},
    )
    for name, value in report_overrides.items():
        setattr(report, name, value)
    proof = SimpleNamespace(
        workspace_id=WORKSPACE,
        id=PROOF_ID,
        proof_hash=PROOF_HASH,
        source_scope_hash=SCOPE_HASH,
        completeness_verified=False,
    )
    rights = SimpleNamespace(
        workspace_id=WORKSPACE,
        scorer_artifact_id=SCORER_ID,
        evaluation_report_id=REPORT_ID,
        shadow_eligible=True,
        paper_gate_eligible=False,
        artifact_hash=ARTIFACT_HASH,
        report_hash=REPORT_HASH,
        proof_hash=PROOF_HASH,
        policy_hash=policy.policy_hash,
        evaluation_status="ACCEPTED_SHADOW_ONLY",
        candidate_completeness_verified=False,
        scored_candidate_coverage_verified=True,
        source_scope_hash=SCOPE_HASH,
    )
    return policy, registry, artifact, report, proof, rights


def test_activation_uses_exact_accepted_report_and_pinned_environment_policy() -> None:
    policy, registry, artifact, report, proof, rights = _activation_rows()

    target = _active_target_from_resolved(
        workspace_id=WORKSPACE,
        mode=ScorerDeploymentMode.SHADOW,
        active_pointer_version=9,
        registry=registry,  # type: ignore[arg-type]
        artifact=artifact,  # type: ignore[arg-type]
        report=report,  # type: ignore[arg-type]
        proof=proof,  # type: ignore[arg-type]
        report_scope_hash=SCOPE_HASH,
        rights=rights,
        policy_provider=ConfiguredScorerRuntimePolicyProvider(policy),
    )

    assert target.evaluation_report_id == REPORT_ID
    assert target.registry_id == REGISTRY_ID
    assert target.active_pointer_version == 9
    assert target.policy_hash == policy.policy_hash


@pytest.mark.parametrize(
    ("mode", "report_overrides", "rights_overrides", "policy_hash"),
    [
        (
            ScorerDeploymentMode.SHADOW,
            {"evaluation_status": "INCONCLUSIVE"},
            {},
            None,
        ),
        (
            ScorerDeploymentMode.SHADOW,
            {},
            {"shadow_eligible": False},
            None,
        ),
        (
            ScorerDeploymentMode.SHADOW,
            {},
            {},
            "9" * 64,
        ),
        (
            ScorerDeploymentMode.PAPER_GATE,
            {"evaluation_status": "ACCEPTED_SHADOW_ONLY"},
            {},
            None,
        ),
    ],
)
def test_activation_rejects_unaccepted_rights_or_policy_target(
    mode: ScorerDeploymentMode,
    report_overrides: dict[str, object],
    rights_overrides: dict[str, object],
    policy_hash: str | None,
) -> None:
    policy, registry, artifact, report, proof, rights = _activation_rows(**report_overrides)
    for name, value in rights_overrides.items():
        setattr(rights, name, value)
    provider = ConfiguredScorerRuntimePolicyProvider(policy)
    if policy_hash is not None:
        provider = ConfiguredScorerRuntimePolicyProvider(
            _policy(policy_version=policy.policy_version, minimum_sample_count=4)
        )

    with pytest.raises(ScorerDeploymentRightsUnavailable):
        _active_target_from_resolved(
            workspace_id=WORKSPACE,
            mode=mode,
            active_pointer_version=9,
            registry=registry,  # type: ignore[arg-type]
            artifact=artifact,  # type: ignore[arg-type]
            report=report,  # type: ignore[arg-type]
            proof=proof,  # type: ignore[arg-type]
            report_scope_hash=SCOPE_HASH,
            rights=rights,
            policy_provider=provider,
        )


async def test_active_deployment_activation_rejects_stale_revision_before_target_queries() -> None:
    class Session:
        def __init__(self) -> None:
            self.scalar_values = iter(
                (
                    "read committed",
                    None,
                    SimpleNamespace(revision=4),
                )
            )
            self.executed: list[object] = []

        async def scalar(self, _statement):
            return next(self.scalar_values)

        async def execute(self, statement, _params=None):
            self.executed.append(statement)

    session = Session()
    repository = LearningScoringDeploymentRepository(session)  # type: ignore[arg-type]

    with pytest.raises(ScorerDeploymentConflict, match="CAS đã cũ"):
        await repository.append_active_event(
            workspace_id=WORKSPACE,
            actor_membership_id=UUID("77777777-7777-4777-8777-777777777777"),
            mfa_verified_at=datetime.now(UTC),
            mode=ScorerDeploymentMode.SHADOW,
            evaluation_report_id=REPORT_ID,
            expected_pointer_version=9,
            expected_deployment_revision=5,
            idempotency_key=UUID("88888888-8888-4888-8888-888888888888"),
            reason="Bật SHADOW sau khi kiểm report và policy.",
            policy_provider=ConfiguredScorerRuntimePolicyProvider(_policy()),
        )

    assert len(session.executed) == 4


async def test_active_idempotency_replays_stored_event_before_current_cas_and_rights() -> None:
    policy, registry, artifact, report, proof, rights = _activation_rows()
    actor_id = UUID("77777777-7777-4777-8777-777777777777")
    idempotency_key = UUID("88888888-8888-4888-8888-888888888888")
    reason = "Bật SHADOW sau khi kiểm report và policy."
    now = datetime.now(UTC)
    target = _active_target_from_resolved(
        workspace_id=WORKSPACE,
        mode=ScorerDeploymentMode.SHADOW,
        active_pointer_version=9,
        registry=registry,  # type: ignore[arg-type]
        artifact=artifact,  # type: ignore[arg-type]
        report=report,  # type: ignore[arg-type]
        proof=proof,  # type: ignore[arg-type]
        report_scope_hash=SCOPE_HASH,
        rights=rights,
        policy_provider=ConfiguredScorerRuntimePolicyProvider(policy),
    )
    stored_event = ScorerDeploymentEvent(
        event_id=UUID("99999999-9999-4999-8999-999999999999"),
        workspace_id=WORKSPACE,
        revision=6,
        mode=ScorerDeploymentMode.SHADOW,
        target=target,
        actor_membership_id=actor_id,
        mfa_verified_at=now - datetime.resolution * 60,
        mfa_method="TOTP",
        expected_pointer_version=9,
        expected_deployment_revision=5,
        reason=reason,
        idempotency_key=idempotency_key,
        previous_event_id=None,
        previous_event_hash=None,
        created_at=now,
    )
    persisted = LearningScoringDeploymentEventRecord.from_domain(stored_event)

    class Session:
        def __init__(self) -> None:
            self.scalar_values = iter(("read committed", persisted))
            self.executed: list[object] = []

        async def scalar(self, _statement):
            return next(self.scalar_values)

        async def execute(self, statement, _params=None):
            self.executed.append(statement)

    session = Session()
    repository = LearningScoringDeploymentRepository(session)  # type: ignore[arg-type]

    replay = await repository.append_active_event(
        workspace_id=WORKSPACE,
        actor_membership_id=actor_id,
        mfa_verified_at=now,
        mode=ScorerDeploymentMode.SHADOW,
        evaluation_report_id=REPORT_ID,
        expected_pointer_version=9,
        expected_deployment_revision=5,
        idempotency_key=idempotency_key,
        reason=reason,
        policy_provider=ConfiguredScorerRuntimePolicyProvider(policy),
    )

    assert replay.event_id == stored_event.event_id
    assert replay.event_hash == stored_event.event_hash
    assert replay.intent_hash == stored_event.intent_hash
    assert len(session.executed) == 2  # workspace context + idempotency advisory lock
