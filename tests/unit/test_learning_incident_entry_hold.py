"""Kiểm tra hợp đồng ledger chặn entry từ incident học máy."""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from trading_bot.auth.policy import MfaMethod
from trading_bot.domain.learning import incident_entry_hold
from trading_bot.domain.learning.drift import (
    DriftAssessmentStatus,
    DriftObservation,
    DriftPolicy,
    assess_learning_drift,
    create_learning_retrain_proposal,
)
from trading_bot.domain.learning.scoring import ScoreDecision
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentMode,
    ScorerDeploymentSnapshot,
    ScorerDeploymentTarget,
)

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
WORKSPACE_ID = UUID(int=1)
ACTOR_ID = UUID(int=2)
KEY = UUID(int=3)


def _evidence():
    observation = DriftObservation(
        observation_id=UUID(int=10),
        workspace_id=WORKSPACE_ID,
        challenger_registry_id=UUID(int=11),
        challenger_revision=2,
        observed_at=NOW - timedelta(minutes=5),
        snapshot_hash="a" * 64,
        snapshot_coverage=0.4,
        snapshot_quality_ok=True,
        baseline_available=True,
        schema_compatible=True,
        rights_eligible=True,
        challenger_decision=ScoreDecision.APPROVE,
        challenger_reason=None,
    )
    assessment = assess_learning_drift(
        workspace_id=WORKSPACE_ID,
        assessment_id=UUID(int=12),
        policy=DriftPolicy("coverage-v2-30d", 1, 0.8, 0.9),
        observations=(observation,),
        created_at=NOW - timedelta(minutes=4),
    )
    proposal = create_learning_retrain_proposal(
        assessment, proposal_id=UUID(int=13), created_at=NOW - timedelta(minutes=3)
    )
    return proposal, assessment


def _target() -> ScorerDeploymentSnapshot:
    return ScorerDeploymentSnapshot(
        workspace_id=WORKSPACE_ID,
        mode=ScorerDeploymentMode.SHADOW,
        deployment_event_id=UUID(int=14),
        deployment_revision=4,
        active_pointer_version=6,
        target=ScorerDeploymentTarget(
            registry_id=UUID(int=15),
            registry_revision=3,
            active_pointer_version=6,
            scorer_artifact_id=UUID(int=16),
            scorer_artifact_hash="b" * 64,
            evaluation_report_id=UUID(int=17),
            evaluation_report_hash="c" * 64,
            evaluation_status="ACCEPTED_SHADOW_ONLY",
            proof_id=UUID(int=18),
            proof_hash="d" * 64,
            candidate_completeness_verified=False,
            report_scope_hash="e" * 64,
            proof_scope_hash="e" * 64,
            policy_id="wp06-scorer-test-v1",
            policy_hash="f" * 64,
            current_rights_receipt_hash="0" * 64,
        ),
    )


def _hold(**overrides):
    contract = incident_entry_hold
    proposal, assessment = _evidence()
    values = {
        "event_id": UUID(int=20),
        "workspace_id": WORKSPACE_ID,
        "proposal": proposal,
        "assessment": assessment,
        "target": _target(),
        "incumbent_artifact_hash": "8" * 64,
        "revision": 1,
        "previous_event": None,
        "actor_membership_id": ACTOR_ID,
        "mfa_verified_at": NOW - timedelta(minutes=1),
        "mfa_method": MfaMethod.TOTP,
        "reason": "Xác nhận thiếu coverage",
        "idempotency_key": KEY,
        "request_id": "request-hold-1",
        "created_at": NOW,
    }
    values.update(overrides)
    return contract.create_hold_event(**values)


def _release(previous_event=None, **overrides):
    contract = incident_entry_hold
    values = {
        "event_id": UUID(int=21),
        "previous_event": previous_event if previous_event is not None else _hold(),
        "actor_membership_id": ACTOR_ID,
        "mfa_verified_at": NOW,
        "mfa_method": MfaMethod.TOTP,
        "reason": "Đã kiểm tra và xử lý nguồn dữ liệu",
        "idempotency_key": UUID(int=4),
        "request_id": "request-release-1",
        "created_at": NOW + timedelta(minutes=1),
    }
    values.update(overrides)
    return contract.create_release_event(**values)


def test_hold_pins_incumbent_instead_of_challenger_and_release_preserves_lineage() -> None:
    hold = _hold()
    release = _release(hold)
    proposal, assessment = _evidence()

    assert hold.action is incident_entry_hold.LearningIncidentHoldAction.HOLD
    assert hold.revision == 1
    assert hold.previous_event_id is None
    assert hold.previous_event_hash is None
    assert hold.proposal_id == proposal.proposal_id
    assert hold.proposal_hash == proposal.content_hash
    assert hold.assessment_id == assessment.assessment_id
    assert hold.assessment_hash == assessment.content_hash
    assert hold.incumbent_registry_id == UUID(int=15)
    assert hold.incumbent_registry_id != assessment.cohort_registry_id
    assert hold.incumbent_registry_revision == 3
    assert hold.incumbent_artifact_hash == "8" * 64
    assert hold.active_pointer_version == 6
    assert hold.deployment_revision == 4
    assert release.action is incident_entry_hold.LearningIncidentHoldAction.RELEASE
    assert release.revision == 2
    assert release.previous_event_id == hold.event_id
    assert release.previous_event_hash == hold.event_hash
    for name in (
        "workspace_id",
        "proposal_id",
        "proposal_hash",
        "assessment_id",
        "assessment_hash",
        "incumbent_registry_id",
        "incumbent_registry_revision",
        "incumbent_artifact_hash",
        "active_pointer_version",
        "deployment_revision",
    ):
        assert getattr(release, name) == getattr(hold, name)


def test_release_is_terminal_and_hold_cannot_append_to_an_existing_proposal() -> None:
    hold = _hold()
    release = _release(hold)
    for previous in (hold, release):
        with pytest.raises(ValueError, match="NONE→HOLD|RELEASE"):
            _hold(previous_event=previous, revision=previous.revision + 1)
    with pytest.raises(ValueError, match="NONE→HOLD|RELEASE"):
        _release(release)


def test_event_is_immutable_and_exported_from_learning_package() -> None:
    hold = _hold()
    package = importlib.import_module("trading_bot.domain.learning")
    assert package.LearningIncidentHoldEvent is type(hold)
    assert package.LearningIncidentHoldAction is type(hold.action)
    with pytest.raises(FrozenInstanceError):
        hold.reason = "Thay đổi trái phép"


@pytest.mark.parametrize("field", ["workspace_id", "proposal", "assessment", "target"])
def test_hold_rejects_cross_workspace_evidence(field: str) -> None:
    proposal, assessment = _evidence()
    other = uuid4()
    if field == "workspace_id":
        changes = {field: other}
    elif field == "target":
        changes = {field: replace(_target(), workspace_id=other)}
    elif field == "assessment":
        changes = {field: replace(assessment, workspace_id=other)}
    else:
        foreign_assessment = replace(assessment, workspace_id=other)
        changes = {
            field: create_learning_retrain_proposal(
                foreign_assessment, proposal_id=proposal.proposal_id, created_at=proposal.created_at
            )
        }
    with pytest.raises(PermissionError):
        _hold(**changes)


@pytest.mark.parametrize("change", ["id", "hash", "policy", "membership", "not_detected"])
def test_hold_rejects_proposal_assessment_lineage_mismatch(change: str) -> None:
    _, assessment = _evidence()
    changes = {
        "id": {"assessment_id": uuid4()},
        "hash": {"quality_rate": 0.5},
        "policy": {"policy_version": "other-policy"},
        "membership": {"created_at": NOW - timedelta(minutes=2)},
        "not_detected": {"data_quality_status": DriftAssessmentStatus.NO_DRIFT},
    }
    with pytest.raises(ValueError, match="Proposal.*assessment"):
        _hold(assessment=replace(assessment, **changes[change]))


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": ScorerDeploymentMode.DISABLED, "target": None},
        {"active_pointer_version": 7},
        {"deployment_revision": 0, "deployment_event_id": None},
    ],
)
def test_hold_requires_an_active_consistent_deployment_snapshot(changes) -> None:
    target = replace(_target(), **changes)
    with pytest.raises(ValueError, match="deployment.*active"):
        _hold(target=target)


@pytest.mark.parametrize("offset", [timedelta(seconds=1), -timedelta(minutes=15, microseconds=1)])
def test_hold_and_release_reject_future_or_expired_mfa(offset: timedelta) -> None:
    with pytest.raises(ValueError, match="MFA"):
        _hold(mfa_verified_at=NOW + offset)
    with pytest.raises(ValueError, match="MFA"):
        _release(mfa_verified_at=NOW + timedelta(minutes=1) + offset)


@pytest.mark.parametrize("field", ["created_at", "mfa_verified_at"])
@pytest.mark.parametrize("value", [None, "2026-11-12", NOW.replace(tzinfo=None)])
def test_event_rejects_invalid_or_naive_timestamps(field: str, value) -> None:
    with pytest.raises(ValueError, match="timezone"):
        _hold(**{field: value})


def test_mfa_boundary_and_utc_normalization_are_canonical() -> None:
    hold = _hold(mfa_verified_at=NOW - timedelta(minutes=15))
    shifted = _hold(
        created_at=NOW.astimezone(timezone(timedelta(hours=7))),
        mfa_verified_at=(NOW - timedelta(minutes=15)).astimezone(timezone(timedelta(hours=7))),
    )
    assert hold.event_hash == shifted.event_hash
    assert shifted.created_at.tzinfo is UTC
    assert shifted.mfa_verified_at.tzinfo is UTC


@pytest.mark.parametrize("method", [None, "SMS", "totp", 1])
def test_hold_rejects_unverified_mfa_method(method) -> None:
    with pytest.raises(ValueError, match="mfa_method"):
        _hold(mfa_method=method)


@pytest.mark.parametrize("reason", ["", " \t\n ", "x" * 501, " " + "x" * 500, "x\x00y"])
def test_event_rejects_blank_overlength_or_control_character_reason(reason: str) -> None:
    with pytest.raises(ValueError, match="reason"):
        _hold(reason=reason)


@pytest.mark.parametrize("reason", ["Ừ", "x" * 500])
def test_event_accepts_reason_length_boundaries(reason: str) -> None:
    assert _hold(reason=reason).reason == reason


def test_event_normalizes_reason_before_hashing() -> None:
    assert _hold(reason="  Đã kiểm tra  ").event_hash == _hold(reason="Đã kiểm tra").event_hash


@pytest.mark.parametrize(
    "field",
    [
        "proposal_hash",
        "assessment_hash",
        "incumbent_artifact_hash",
        "previous_event_hash",
    ],
)
@pytest.mark.parametrize("value", ["A" * 64, "0" * 63, "z" * 64, None, 3])
def test_event_rejects_malformed_hashes(field: str, value) -> None:
    event = _release() if field == "previous_event_hash" else _hold()
    with pytest.raises(
        ValueError,
        match="proposal_hash|assessment_hash|incumbent_artifact_hash|previous_event_hash",
    ):
        replace(event, **{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "event_id",
        "workspace_id",
        "proposal_id",
        "assessment_id",
        "incumbent_registry_id",
        "actor_membership_id",
        "idempotency_key",
    ],
)
@pytest.mark.parametrize("value", [UUID(int=0), "not-uuid", None])
def test_event_rejects_invalid_ids(field: str, value) -> None:
    event = _hold()
    with pytest.raises(ValueError, match="UUID"):
        replace(event, **{field: value})


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": 2},
        {"revision": True},
        {"previous_event_id": uuid4()},
        {"previous_event_id": UUID(int=20), "previous_event_hash": "a" * 64},
        {"incumbent_registry_revision": 0},
        {"incumbent_registry_revision": True},
        {"active_pointer_version": 0},
        {"deployment_revision": 0},
        {"action": "HOLD"},
        {"request_id": ""},
        {"request_id": " "},
        {"request_id": "a" * 201},
        {"request_id": "req\nspoof"},
    ],
)
def test_event_rejects_invalid_sequence_or_metadata(changes) -> None:
    event = _hold()
    with pytest.raises(ValueError, match="HOLD|revision|version|request_id|action"):
        replace(event, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": 1},
        {"revision": 3},
        {"previous_event_id": None},
        {"previous_event_id": UUID(int=0)},
        {"previous_event_id": UUID(int=21)},
        {"previous_event_hash": None},
    ],
)
def test_release_requires_exact_second_revision_and_valid_predecessor(changes) -> None:
    event = _release()
    with pytest.raises(ValueError, match="RELEASE|previous_event|predecessor"):
        replace(event, **changes)


def test_release_rejects_time_before_hold_or_reused_event_and_idempotency_ids() -> None:
    hold = _hold()
    for changes in (
        {"created_at": NOW - timedelta(seconds=1), "mfa_verified_at": NOW - timedelta(minutes=1)},
        {"event_id": hold.event_id},
        {"idempotency_key": hold.idempotency_key},
    ):
        with pytest.raises(ValueError, match="RELEASE"):
            _release(hold, **changes)


def test_canonical_event_hash_covers_audit_metadata_and_predecessor() -> None:
    event = _release()
    payload = json.loads(event.canonical_json)
    assert payload["schema_version"] == "crypidian.learning.incident-entry-hold.v1"
    assert payload["proposal_id"] == str(UUID(int=13))
    assert payload["previous_event_id"] == str(UUID(int=20))
    assert payload["previous_event_hash"] == _hold().event_hash
    assert payload["request_id"] == "request-release-1"
    assert payload["intent_hash"] == event.intent_hash
    assert "snapshot" not in payload
    assert "target" not in payload
    assert event.event_hash == hashlib.sha256(event.canonical_json.encode("utf-8")).hexdigest()
    assert replace(event, previous_event_hash="9" * 64).event_hash != event.event_hash
    assert replace(event, request_id="retry").event_hash != event.event_hash


def test_idempotency_intent_is_stable_across_server_audit_metadata_on_retry() -> None:
    event = _hold()
    retry = _hold(
        event_id=uuid4(),
        request_id="retry",
        created_at=NOW + timedelta(seconds=1),
        mfa_verified_at=NOW,
        mfa_method="TOTP",
    )
    assert retry.intent_hash == event.intent_hash
    assert retry.event_hash != event.event_hash
    payload = json.loads(event.intent_json)
    assert payload["schema_version"] == "crypidian.learning.incident-entry-hold-intent.v1"
    assert payload["expected_revision"] == 0
    assert payload["deployment_revision"] == 4
    assert payload["active_pointer_version"] == 6
    assert event.intent_hash == hashlib.sha256(event.intent_json.encode("utf-8")).hexdigest()
    for changes in (
        {"reason": "Lý do khác"},
        {"actor_membership_id": uuid4()},
        {"idempotency_key": uuid4()},
        {"proposal_hash": "9" * 64},
        {"assessment_hash": "9" * 64},
        {"incumbent_registry_id": uuid4()},
        {"incumbent_registry_revision": 4},
        {"incumbent_artifact_hash": "9" * 64},
        {"deployment_revision": 5},
        {"active_pointer_version": 7},
    ):
        assert replace(event, **changes).intent_hash != event.intent_hash


@pytest.mark.parametrize("artifact_hash", ["Z" * 64, "bad", None])
def test_hold_rejects_malformed_incumbent_artifact_hash(artifact_hash) -> None:
    with pytest.raises(ValueError, match="incumbent_artifact_hash"):
        _hold(incumbent_artifact_hash=artifact_hash)


def test_hold_distinguishes_registry_artifact_hash_from_scorer_artifact_hash() -> None:
    hold = _hold()
    assert hold.incumbent_artifact_hash == "8" * 64
    assert hold.incumbent_artifact_hash != _target().target.scorer_artifact_hash
