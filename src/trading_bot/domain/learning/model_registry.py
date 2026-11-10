"""Quy tắc trạng thái bất biến cho registry mô hình learning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class LearningModelRegistryState(StrEnum):
    DRAFT = "draft"
    EVALUATED = "evaluated"
    REJECTED = "rejected"
    PAPER_ACTIVE = "paper_active"
    RETIRED = "retired"


class LearningModelRegistryAction(StrEnum):
    REGISTER = "register"
    EVALUATE = "evaluate"
    APPROVE = "approve"
    REJECT = "reject"
    ACTIVATE = "activate"
    ROLLBACK = "rollback"
    RETIRE = "retire"


class LearningModelTransitionRejected(ValueError):
    """Thao tác registry không hợp lệ với trạng thái hiện tại."""


@dataclass(frozen=True, slots=True)
class LearningModelRegistryEntry:
    """Snapshot metadata của artifact đã đăng ký trong đúng một workspace."""

    id: UUID
    workspace_id: UUID
    artifact_id: UUID
    artifact_hash: str
    manifest_hash: str
    fold_plan_hash: str
    approval_hash: str
    state: LearningModelRegistryState
    revision: int
    evaluation_report_id: UUID | None
    evaluation_report_hash: str | None
    created_by_membership_id: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class LearningModelRegistryEvent:
    """Snapshot audit event; không mang feature/label payload."""

    id: UUID
    workspace_id: UUID
    registry_id: UUID
    sequence: int
    action: LearningModelRegistryAction
    from_state: LearningModelRegistryState | None
    to_state: LearningModelRegistryState
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


@dataclass(frozen=True, slots=True)
class LearningModelActivePointer:
    """Con trỏ CAS workspace-scoped; không tự nối scorer hay execution."""

    workspace_id: UUID
    registry_id: UUID | None
    version: int
    updated_by_membership_id: UUID | None
    changed_at: datetime


def transition_learning_model_state(
    current_state: LearningModelRegistryState | None,
    action: LearningModelRegistryAction,
    *,
    has_evaluation: bool = False,
    approved: bool = False,
    previously_active: bool = False,
) -> LearningModelRegistryState:
    """Trả trạng thái tiếp theo; approval là sự kiện riêng trước khi kích hoạt."""
    if not isinstance(action, LearningModelRegistryAction):
        raise ValueError("action phải là LearningModelRegistryAction.")
    if current_state is not None and not isinstance(current_state, LearningModelRegistryState):
        raise ValueError("current_state phải là trạng thái registry đã biết.")

    if action is LearningModelRegistryAction.REGISTER:
        if current_state is not None:
            raise LearningModelTransitionRejected("Chỉ đăng ký model một lần.")
        return LearningModelRegistryState.DRAFT

    if current_state is None:
        raise LearningModelTransitionRejected("Model chưa được đăng ký.")

    if action is LearningModelRegistryAction.EVALUATE:
        if current_state is not LearningModelRegistryState.DRAFT or not has_evaluation:
            raise LearningModelTransitionRejected("Cần evaluation gắn đúng model draft.")
        return LearningModelRegistryState.EVALUATED

    if action is LearningModelRegistryAction.APPROVE:
        if current_state is not LearningModelRegistryState.EVALUATED or not has_evaluation:
            raise LearningModelTransitionRejected("Chỉ duyệt model đã có evaluation.")
        return LearningModelRegistryState.EVALUATED

    if action is LearningModelRegistryAction.REJECT:
        if current_state not in {
            LearningModelRegistryState.DRAFT,
            LearningModelRegistryState.EVALUATED,
        }:
            raise LearningModelTransitionRejected("Không thể từ chối model ở trạng thái này.")
        return LearningModelRegistryState.REJECTED

    if action is LearningModelRegistryAction.ACTIVATE:
        if (
            current_state is not LearningModelRegistryState.EVALUATED
            or not has_evaluation
            or not approved
        ):
            raise LearningModelTransitionRejected("Kích hoạt cần evaluation và approval hiện hành.")
        return LearningModelRegistryState.PAPER_ACTIVE

    if action is LearningModelRegistryAction.ROLLBACK:
        if (
            current_state is not LearningModelRegistryState.RETIRED
            or not has_evaluation
            or not approved
            or not previously_active
        ):
            raise LearningModelTransitionRejected(
                "Rollback chỉ được khôi phục model PAPER từng active và đã duyệt."
            )
        return LearningModelRegistryState.PAPER_ACTIVE

    if action is LearningModelRegistryAction.RETIRE:
        if current_state is LearningModelRegistryState.RETIRED:
            raise LearningModelTransitionRejected("Model đã retired.")
        return LearningModelRegistryState.RETIRED

    raise LearningModelTransitionRejected("Thao tác registry không được hỗ trợ.")


__all__ = [
    "LearningModelActivePointer",
    "LearningModelRegistryAction",
    "LearningModelRegistryEntry",
    "LearningModelRegistryEvent",
    "LearningModelRegistryState",
    "LearningModelTransitionRejected",
    "transition_learning_model_state",
]
