"""Schema quản trị registry; chỉ công bố metadata và lineage đã băm."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from trading_bot.domain.learning.model_registry import (
    LearningModelActivePointer,
    LearningModelRegistryEntry,
    LearningModelRegistryEvent,
)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterArtifactRequest(StrictRequest):
    artifact_id: UUID
    reason: str = Field(min_length=12, max_length=500)


class AttachEvaluationRequest(StrictRequest):
    evaluation_report_id: UUID
    evaluation_report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision: int = Field(strict=True, ge=1)
    reason: str = Field(min_length=12, max_length=500)


class RegistryDecisionRequest(StrictRequest):
    expected_revision: int = Field(strict=True, ge=1)
    reason: str = Field(min_length=12, max_length=500)


class RejectModelRequest(RegistryDecisionRequest):
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")


class PointerChangeRequest(StrictRequest):
    expected_revision: int = Field(strict=True, ge=1)
    expected_pointer_version: int = Field(strict=True, ge=0)
    expected_current_registry_id: UUID | None
    reason: str = Field(min_length=12, max_length=500)


class RegistryEntryResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    artifact_id: UUID
    artifact_hash: str
    manifest_hash: str
    fold_plan_hash: str
    approval_hash: str
    state: str
    revision: int
    evaluation_report_id: UUID | None
    evaluation_report_hash: str | None
    created_by_membership_id: UUID
    created_at: datetime

    @classmethod
    def from_domain(cls, entry: LearningModelRegistryEntry) -> RegistryEntryResponse:
        return cls(
            id=entry.id,
            workspace_id=entry.workspace_id,
            artifact_id=entry.artifact_id,
            artifact_hash=entry.artifact_hash,
            manifest_hash=entry.manifest_hash,
            fold_plan_hash=entry.fold_plan_hash,
            approval_hash=entry.approval_hash,
            state=entry.state.value,
            revision=entry.revision,
            evaluation_report_id=entry.evaluation_report_id,
            evaluation_report_hash=entry.evaluation_report_hash,
            created_by_membership_id=entry.created_by_membership_id,
            created_at=entry.created_at,
        )


class RegistryEventResponse(BaseModel):
    id: UUID
    registry_id: UUID
    sequence: int
    action: str
    from_state: str | None
    to_state: str
    actor_membership_id: UUID
    policy_version: str
    reason_code: str
    reason: str
    idempotency_key: UUID
    mfa_verified_at: datetime | None
    expected_revision: int | None
    expected_pointer_version: int | None
    evaluation_report_id: UUID | None
    evaluation_report_hash: str | None
    created_at: datetime

    @classmethod
    def from_domain(cls, event: LearningModelRegistryEvent) -> RegistryEventResponse:
        return cls(
            id=event.id,
            registry_id=event.registry_id,
            sequence=event.sequence,
            action=event.action.value,
            from_state=event.from_state.value if event.from_state else None,
            to_state=event.to_state.value,
            actor_membership_id=event.actor_membership_id,
            policy_version=event.policy_version,
            reason_code=event.reason_code,
            reason=event.reason,
            idempotency_key=event.idempotency_key,
            mfa_verified_at=event.mfa_verified_at,
            expected_revision=event.expected_revision,
            expected_pointer_version=event.expected_pointer_version,
            evaluation_report_id=event.evaluation_report_id,
            evaluation_report_hash=event.evaluation_report_hash,
            created_at=event.created_at,
        )


class ActivePointerResponse(BaseModel):
    workspace_id: UUID
    registry_id: UUID | None
    version: int
    updated_by_membership_id: UUID | None
    changed_at: datetime

    @classmethod
    def from_domain(cls, pointer: LearningModelActivePointer) -> ActivePointerResponse:
        return cls(
            workspace_id=pointer.workspace_id,
            registry_id=pointer.registry_id,
            version=pointer.version,
            updated_by_membership_id=pointer.updated_by_membership_id,
            changed_at=pointer.changed_at,
        )


__all__ = [
    "ActivePointerResponse",
    "AttachEvaluationRequest",
    "PointerChangeRequest",
    "RegisterArtifactRequest",
    "RegistryDecisionRequest",
    "RegistryEntryResponse",
    "RegistryEventResponse",
    "RejectModelRequest",
]
