"""Ledger bất biến xác nhận và kết thúc chặn entry do incident học máy."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from trading_bot.auth.policy import RECENT_MFA_LIFETIME, MfaMethod
from trading_bot.domain.learning.drift import (
    DriftAssessmentStatus,
    DriftReason,
    LearningDriftAssessment,
    LearningRetrainProposal,
)
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentMode,
    ScorerDeploymentSnapshot,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DATA_REASONS = frozenset(
    {DriftReason.COVERAGE_BELOW_THRESHOLD, DriftReason.QUALITY_BELOW_THRESHOLD}
)


class LearningIncidentHoldAction(StrEnum):
    """Hai hành động được phép; RELEASE là trạng thái cuối của proposal."""

    HOLD = "HOLD"
    RELEASE = "RELEASE"


@dataclass(frozen=True, slots=True)
class LearningIncidentHoldEvent:
    """Metadata audit; việc xác minh membership/target hiện hành thuộc repository."""

    event_id: UUID
    workspace_id: UUID
    proposal_id: UUID
    proposal_hash: str
    assessment_id: UUID
    assessment_hash: str
    action: LearningIncidentHoldAction
    revision: int
    previous_event_id: UUID | None
    previous_event_hash: str | None
    incumbent_registry_id: UUID
    incumbent_registry_revision: int
    incumbent_artifact_hash: str
    active_pointer_version: int
    deployment_revision: int
    actor_membership_id: UUID
    mfa_verified_at: datetime
    mfa_method: MfaMethod | str
    reason: str
    idempotency_key: UUID
    request_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "event_id",
            "workspace_id",
            "proposal_id",
            "assessment_id",
            "incumbent_registry_id",
            "actor_membership_id",
            "idempotency_key",
        ):
            _uuid(getattr(self, name), name)
        for name in ("proposal_hash", "assessment_hash", "incumbent_artifact_hash"):
            _hash(getattr(self, name), name)
        if type(self.action) is not LearningIncidentHoldAction:
            raise ValueError("action phải là LearningIncidentHoldAction.")
        for name in (
            "revision",
            "incumbent_registry_revision",
            "active_pointer_version",
            "deployment_revision",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} phải là số nguyên dương.")
        if self.action is LearningIncidentHoldAction.HOLD:
            if (
                self.revision != 1
                or self.previous_event_id is not None
                or self.previous_event_hash is not None
            ):
                raise ValueError("HOLD chỉ được tạo ở revision 1 và không có predecessor.")
        else:
            if self.revision != 2:
                raise ValueError("RELEASE chỉ được tạo ở revision 2 và là trạng thái cuối.")
            _uuid(self.previous_event_id, "previous_event_id")
            _hash(self.previous_event_hash, "previous_event_hash")
            if self.previous_event_id == self.event_id:
                raise ValueError("Event không được tham chiếu chính nó làm predecessor.")
        if (
            not isinstance(self.reason, str)
            or not 1 <= len(self.reason.strip()) <= 500
            or len(self.reason) > 500
            or any(ord(char) < 32 and char not in "\t\n\r" for char in self.reason)
        ):
            raise ValueError("reason phải có 1–500 ký tự và không chứa ký tự điều khiển ẩn.")
        object.__setattr__(self, "reason", self.reason.strip())
        if (
            not isinstance(self.request_id, str)
            or not self.request_id.strip()
            or len(self.request_id) > 200
            or any(ord(char) < 32 for char in self.request_id)
        ):
            raise ValueError("request_id phải có 1–200 ký tự và không có ký tự điều khiển.")
        if not isinstance(self.mfa_method, (MfaMethod, str)) or self.mfa_method != "TOTP":
            raise ValueError("mfa_method phải là TOTP đã được server xác minh.")
        object.__setattr__(self, "mfa_method", MfaMethod.TOTP)
        created_at = _utc(self.created_at, "created_at")
        mfa_verified_at = _utc(self.mfa_verified_at, "mfa_verified_at")
        if mfa_verified_at > created_at or created_at - mfa_verified_at > RECENT_MFA_LIFETIME:
            raise ValueError("MFA phải còn mới trong 15 phút và không được ở tương lai.")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "mfa_verified_at", mfa_verified_at)

    @property
    def intent_payload(self) -> dict[str, object]:
        """Ý định ổn định khi retry, không gồm UUID/thời điểm audit do server sinh."""
        return {
            "action": self.action.value,
            "actor_membership_id": str(self.actor_membership_id),
            "active_pointer_version": self.active_pointer_version,
            "assessment_hash": self.assessment_hash,
            "assessment_id": str(self.assessment_id),
            "deployment_revision": self.deployment_revision,
            "expected_revision": self.revision - 1,
            "idempotency_key": str(self.idempotency_key),
            "incumbent_artifact_hash": self.incumbent_artifact_hash,
            "incumbent_registry_id": str(self.incumbent_registry_id),
            "incumbent_registry_revision": self.incumbent_registry_revision,
            "proposal_hash": self.proposal_hash,
            "proposal_id": str(self.proposal_id),
            "reason": self.reason,
            "schema_version": "crypidian.learning.incident-entry-hold-intent.v1",
            "workspace_id": str(self.workspace_id),
        }

    @property
    def intent_json(self) -> str:
        """JSON chuẩn dùng đối chiếu idempotency trước các kiểm tra CAS hiện hành."""
        return _canonical(self.intent_payload)

    @property
    def intent_hash(self) -> str:
        """SHA-256 của ý định đã chuẩn hóa, không chứa dữ liệu sample."""
        return _digest(self.intent_json)

    @property
    def canonical_payload(self) -> dict[str, object]:
        """Payload đầy đủ cho hash audit, bao gồm lineage predecessor và MFA."""
        return {
            **self.intent_payload,
            "created_at": self.created_at.isoformat(),
            "event_id": str(self.event_id),
            "intent_hash": self.intent_hash,
            "mfa_method": str(self.mfa_method),
            "mfa_verified_at": self.mfa_verified_at.isoformat(),
            "previous_event_hash": self.previous_event_hash,
            "previous_event_id": str(self.previous_event_id) if self.previous_event_id else None,
            "request_id": self.request_id,
            "revision": self.revision,
            "schema_version": "crypidian.learning.incident-entry-hold.v1",
        }

    @property
    def canonical_json(self) -> str:
        """JSON chuẩn chỉ gồm metadata được phép lưu trong ledger."""
        return _canonical(self.canonical_payload)

    @property
    def event_hash(self) -> str:
        """SHA-256 của event đầy đủ để nối chuỗi bất biến."""
        return _digest(self.canonical_json)


def create_hold_event(
    *,
    event_id: UUID,
    workspace_id: UUID,
    proposal: LearningRetrainProposal,
    assessment: LearningDriftAssessment,
    target: ScorerDeploymentSnapshot,
    incumbent_artifact_hash: str,
    revision: int,
    previous_event: LearningIncidentHoldEvent | None,
    actor_membership_id: UUID,
    mfa_verified_at: datetime,
    mfa_method: MfaMethod | str,
    reason: str,
    idempotency_key: UUID,
    request_id: str,
    created_at: datetime,
) -> LearningIncidentHoldEvent:
    """Tạo NONE→HOLD; repository chịu trách nhiệm latest assessment và target dưới khóa."""
    if (
        type(proposal) is not LearningRetrainProposal
        or type(assessment) is not LearningDriftAssessment
    ):
        raise ValueError("HOLD cần proposal và assessment authoritative.")
    if type(target) is not ScorerDeploymentSnapshot:
        raise ValueError("target phải là ScorerDeploymentSnapshot.")
    if any(item.workspace_id != workspace_id for item in (proposal, assessment, target)):
        raise PermissionError("Proposal, assessment và target phải cùng workspace.")
    if previous_event is not None or type(revision) is not int or revision != 1:
        raise ValueError("Chỉ NONE→HOLD được phép; không HOLD lại proposal đã có event.")
    if (
        proposal.assessment_id != assessment.assessment_id
        or proposal.assessment_hash != assessment.content_hash
        or proposal.policy_version != assessment.policy_version
        or proposal.membership_digest != assessment.membership_digest
        or assessment.data_quality_status is not DriftAssessmentStatus.DETECTED
        or proposal.reason_codes
        != tuple(reason for reason in assessment.reason_codes if reason in _DATA_REASONS)
    ):
        raise ValueError("Proposal phải ghim đúng assessment DETECTED về quality/coverage.")
    if (
        target.mode is ScorerDeploymentMode.DISABLED
        or target.target is None
        or target.deployment_revision < 1
        or target.active_pointer_version != target.target.active_pointer_version
    ):
        raise ValueError("HOLD cần incumbent của deployment đang active với pointer nhất quán.")
    if _utc(created_at, "created_at") < max(proposal.created_at, assessment.created_at):
        raise ValueError("HOLD không được xảy ra trước proposal hoặc assessment.")
    # Hash artifact registry khác hash artifact scorer; repository xác minh riêng từ registry.
    return LearningIncidentHoldEvent(
        event_id=event_id,
        workspace_id=workspace_id,
        proposal_id=proposal.proposal_id,
        proposal_hash=proposal.content_hash,
        assessment_id=assessment.assessment_id,
        assessment_hash=assessment.content_hash,
        action=LearningIncidentHoldAction.HOLD,
        revision=revision,
        previous_event_id=None,
        previous_event_hash=None,
        incumbent_registry_id=target.target.registry_id,
        incumbent_registry_revision=target.target.registry_revision,
        incumbent_artifact_hash=incumbent_artifact_hash,
        active_pointer_version=target.active_pointer_version,
        deployment_revision=target.deployment_revision,
        actor_membership_id=actor_membership_id,
        mfa_verified_at=mfa_verified_at,
        mfa_method=mfa_method,
        reason=reason,
        idempotency_key=idempotency_key,
        request_id=request_id,
        created_at=created_at,
    )


def create_release_event(
    *,
    event_id: UUID,
    previous_event: LearningIncidentHoldEvent,
    actor_membership_id: UUID,
    mfa_verified_at: datetime,
    mfa_method: MfaMethod | str,
    reason: str,
    idempotency_key: UUID,
    request_id: str,
    created_at: datetime,
) -> LearningIncidentHoldEvent:
    """Tạo HOLD→RELEASE cuối cùng, không phụ thuộc target hiện hành còn active."""
    if (
        type(previous_event) is not LearningIncidentHoldEvent
        or previous_event.action is not LearningIncidentHoldAction.HOLD
        or previous_event.revision != 1
    ):
        raise ValueError("RELEASE chỉ được nối trực tiếp sau HOLD đầu tiên.")
    if _utc(created_at, "created_at") < previous_event.created_at:
        raise ValueError("RELEASE không được xảy ra trước HOLD.")
    if event_id == previous_event.event_id or idempotency_key == previous_event.idempotency_key:
        raise ValueError("RELEASE cần event_id và idempotency_key riêng.")
    return LearningIncidentHoldEvent(
        event_id=event_id,
        workspace_id=previous_event.workspace_id,
        proposal_id=previous_event.proposal_id,
        proposal_hash=previous_event.proposal_hash,
        assessment_id=previous_event.assessment_id,
        assessment_hash=previous_event.assessment_hash,
        action=LearningIncidentHoldAction.RELEASE,
        revision=previous_event.revision + 1,
        previous_event_id=previous_event.event_id,
        previous_event_hash=previous_event.event_hash,
        incumbent_registry_id=previous_event.incumbent_registry_id,
        incumbent_registry_revision=previous_event.incumbent_registry_revision,
        incumbent_artifact_hash=previous_event.incumbent_artifact_hash,
        active_pointer_version=previous_event.active_pointer_version,
        deployment_revision=previous_event.deployment_revision,
        actor_membership_id=actor_membership_id,
        mfa_verified_at=mfa_verified_at,
        mfa_method=mfa_method,
        reason=reason,
        idempotency_key=idempotency_key,
        request_id=request_id,
        created_at=created_at,
    )


def _uuid(value: object, name: str) -> None:
    if type(value) is not UUID or value.int == 0:
        raise ValueError(f"{name} phải là UUID khác zero.")


def _hash(value: object, name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} phải là SHA-256 hex chữ thường.")


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có timezone.")
    return value.astimezone(UTC)


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "LearningIncidentHoldAction",
    "LearningIncidentHoldEvent",
    "create_hold_event",
    "create_release_event",
]
