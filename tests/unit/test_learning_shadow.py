from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from trading_bot.domain.learning.model_registry import LearningModelRegistryState
from trading_bot.domain.learning.shadow import (
    IncumbentDecisionReference,
    IncumbentDecisionStatus,
    ShadowAbstainReason,
    ShadowModelReference,
    ShadowObservation,
    ShadowSnapshotReference,
)

WORKSPACE_ID = uuid4()
NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)
HASHES = {
    "snapshot": "a" * 64,
    "schema": "b" * 64,
    "incumbent_artifact": "c" * 64,
    "incumbent_manifest": "d" * 64,
    "incumbent_fold": "e" * 64,
    "incumbent_approval": "f" * 64,
    "challenger_artifact": "1" * 64,
    "challenger_manifest": "2" * 64,
    "challenger_fold": "3" * 64,
    "challenger_training_approval": "4" * 64,
    "challenger_registry_approval": "7" * 64,
    "active_artifact": "8" * 64,
    "decision": "5" * 64,
    "strategy": "6" * 64,
}


def _snapshot(**changes: object) -> ShadowSnapshotReference:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "snapshot_id": "snapshot-123",
        "snapshot_hash": HASHES["snapshot"],
        "schema_hash": HASHES["schema"],
        "as_of": NOW,
    }
    values.update(changes)
    return ShadowSnapshotReference(**values)  # type: ignore[arg-type]


def _model(
    *,
    registry_id=None,
    state: LearningModelRegistryState,
    prefix: str,
) -> ShadowModelReference:
    return ShadowModelReference(
        workspace_id=WORKSPACE_ID,
        registry_id=registry_id or uuid4(),
        artifact_id=uuid4(),
        artifact_hash=HASHES[f"{prefix}_artifact"],
        manifest_hash=HASHES[f"{prefix}_manifest"],
        fold_plan_hash=HASHES[f"{prefix}_fold"],
        training_approval_hash=HASHES[f"{prefix}_training_approval"],
        registry_approval_event_id=uuid4(),
        registry_approval_event_hash=HASHES["challenger_registry_approval"],
        registry_approval_event_sequence=4,
        revision=3,
        state=state,
    )


def _observation(*, evaluated_at: datetime = NOW, **changes: object) -> ShadowObservation:
    snapshot = _snapshot()
    challenger = _model(state=LearningModelRegistryState.EVALUATED, prefix="challenger")
    decision = IncumbentDecisionReference(
        decision_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        decision_hash=HASHES["decision"],
        strategy_release_id="h03-v1",
        strategy_release_hash=HASHES["strategy"],
        status=IncumbentDecisionStatus.CANDIDATE,
        snapshot=snapshot,
        evaluated_at=evaluated_at,
    )
    values: dict[str, object] = {
        "observation_id": uuid4(),
        "workspace_id": WORKSPACE_ID,
        "snapshot": snapshot,
        "challenger_model": challenger,
        "incumbent_decision": decision,
        "active_registry_id": uuid4(),
        "active_registry_revision": 7,
        "active_artifact_hash": HASHES["active_artifact"],
        "active_pointer_version": 8,
        "snapshot_coverage": 0.95,
        "snapshot_quality_ok": True,
        "schema_compatible": True,
        "rights_eligible": True,
        "rights_fingerprint": "9" * 64,
        "rights_source_id": "BINANCE_USDM_REST",
        "rights_policy_id": "BINANCE_USDM_REST:policy:2",
        "rights_policy_sequence": 2,
        "rights_known_at": NOW + timedelta(seconds=1),
        "challenger_reason": ShadowAbstainReason.UNCALIBRATED_SCORE,
        "observed_at": NOW + timedelta(seconds=2),
    }
    values.update(changes)
    return ShadowObservation(**values)  # type: ignore[arg-type]


def test_observation_pins_approved_models_same_snapshot_and_active_pointer() -> None:
    observation = _observation()

    assert observation.incumbent_decision.snapshot == observation.snapshot
    assert observation.incumbent_decision.strategy_release_id == "h03-v1"
    assert observation.incumbent_decision.strategy_release_hash == HASHES["strategy"]
    assert observation.challenger_model.state is LearningModelRegistryState.EVALUATED
    assert (
        observation.challenger_model.training_approval_hash
        == HASHES["challenger_training_approval"]
    )
    assert observation.challenger_model.registry_approval_event_sequence == 4
    assert observation.active_registry_id != observation.challenger_model.registry_id
    assert observation.active_registry_revision == 7
    assert observation.active_pointer_version == 8
    assert observation.rights_eligible is True
    assert observation.rights_fingerprint == "9" * 64
    assert observation.rights_source_id == "BINANCE_USDM_REST"
    assert observation.rights_policy_id == "BINANCE_USDM_REST:policy:2"
    assert observation.rights_policy_sequence == 2
    assert observation.rights_known_at == NOW + timedelta(seconds=1)
    assert observation.challenger_reason is ShadowAbstainReason.UNCALIBRATED_SCORE
    assert observation.content_hash == replace(observation).content_hash
    assert len(observation.content_hash) == 64
    assert len(observation.idempotency_key) == 64
    assert not hasattr(observation.incumbent_decision, "registry_id")
    assert "incumbent_model" not in observation.canonical_payload()
    assert (
        replace(observation, active_pointer_version=9).idempotency_key
        != observation.idempotency_key
    )
    assert (
        replace(observation, active_registry_id=uuid4()).idempotency_key
        != observation.idempotency_key
    )
    assert (
        replace(
            observation,
            challenger_model=replace(
                observation.challenger_model, registry_approval_event_sequence=5
            ),
        ).idempotency_key
        != observation.idempotency_key
    )
    assert (
        replace(observation, rights_fingerprint="a" * 64).idempotency_key
        != observation.idempotency_key
    )


def test_decision_after_snapshot_is_valid_when_observation_follows_decision() -> None:
    observation = _observation(evaluated_at=NOW + timedelta(seconds=1))

    assert observation.snapshot.as_of == NOW
    assert observation.incumbent_decision.evaluated_at == NOW + timedelta(seconds=1)
    assert observation.observed_at >= observation.incumbent_decision.evaluated_at


def test_observation_is_immutable_and_contains_no_scores_or_execution_capability() -> None:
    observation = _observation()

    with pytest.raises(FrozenInstanceError):
        observation.active_pointer_version = 9  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        observation.order_id = "venue-order"  # type: ignore[attr-defined]
    assert not hasattr(observation, "probability")
    assert not hasattr(observation, "confidence")
    assert not hasattr(observation, "uncertainty")
    assert not hasattr(observation, "features")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"workspace_id": uuid4()}, "cùng workspace"),
        ({"active_pointer_version": 0}, "active_pointer_version"),
        ({"active_registry_revision": 0}, "active_registry_revision"),
        ({"observed_at": NOW - timedelta(seconds=1)}, "trước snapshot"),
        ({"challenger_reason": "uncalibrated"}, "lý do ABSTAIN"),
        ({"snapshot_coverage": 1.1}, "snapshot_coverage"),
        ({"rights_eligible": True, "rights_fingerprint": None}, "rights_fingerprint"),
        ({"rights_policy_id": None}, "rights_policy_id"),
        ({"rights_policy_sequence": 0}, "rights_policy_sequence"),
        ({"rights_known_at": NOW + timedelta(seconds=3)}, "rights_known_at"),
        ({"active_artifact_hash": "bad"}, "active_artifact_hash"),
    ],
)
def test_observation_rejects_invalid_scope_version_time_or_abstain_reason(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _observation(**changes)


def test_observation_rejects_snapshot_or_workspace_mismatch() -> None:
    changed_snapshot = _snapshot(snapshot_hash="7" * 64)

    with pytest.raises(ValueError, match="snapshot"):
        _observation(snapshot=changed_snapshot)
    with pytest.raises(ValueError, match="cùng workspace"):
        _observation(
            incumbent_decision=IncumbentDecisionReference(
                decision_id=uuid4(),
                workspace_id=uuid4(),
                decision_hash=HASHES["decision"],
                strategy_release_id="h03-v1",
                strategy_release_hash=HASHES["strategy"],
                status=IncumbentDecisionStatus.CANDIDATE,
                snapshot=_snapshot(workspace_id=uuid4()),
                evaluated_at=NOW,
            )
        )


def test_observation_requires_distinct_active_model_and_registry_approval_event() -> None:
    observation = _observation()
    with pytest.raises(ValueError, match="không được trùng model đang được active"):
        replace(observation, active_registry_id=observation.challenger_model.registry_id)
    with pytest.raises(ValueError, match="registry_approval_event_id"):
        replace(
            observation.challenger_model,
            registry_approval_event_id=None,  # type: ignore[arg-type]
        )


def test_schema_and_source_rights_fail_closed_with_explicit_abstain_reasons() -> None:
    with pytest.raises(ValueError, match="FEATURE_SCHEMA_MISMATCH"):
        _observation(schema_compatible=False)
    schema_mismatch = _observation(
        schema_compatible=False,
        challenger_reason=ShadowAbstainReason.FEATURE_SCHEMA_MISMATCH,
    )
    assert schema_mismatch.schema_compatible is False

    with pytest.raises(ValueError, match="SOURCE_RIGHTS_INELIGIBLE"):
        _observation(rights_eligible=False, rights_fingerprint=None)
    rights_blocked = _observation(
        rights_eligible=False,
        rights_fingerprint=None,
        rights_source_id=None,
        rights_policy_id=None,
        rights_policy_sequence=None,
        rights_known_at=None,
        challenger_reason=ShadowAbstainReason.SOURCE_RIGHTS_INELIGIBLE,
    )
    assert rights_blocked.rights_eligible is False

    rights_priority = _observation(
        schema_compatible=False,
        rights_eligible=False,
        rights_fingerprint=None,
        rights_source_id=None,
        rights_policy_id=None,
        rights_policy_sequence=None,
        rights_known_at=None,
        challenger_reason=ShadowAbstainReason.SOURCE_RIGHTS_INELIGIBLE,
    )
    assert rights_priority.schema_compatible is False
    assert rights_priority.rights_eligible is False
    assert rights_priority.challenger_reason is ShadowAbstainReason.SOURCE_RIGHTS_INELIGIBLE


def test_unverified_source_rights_cannot_retain_a_fingerprint() -> None:
    with pytest.raises(ValueError, match="rights_fingerprint"):
        _observation(
            rights_eligible=False,
            rights_fingerprint="9" * 64,
            rights_source_id=None,
            rights_policy_id=None,
            rights_policy_sequence=None,
            rights_known_at=None,
            challenger_reason=ShadowAbstainReason.SOURCE_RIGHTS_INELIGIBLE,
        )


def test_decision_before_snapshot_is_rejected() -> None:
    with pytest.raises(ValueError, match="không được có trước as_of snapshot"):
        _observation(evaluated_at=NOW - timedelta(seconds=1))


def test_observation_before_decision_is_rejected() -> None:
    with pytest.raises(ValueError, match="trước quyết định incumbent"):
        _observation(
            evaluated_at=NOW + timedelta(seconds=3),
            observed_at=NOW + timedelta(seconds=2),
        )


@pytest.mark.parametrize(
    "state",
    [
        LearningModelRegistryState.DRAFT,
        LearningModelRegistryState.PAPER_ACTIVE,
        LearningModelRegistryState.REJECTED,
        LearningModelRegistryState.RETIRED,
    ],
)
def test_observation_requires_approved_incumbent_and_challenger(
    state: LearningModelRegistryState,
) -> None:
    with pytest.raises(ValueError, match="được duyệt"):
        _observation(challenger_model=_model(state=state, prefix="challenger"))


def test_snapshot_and_model_hashes_must_be_sha256() -> None:
    with pytest.raises(ValueError, match="snapshot_hash"):
        _snapshot(snapshot_hash="bad")
    with pytest.raises(ValueError, match="artifact_hash"):
        ShadowModelReference(
            workspace_id=WORKSPACE_ID,
            registry_id=uuid4(),
            artifact_id=uuid4(),
            artifact_hash="bad",
            manifest_hash=HASHES["challenger_manifest"],
            fold_plan_hash=HASHES["challenger_fold"],
            training_approval_hash=HASHES["challenger_training_approval"],
            registry_approval_event_id=uuid4(),
            registry_approval_event_hash=HASHES["challenger_registry_approval"],
            registry_approval_event_sequence=1,
            revision=1,
            state=LearningModelRegistryState.EVALUATED,
        )
