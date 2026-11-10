"""Chính sách recovery bảo toàn quyền trainer receipt một lần."""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

_WORKER_ID = re.compile(r"[A-Za-z0-9_.:-]{1,120}\Z")


class LearningTrainingJobRecoveryAction(StrEnum):
    """Hành động lease recovery theo receipt và artifact đã persist."""

    REQUEUE = "requeue"
    FAIL_ATTEMPT = "fail_attempt"
    ALREADY_SUCCEEDED = "already_succeeded"


@dataclass(frozen=True, slots=True)
class LearningTrainingJobClaim:
    """Lease generation đã commit, dùng để fence mọi update của worker cũ."""

    job_id: UUID
    workspace_id: UUID
    attempt_id: UUID
    worker_id: str
    generation: int
    lease_until: datetime

    def __post_init__(self) -> None:
        for name in ("job_id", "workspace_id", "attempt_id"):
            if type(getattr(self, name)) is not UUID or getattr(self, name).int == 0:
                raise ValueError(f"{name} phải là UUID khác zero.")
        if not isinstance(self.worker_id, str) or _WORKER_ID.fullmatch(self.worker_id) is None:
            raise ValueError("worker_id không hợp lệ.")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("generation phải là số nguyên dương.")
        if not isinstance(self.lease_until, datetime) or self.lease_until.tzinfo is None:
            raise ValueError("lease_until phải có múi giờ.")


def recover_learning_training_job(
    *,
    has_receipt: bool,
    has_artifact: bool,
) -> LearningTrainingJobRecoveryAction:
    """Chỉ requeue nếu receipt chưa commit; không cấp lại batch đã disclose."""
    if type(has_receipt) is not bool or type(has_artifact) is not bool:
        raise ValueError("Receipt/artifact indicators phải là bool.")
    if has_artifact and not has_receipt:
        raise ValueError("Artifact trainer không thể tồn tại nếu không có receipt.")
    if has_artifact:
        return LearningTrainingJobRecoveryAction.ALREADY_SUCCEEDED
    if has_receipt:
        return LearningTrainingJobRecoveryAction.FAIL_ATTEMPT
    return LearningTrainingJobRecoveryAction.REQUEUE


__all__ = [
    "LearningTrainingJobClaim",
    "LearningTrainingJobRecoveryAction",
    "recover_learning_training_job",
]
