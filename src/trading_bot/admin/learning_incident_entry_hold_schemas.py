"""Schema HTTP nghiêm ngặt cho lịch sử HOLD/RELEASE learning incident."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from trading_bot.domain.learning.incident_entry_hold import LearningIncidentHoldEvent


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LearningIncidentHoldRequest(StrictRequest):
    assessment_id: UUID
    assessment_hash: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")
    expected_revision: int = Field(strict=True, ge=0)
    expected_deployment_revision: int = Field(strict=True, ge=1)
    expected_active_pointer_version: int = Field(strict=True, ge=1)
    reason: str = Field(strict=True, min_length=1, max_length=500)


class LearningIncidentReleaseRequest(StrictRequest):
    expected_revision: int = Field(strict=True, ge=1)
    reason: str = Field(strict=True, min_length=1, max_length=500)


class LearningIncidentHoldEventResponse(BaseModel):
    event_id: UUID
    workspace_id: UUID
    proposal_id: UUID
    proposal_hash: str
    assessment_id: UUID
    assessment_hash: str
    action: str
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
    mfa_method: str
    reason: str
    idempotency_key: UUID
    request_id: str
    intent_hash: str
    event_hash: str
    created_at: datetime

    @classmethod
    def from_event(cls, event: LearningIncidentHoldEvent) -> LearningIncidentHoldEventResponse:
        return cls(
            event_id=event.event_id,
            workspace_id=event.workspace_id,
            proposal_id=event.proposal_id,
            proposal_hash=event.proposal_hash,
            assessment_id=event.assessment_id,
            assessment_hash=event.assessment_hash,
            action=event.action.value,
            revision=event.revision,
            previous_event_id=event.previous_event_id,
            previous_event_hash=event.previous_event_hash,
            incumbent_registry_id=event.incumbent_registry_id,
            incumbent_registry_revision=event.incumbent_registry_revision,
            incumbent_artifact_hash=event.incumbent_artifact_hash,
            active_pointer_version=event.active_pointer_version,
            deployment_revision=event.deployment_revision,
            actor_membership_id=event.actor_membership_id,
            mfa_verified_at=event.mfa_verified_at,
            mfa_method=str(event.mfa_method),
            reason=event.reason,
            idempotency_key=event.idempotency_key,
            request_id=event.request_id,
            intent_hash=event.intent_hash,
            event_hash=event.event_hash,
            created_at=event.created_at,
        )


class LearningIncidentHoldHistoryResponse(BaseModel):
    events: tuple[LearningIncidentHoldEventResponse, ...]

    @classmethod
    def from_events(
        cls, events: tuple[LearningIncidentHoldEvent, ...]
    ) -> LearningIncidentHoldHistoryResponse:
        return cls(
            events=tuple(LearningIncidentHoldEventResponse.from_event(item) for item in events)
        )


__all__ = [
    "LearningIncidentHoldEventResponse",
    "LearningIncidentHoldHistoryResponse",
    "LearningIncidentHoldRequest",
    "LearningIncidentReleaseRequest",
]
