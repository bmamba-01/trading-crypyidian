"""Hợp đồng trạng thái và chi phí cho các lần chạy learning."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from trading_bot.domain.learning.resources import LearningResourcePlan, LearningResourceRequest

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_TRIALS_PER_PARAMETER_FAMILY = 100


class LearningAttemptState(StrEnum):
    """Trạng thái append-only của một lần chạy train/evaluation."""

    QUEUED = "queued"
    REJECTED = "rejected"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LearningAttemptReasonCode(StrEnum):
    """Mã lý do đã chuẩn hóa; không lưu exception hoặc thông tin nhạy cảm."""

    RESOURCE_LIMIT = "resource_limit"
    APPROVAL_MISSING = "approval_missing"
    RIGHTS_DENIED = "rights_denied"
    INVALID_REQUEST = "invalid_request"
    ADAPTER_ERROR = "adapter_error"
    CANCELLED_BY_OPERATOR = "cancelled_by_operator"


class LearningAttemptActor(StrEnum):
    """Principal nội bộ ghi event accounting."""

    ADMIN_WEB = "crypidian_admin_web"
    TRAINER_WORKER = "crypidian_trainer_worker"
    LEARNING_EVALUATOR = "crypidian_learning_evaluator"


@dataclass(frozen=True, slots=True)
class LearningTrainingAttempt:
    """Ghim dữ liệu, approval, trial/fold, phiên bản và ngân sách của một lượt train."""

    id: UUID
    workspace_id: UUID
    actor_membership_id: UUID
    idempotency_key: UUID
    manifest_hash: str
    split_policy_version: str
    fold_plan_hash: str
    approval_hash: str
    parameter_family_hash: str
    trial_index: int
    trial_count: int
    fold_id: str
    seed: int
    trainer_version: str
    dependency_fingerprint: str
    resource_policy_version: str
    resource_request: LearningResourceRequest
    resource_plan: LearningResourcePlan | None
    rejection_reason_code: LearningAttemptReasonCode | None = None

    def __post_init__(self) -> None:
        for name in ("id", "workspace_id", "actor_membership_id", "idempotency_key"):
            if type(getattr(self, name)) is not UUID:
                raise ValueError(f"{name} phải là UUID.")
        for name in (
            "manifest_hash",
            "fold_plan_hash",
            "approval_hash",
            "parameter_family_hash",
            "fold_id",
            "dependency_fingerprint",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"{name} phải là SHA-256 dạng hex thường.")
        for name, value, maximum in (
            ("split_policy_version", self.split_policy_version, 128),
            ("trainer_version", self.trainer_version, 64),
            ("resource_policy_version", self.resource_policy_version, 64),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError(f"{name} không hợp lệ.")
        if (
            type(self.trial_index) is not int
            or type(self.trial_count) is not int
            or not 0 <= self.trial_index < self.trial_count <= MAX_TRIALS_PER_PARAMETER_FAMILY
        ):
            raise ValueError("trial_index/count nằm ngoài giới hạn parameter family.")
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("seed phải là số nguyên không âm.")
        if type(self.resource_request) is not LearningResourceRequest:
            raise ValueError("resource_request phải là LearningResourceRequest.")
        if self.resource_plan is not None and type(self.resource_plan) is not LearningResourcePlan:
            raise ValueError("resource_plan không hợp lệ.")
        if self.resource_plan is None:
            if self.rejection_reason_code is not LearningAttemptReasonCode.RESOURCE_LIMIT:
                raise ValueError("Attempt bị từ chối ngân sách cần mã RESOURCE_LIMIT.")
        elif self.rejection_reason_code is not None:
            raise ValueError("Attempt có resource plan không được mang rejection reason.")
        else:
            plan = self.resource_plan
            request = self.resource_request
            if (
                plan.sample_count != request.training_samples + request.validation_samples
                or plan.feature_count != request.feature_count
                or plan.max_steps != request.max_steps
                or plan.max_runtime_seconds != request.max_runtime_seconds
                or plan.memory_limit_bytes != request.memory_limit_bytes
            ):
                raise ValueError("resource_plan không khớp resource_request.")

    @property
    def initial_state(self) -> LearningAttemptState:
        return (
            LearningAttemptState.QUEUED
            if self.resource_plan is not None
            else LearningAttemptState.REJECTED
        )

    @property
    def canonical_json(self) -> str:
        """Canonical request để retry không thể đổi budget hoặc dataset lineage."""
        return json.dumps(
            {
                "actor_membership_id": str(self.actor_membership_id),
                "approval_hash": self.approval_hash,
                "dependency_fingerprint": self.dependency_fingerprint,
                "fold_id": self.fold_id,
                "fold_plan_hash": self.fold_plan_hash,
                "id": str(self.id),
                "idempotency_key": str(self.idempotency_key),
                "manifest_hash": self.manifest_hash,
                "parameter_family_hash": self.parameter_family_hash,
                "rejection_reason_code": (
                    self.rejection_reason_code.value if self.rejection_reason_code else None
                ),
                "resource_plan": _resource_plan_payload(self.resource_plan),
                "resource_policy_version": self.resource_policy_version,
                "resource_request": _resource_request_payload(self.resource_request),
                "seed": self.seed,
                "split_policy_version": self.split_policy_version,
                "trainer_version": self.trainer_version,
                "trial_count": self.trial_count,
                "trial_index": self.trial_index,
                "workspace_id": str(self.workspace_id),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


_TERMINAL_STATES = frozenset(
    {
        LearningAttemptState.SUCCEEDED,
        LearningAttemptState.FAILED,
        LearningAttemptState.CANCELLED,
    }
)
_REASON_REQUIRED_STATES = frozenset(
    {
        LearningAttemptState.REJECTED,
        LearningAttemptState.FAILED,
        LearningAttemptState.CANCELLED,
    }
)
_TRANSITIONS = {
    None: frozenset({LearningAttemptState.QUEUED, LearningAttemptState.REJECTED}),
    LearningAttemptState.QUEUED: frozenset(
        {LearningAttemptState.RUNNING, LearningAttemptState.FAILED, LearningAttemptState.CANCELLED}
    ),
    LearningAttemptState.RUNNING: frozenset(
        {
            LearningAttemptState.SUCCEEDED,
            LearningAttemptState.FAILED,
            LearningAttemptState.CANCELLED,
        }
    ),
    LearningAttemptState.REJECTED: frozenset(),
    LearningAttemptState.SUCCEEDED: frozenset(),
    LearningAttemptState.FAILED: frozenset(),
    LearningAttemptState.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class LearningAttemptEvent:
    """Một thay đổi trạng thái bất biến cùng cost/memory đã đo hoặc lý do chuẩn."""

    attempt_id: UUID
    sequence: int
    state: LearningAttemptState
    occurred_at: datetime
    actor_principal: LearningAttemptActor
    reason_code: LearningAttemptReasonCode | None = None
    elapsed_milliseconds: int | None = None
    peak_memory_bytes: int | None = None
    actual_operation_units: int | None = None

    def __post_init__(self) -> None:
        if type(self.attempt_id) is not UUID:
            raise ValueError("attempt_id phải là UUID.")
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("sequence phải là số nguyên dương; không chấp nhận bool.")
        if type(self.state) is not LearningAttemptState:
            raise ValueError("state không hợp lệ.")
        if type(self.actor_principal) is not LearningAttemptActor:
            raise ValueError("actor_principal phải là principal learning đã cho phép.")
        if self.reason_code is not None and type(self.reason_code) is not LearningAttemptReasonCode:
            raise ValueError("reason_code phải là mã đã chuẩn hóa, không nhận chuỗi tự do.")

        timestamp = self.occurred_at
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise ValueError("occurred_at phải có múi giờ.")
        object.__setattr__(self, "occurred_at", timestamp.astimezone(UTC))

        if (self.state in _REASON_REQUIRED_STATES) != (self.reason_code is not None):
            raise ValueError("Trạng thái terminal/rejected cần đúng một mã lý do chuẩn hóa.")
        if self.state in _TERMINAL_STATES and self.elapsed_milliseconds is None:
            raise ValueError("Event terminal cần ghi thời gian chạy đã đo.")
        if self.state not in _TERMINAL_STATES and self.elapsed_milliseconds is not None:
            raise ValueError("Chỉ event terminal mới được ghi thời gian chạy.")

        for name in ("elapsed_milliseconds", "peak_memory_bytes", "actual_operation_units"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} phải là số nguyên không âm.")


def validate_learning_attempt_transition(
    previous: LearningAttemptEvent | None,
    next_event: LearningAttemptEvent,
) -> None:
    """Kiểm state transition, identity, sequence và timestamp trước khi append."""
    if type(next_event) is not LearningAttemptEvent:
        raise ValueError("next_event phải là LearningAttemptEvent.")
    if previous is not None and type(previous) is not LearningAttemptEvent:
        raise ValueError("previous phải là LearningAttemptEvent hoặc None.")

    previous_state = previous.state if previous is not None else None
    if next_event.state not in _TRANSITIONS[previous_state]:
        raise ValueError("Chuyển trạng thái learning attempt không hợp lệ.")
    if previous is None:
        if next_event.sequence != 1:
            raise ValueError("Event đầu tiên phải có sequence bằng 1.")
        return
    if next_event.attempt_id != previous.attempt_id:
        raise ValueError("Các event phải cùng attempt_id.")
    if next_event.sequence != previous.sequence + 1:
        raise ValueError("Sequence event phải liên tục.")
    if next_event.occurred_at < previous.occurred_at:
        raise ValueError("occurred_at không được lùi so với event trước.")


__all__ = [
    "MAX_TRIALS_PER_PARAMETER_FAMILY",
    "LearningAttemptActor",
    "LearningAttemptEvent",
    "LearningAttemptReasonCode",
    "LearningAttemptState",
    "LearningTrainingAttempt",
    "validate_learning_attempt_transition",
]


def _resource_request_payload(request: LearningResourceRequest) -> dict[str, int]:
    return {
        "feature_count": request.feature_count,
        "max_runtime_seconds": request.max_runtime_seconds,
        "max_steps": request.max_steps,
        "memory_limit_bytes": request.memory_limit_bytes,
        "training_samples": request.training_samples,
        "validation_samples": request.validation_samples,
    }


def _resource_plan_payload(plan: LearningResourcePlan | None) -> dict[str, int] | None:
    if plan is None:
        return None
    return {
        "estimated_memory_bytes": plan.estimated_memory_bytes,
        "estimated_operation_units": plan.estimated_operation_units,
        "feature_count": plan.feature_count,
        "max_runtime_seconds": plan.max_runtime_seconds,
        "max_steps": plan.max_steps,
        "memory_limit_bytes": plan.memory_limit_bytes,
        "sample_count": plan.sample_count,
    }
