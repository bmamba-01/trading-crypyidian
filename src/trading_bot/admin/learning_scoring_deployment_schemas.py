"""Schema HTTP cho scorer deployment; caller chỉ cung cấp mode, CAS và reason."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentAdminSnapshot,
    ScorerDeploymentEvent,
    ScorerDeploymentMode,
)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScorerDeploymentRequest(StrictRequest):
    mode: ScorerDeploymentMode
    evaluation_report_id: UUID | None = None
    expected_pointer_version: int = Field(strict=True, ge=0)
    expected_deployment_revision: int = Field(strict=True, ge=0)
    reason: str = Field(min_length=12, max_length=500)

    @model_validator(mode="after")
    def validate_report_selection(self) -> ScorerDeploymentRequest:
        if self.mode is ScorerDeploymentMode.DISABLED:
            if self.evaluation_report_id is not None:
                raise ValueError("DISABLED không nhận evaluation_report_id.")
        elif self.evaluation_report_id is None:
            raise ValueError("SHADOW/PAPER_GATE cần evaluation_report_id đã lưu.")
        return self


class ScorerDeploymentResponse(BaseModel):
    workspace_id: UUID
    mode: str
    deployment_event_id: UUID | None
    deployment_revision: int
    active_pointer_version: int
    health: str
    runtime_usable: bool
    evaluation_report_id: UUID | None = None
    evaluation_report_hash: str | None = None
    evaluation_status: str | None = None
    registry_id: UUID | None = None
    registry_revision: int | None = None
    scorer_artifact_id: UUID | None = None
    scorer_artifact_hash: str | None = None
    proof_id: UUID | None = None
    proof_hash: str | None = None
    policy_id: str | None = None
    policy_hash: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_snapshot(cls, snapshot: ScorerDeploymentAdminSnapshot) -> ScorerDeploymentResponse:
        return cls(
            workspace_id=snapshot.workspace_id,
            mode=snapshot.mode.value,
            deployment_event_id=snapshot.deployment_event_id,
            deployment_revision=snapshot.deployment_revision,
            active_pointer_version=snapshot.active_pointer_version,
            health=snapshot.health.value,
            runtime_usable=snapshot.runtime_usable,
        )

    @classmethod
    def from_event(cls, event: ScorerDeploymentEvent) -> ScorerDeploymentResponse:
        target = event.target
        return cls(
            workspace_id=event.workspace_id,
            mode=event.mode.value,
            deployment_event_id=event.event_id,
            deployment_revision=event.revision,
            active_pointer_version=event.expected_pointer_version,
            health=("DISABLED" if target is None else "RIGHTS_RECHECK_REQUIRED"),
            runtime_usable=False,
            evaluation_report_id=target.evaluation_report_id if target else None,
            evaluation_report_hash=target.evaluation_report_hash if target else None,
            evaluation_status=target.evaluation_status if target else None,
            registry_id=target.registry_id if target else None,
            registry_revision=target.registry_revision if target else None,
            scorer_artifact_id=target.scorer_artifact_id if target else None,
            scorer_artifact_hash=target.scorer_artifact_hash if target else None,
            proof_id=target.proof_id if target else None,
            proof_hash=target.proof_hash if target else None,
            policy_id=target.policy_id if target else None,
            policy_hash=target.policy_hash if target else None,
            created_at=event.created_at,
        )


__all__ = ["ScorerDeploymentRequest", "ScorerDeploymentResponse"]
