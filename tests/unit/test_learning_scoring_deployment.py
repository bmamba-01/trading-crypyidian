from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentEvent,
    ScorerDeploymentMode,
    ScorerDeploymentTarget,
)


def _target(**overrides: object) -> ScorerDeploymentTarget:
    values: dict[str, object] = {
        "registry_id": uuid4(),
        "registry_revision": 3,
        "active_pointer_version": 7,
        "scorer_artifact_id": uuid4(),
        "scorer_artifact_hash": "a" * 64,
        "evaluation_report_id": uuid4(),
        "evaluation_report_hash": "b" * 64,
        "evaluation_status": "ACCEPTED_SHADOW_ONLY",
        "proof_id": uuid4(),
        "proof_hash": "c" * 64,
        "candidate_completeness_verified": False,
        "report_scope_hash": "d" * 64,
        "proof_scope_hash": "d" * 64,
        "policy_id": "wp06-scorer-policy-v1",
        "policy_hash": "e" * 64,
        "current_rights_receipt_hash": "f" * 64,
    }
    values.update(overrides)
    return ScorerDeploymentTarget(**values)  # type: ignore[arg-type]


def test_shadow_accepts_only_scorer_specific_accepted_reports() -> None:
    _target().require_mode(ScorerDeploymentMode.SHADOW)
    _target(evaluation_status="ACCEPTED_PAPER_GATE_ELIGIBLE").require_mode(
        ScorerDeploymentMode.SHADOW
    )

    with pytest.raises(ValueError, match="report scorer"):
        _target(evaluation_status="INCONCLUSIVE").require_mode(ScorerDeploymentMode.SHADOW)


def test_paper_gate_requires_complete_matching_test_proof() -> None:
    eligible = _target(
        evaluation_status="ACCEPTED_PAPER_GATE_ELIGIBLE",
        candidate_completeness_verified=True,
    )
    eligible.require_mode(ScorerDeploymentMode.PAPER_GATE)

    with pytest.raises(ValueError, match="completeness"):
        _target(
            evaluation_status="ACCEPTED_PAPER_GATE_ELIGIBLE",
            candidate_completeness_verified=False,
        ).require_mode(ScorerDeploymentMode.PAPER_GATE)

    with pytest.raises(ValueError, match="proof"):
        _target(
            evaluation_status="ACCEPTED_PAPER_GATE_ELIGIBLE",
            candidate_completeness_verified=True,
            proof_id=None,
            proof_hash=None,
        ).require_mode(ScorerDeploymentMode.PAPER_GATE)

    with pytest.raises(ValueError, match="scope"):
        _target(
            evaluation_status="ACCEPTED_PAPER_GATE_ELIGIBLE",
            candidate_completeness_verified=True,
            proof_scope_hash="0" * 64,
        ).require_mode(ScorerDeploymentMode.PAPER_GATE)


def test_scoreable_deployment_requires_current_rights_fingerprint() -> None:
    with pytest.raises(ValueError, match="rights"):
        _target(current_rights_receipt_hash=None).require_mode(ScorerDeploymentMode.SHADOW)


def test_deployment_intent_hash_ignores_generated_event_and_mfa_timestamps() -> None:
    now = datetime.now(UTC)
    workspace_id = uuid4()
    actor_id = uuid4()
    idempotency_key = uuid4()

    def event(*, event_id, created_at, mfa_verified_at, reason="Tắt scorer sau rà soát."):
        return ScorerDeploymentEvent(
            event_id=event_id,
            workspace_id=workspace_id,
            revision=1,
            mode=ScorerDeploymentMode.DISABLED,
            target=None,
            actor_membership_id=actor_id,
            mfa_verified_at=mfa_verified_at,
            mfa_method="TOTP",
            expected_pointer_version=0,
            expected_deployment_revision=0,
            reason=reason,
            idempotency_key=idempotency_key,
            previous_event_id=None,
            previous_event_hash=None,
            created_at=created_at,
        )

    first = event(
        event_id=uuid4(),
        created_at=now,
        mfa_verified_at=now - timedelta(minutes=1),
    )
    retried = event(
        event_id=uuid4(),
        created_at=now + timedelta(seconds=2),
        mfa_verified_at=now + timedelta(seconds=1),
    )
    changed = event(
        event_id=uuid4(),
        created_at=now + timedelta(seconds=2),
        mfa_verified_at=now + timedelta(seconds=1),
        reason="Tắt scorer sau phát hiện quyền nguồn bị thu hồi.",
    )

    assert first.event_hash != retried.event_hash
    assert first.intent_hash == retried.intent_hash
    assert first.intent_hash != changed.intent_hash

    with pytest.raises(ValueError, match="ít nhất 12"):
        event(
            event_id=uuid4(),
            created_at=now,
            mfa_verified_at=now - timedelta(minutes=1),
            reason="short",
        )
