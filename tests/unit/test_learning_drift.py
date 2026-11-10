from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from trading_bot.domain.learning.drift import (
    DriftAssessmentStatus,
    DriftObservation,
    DriftPolicy,
    DriftReason,
    assess_learning_drift,
    create_learning_retrain_proposal,
)
from trading_bot.domain.learning.scoring import ScoreDecision, ScoreReason

WORKSPACE_ID = uuid4()
REGISTRY_ID = uuid4()
REGISTRY_REVISION = 4
NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


def _policy(**overrides: object) -> DriftPolicy:
    values: dict[str, object] = {
        "policy_version": "coverage-v1",
        "minimum_observations": 3,
        "minimum_snapshot_coverage": 0.8,
        "minimum_quality_rate": 0.9,
    }
    values.update(overrides)
    return DriftPolicy(**values)  # type: ignore[arg-type]


def _observation(
    index: int,
    *,
    coverage: float = 0.95,
    quality_ok: bool = True,
    baseline_available: bool = True,
    schema_compatible: bool = True,
    rights_eligible: bool = True,
    decision: ScoreDecision = ScoreDecision.ABSTAIN,
    registry_id: UUID = REGISTRY_ID,
    registry_revision: int = REGISTRY_REVISION,
) -> DriftObservation:
    return DriftObservation(
        observation_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        challenger_registry_id=registry_id,
        challenger_revision=registry_revision,
        observed_at=NOW + timedelta(minutes=index),
        snapshot_hash=f"{index + 1:064x}",
        snapshot_coverage=coverage,
        snapshot_quality_ok=quality_ok,
        baseline_available=baseline_available,
        schema_compatible=schema_compatible,
        rights_eligible=rights_eligible,
        challenger_decision=decision,
        challenger_reason=ScoreReason.UNCALIBRATED_SCORE,
    )


def test_drift_observation_contract_has_challenger_cohort_identity() -> None:
    observation_fields = {item.name for item in fields(DriftObservation)}

    assert {"challenger_registry_id", "challenger_revision"} <= observation_fields


def test_drift_observation_rejects_invalid_challenger_revision() -> None:
    with pytest.raises(ValueError, match="challenger_revision"):
        _observation(0, registry_revision=0)


def _assess(observations: tuple[DriftObservation, ...], policy: DriftPolicy | None = None):
    return assess_learning_drift(
        workspace_id=WORKSPACE_ID,
        assessment_id=uuid4(),
        policy=policy or _policy(),
        observations=observations,
        created_at=NOW + timedelta(hours=1),
    )


def test_assessment_uses_authoritative_metadata_and_keeps_performance_inconclusive() -> None:
    result = _assess(tuple(_observation(index) for index in range(3)))

    assert result.data_quality_status is DriftAssessmentStatus.NO_DRIFT
    assert result.performance_status is DriftAssessmentStatus.INCONCLUSIVE
    assert result.snapshot_coverage_rate == pytest.approx(0.95)
    assert result.quality_rate == pytest.approx(1.0)
    assert result.abstention_rate == pytest.approx(1.0)
    assert DriftReason.PERFORMANCE_NOT_EVALUABLE in result.reason_codes
    assert result.member_count == 3
    assert len(result.membership_digest) == 64
    assert result.cohort_registry_id == REGISTRY_ID
    assert result.cohort_revision == REGISTRY_REVISION
    assert '"cohort_registry_id":"' + str(REGISTRY_ID) + '"' in result.canonical_json
    assert '"cohort_revision":4' in result.canonical_json


def test_drift_rates_use_decimal_rounding_shared_with_postgres() -> None:
    result = _assess(
        (
            _observation(0, coverage=0.1, quality_ok=True),
            _observation(1, coverage=0.2, quality_ok=True),
            _observation(2, coverage=0.3, quality_ok=False),
        )
    )

    assert result.snapshot_coverage_rate == 0.2
    assert result.quality_rate == 0.666666666667


def test_assessment_rejects_mixed_challenger_registry_cohorts() -> None:
    observations = (
        _observation(0),
        _observation(1, registry_id=uuid4()),
        _observation(2),
    )

    with pytest.raises(ValueError, match="cùng challenger registry"):
        _assess(observations)


def test_assessment_rejects_mixed_challenger_revisions() -> None:
    observations = (
        _observation(0),
        _observation(1, registry_revision=REGISTRY_REVISION + 1),
        _observation(2),
    )

    with pytest.raises(ValueError, match="cùng challenger revision"):
        _assess(observations)


def test_empty_assessment_is_inconclusive_without_cohort_or_rates() -> None:
    result = _assess(())

    assert result.member_count == 0
    assert result.cohort_registry_id is None
    assert result.cohort_revision is None
    assert result.window_start is None
    assert result.window_end is None
    assert result.snapshot_coverage_rate is None
    assert result.quality_rate is None
    assert result.abstention_rate is None
    assert result.data_quality_status is DriftAssessmentStatus.INCONCLUSIVE
    assert DriftReason.LOW_SAMPLE in result.reason_codes


def test_assessment_content_hash_pins_cohort_identity() -> None:
    result = _assess(tuple(_observation(index) for index in range(3)))
    changed_scope = replace(result, cohort_revision=REGISTRY_REVISION + 1)

    assert changed_scope.canonical_json != result.canonical_json
    assert changed_scope.content_hash != result.content_hash


@pytest.mark.parametrize(
    ("observations", "reason"),
    [
        ((), DriftReason.LOW_SAMPLE),
        (tuple(_observation(index) for index in range(2)), DriftReason.LOW_SAMPLE),
        (
            tuple(_observation(index, baseline_available=index != 1) for index in range(3)),
            DriftReason.BASELINE_MISSING,
        ),
        (
            tuple(_observation(index, schema_compatible=index != 1) for index in range(3)),
            DriftReason.SCHEMA_INCOMPATIBLE,
        ),
        (
            tuple(_observation(index, rights_eligible=index != 1) for index in range(3)),
            DriftReason.RIGHTS_UNVERIFIED,
        ),
    ],
)
def test_missing_evidence_or_low_sample_is_inconclusive(
    observations: tuple[DriftObservation, ...], reason: DriftReason
) -> None:
    result = _assess(observations)

    assert result.data_quality_status is DriftAssessmentStatus.INCONCLUSIVE
    assert result.performance_status is DriftAssessmentStatus.INCONCLUSIVE
    assert reason in result.reason_codes
    assert result.snapshot_coverage_rate is None
    assert result.quality_rate is None


def test_only_data_quality_thresholds_can_detect_drift() -> None:
    observations = tuple(
        _observation(
            index,
            coverage=0.5 if index == 0 else 0.9,
            quality_ok=index != 1,
        )
        for index in range(3)
    )

    result = _assess(observations)

    assert result.data_quality_status is DriftAssessmentStatus.DETECTED
    assert result.performance_status is DriftAssessmentStatus.INCONCLUSIVE
    assert DriftReason.COVERAGE_BELOW_THRESHOLD in result.reason_codes
    assert DriftReason.QUALITY_BELOW_THRESHOLD in result.reason_codes
    assert not hasattr(result, "profit_factor")
    assert not hasattr(result, "pnl_drift")


def test_membership_digest_is_order_independent_but_duplicate_members_are_rejected() -> None:
    observations = tuple(_observation(index) for index in range(3))
    left = _assess(observations)
    right = _assess(tuple(reversed(observations)))

    assert left.membership_digest == right.membership_digest
    with pytest.raises(ValueError, match="trùng"):
        _assess((observations[0], observations[1], observations[1]))


def test_retrain_proposal_is_metadata_only_and_requires_detected_data_quality() -> None:
    assessment = _assess(tuple(_observation(index, coverage=0.2) for index in range(3)))
    proposal = create_learning_retrain_proposal(
        assessment,
        proposal_id=uuid4(),
        created_at=NOW + timedelta(hours=2),
    )

    assert proposal.assessment_id == assessment.assessment_id
    assert proposal.assessment_hash == assessment.content_hash
    assert proposal.policy_version == assessment.policy_version
    assert proposal.reason_codes == (DriftReason.COVERAGE_BELOW_THRESHOLD,)
    assert len(proposal.idempotency_key) == 64
    assert not hasattr(proposal, "job_id")
    assert not hasattr(proposal, "registry_pointer_id")
    assert not hasattr(proposal, "order_id")

    inconclusive = _assess(tuple(_observation(index) for index in range(2)))
    with pytest.raises(ValueError, match="DETECTED"):
        create_learning_retrain_proposal(
            inconclusive,
            proposal_id=uuid4(),
            created_at=NOW + timedelta(hours=2),
        )


def test_policy_rejects_invalid_thresholds_and_boolean_counts() -> None:
    with pytest.raises(ValueError, match="minimum_observations"):
        _policy(minimum_observations=True)
    with pytest.raises(ValueError, match="minimum_snapshot_coverage"):
        _policy(minimum_snapshot_coverage=float("nan"))
    with pytest.raises(ValueError, match="minimum_quality_rate"):
        _policy(minimum_quality_rate=1.1)


def test_observation_rejects_invalid_coverage_and_non_abstain_reason_pair() -> None:
    with pytest.raises(ValueError, match="snapshot_coverage"):
        _observation(0, coverage=1.1)
    with pytest.raises(ValueError, match="reason"):
        DriftObservation(
            observation_id=uuid4(),
            workspace_id=WORKSPACE_ID,
            challenger_registry_id=REGISTRY_ID,
            challenger_revision=REGISTRY_REVISION,
            observed_at=NOW,
            snapshot_hash="a" * 64,
            snapshot_coverage=0.9,
            snapshot_quality_ok=True,
            baseline_available=True,
            schema_compatible=True,
            rights_eligible=True,
            challenger_decision=ScoreDecision.APPROVE,
            challenger_reason=ScoreReason.UNCALIBRATED_SCORE,
        )
