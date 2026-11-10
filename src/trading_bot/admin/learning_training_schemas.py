"""Schema API learning training chỉ công bố metadata, không có sample payload."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from trading_bot.adapters.database.repositories.learning_training_admin import (
    LearningTrainingFoldOption,
    LearningTrainingJobSnapshot,
)
from trading_bot.application.learning_training_admin import LearningTrainingSubmission


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnqueueTrainingRequest(StrictRequest):
    normalized_dataset_id: UUID
    fold_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class LearningTrainingFoldOptionResponse(BaseModel):
    normalized_dataset_id: UUID
    manifest_hash: str
    split_policy_version: str
    fold_plan_hash: str
    fold_id: str
    training_samples: int
    validation_samples: int
    feature_count: int

    @classmethod
    def from_domain(cls, value: LearningTrainingFoldOption) -> LearningTrainingFoldOptionResponse:
        return cls(
            normalized_dataset_id=value.normalized_dataset_id,
            manifest_hash=value.manifest_hash,
            split_policy_version=value.split_policy_version,
            fold_plan_hash=value.fold_plan_hash,
            fold_id=value.fold_id,
            training_samples=value.training_samples,
            validation_samples=value.validation_samples,
            feature_count=value.feature_count,
        )


class LearningTrainingJobResponse(BaseModel):
    job_id: UUID
    attempt_id: UUID
    normalized_dataset_id: UUID
    manifest_hash: str
    fold_plan_hash: str
    fold_id: str
    status: str
    stage: str
    progress: int
    attempt_count: int
    lease_generation: int
    cancel_requested: bool
    error_code: str | None
    training_samples: int
    validation_samples: int
    feature_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: LearningTrainingJobSnapshot) -> LearningTrainingJobResponse:
        return cls(
            job_id=value.job_id,
            attempt_id=value.attempt_id,
            normalized_dataset_id=value.normalized_dataset_id,
            manifest_hash=value.manifest_hash,
            fold_plan_hash=value.fold_plan_hash,
            fold_id=value.fold_id,
            status=value.status,
            stage=value.stage,
            progress=value.progress,
            attempt_count=value.attempt_count,
            lease_generation=value.lease_generation,
            cancel_requested=value.cancel_requested,
            error_code=value.error_code,
            training_samples=value.training_samples,
            validation_samples=value.validation_samples,
            feature_count=value.feature_count,
            created_at=value.created_at,
            updated_at=value.updated_at,
        )


class LearningTrainingSnapshotResponse(BaseModel):
    eligible_folds: list[LearningTrainingFoldOptionResponse]
    jobs: list[LearningTrainingJobResponse]


class LearningTrainingSubmissionResponse(BaseModel):
    attempt_id: UUID
    job_id: UUID | None
    normalized_dataset_id: UUID
    manifest_hash: str
    fold_id: str
    trial_index: int
    seed: int
    status: str
    duplicate: bool
    error_code: str | None

    @classmethod
    def from_domain(cls, value: LearningTrainingSubmission) -> LearningTrainingSubmissionResponse:
        return cls(
            attempt_id=value.attempt_id,
            job_id=value.job_id,
            normalized_dataset_id=value.normalized_dataset_id,
            manifest_hash=value.manifest_hash,
            fold_id=value.fold_id,
            trial_index=value.trial_index,
            seed=value.seed,
            status=value.status,
            duplicate=value.duplicate,
            error_code=value.error_code,
        )


__all__ = [
    "EnqueueTrainingRequest",
    "LearningTrainingFoldOptionResponse",
    "LearningTrainingJobResponse",
    "LearningTrainingSnapshotResponse",
    "LearningTrainingSubmissionResponse",
]
