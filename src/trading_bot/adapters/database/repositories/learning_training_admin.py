"""Truy vấn metadata và lệnh admin cho hàng đợi trainer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.learning_attempt_entities import (
    LearningFoldResourceProfileRecord,
    LearningTrainingAttemptRecord,
)
from trading_bot.adapters.database.models.learning_entities import LearningSplitApprovalRecord
from trading_bot.adapters.database.models.learning_fold_sample_entities import (
    NormalizedLearningDatasetRecord,
)
from trading_bot.adapters.database.models.learning_training_job_entities import (
    LearningTrainingJobRecord,
)
from trading_bot.adapters.database.repositories.learning_training_jobs import (
    LearningTrainingJobRepository,
)


@dataclass(frozen=True, slots=True)
class LearningTrainingFoldOption:
    """Fold đã được duyệt kèm đúng metadata ngân sách; không chứa payload."""

    normalized_dataset_id: UUID
    manifest_hash: str
    split_policy_version: str
    fold_plan_hash: str
    approval_hash: str
    fold_id: str
    training_samples: int
    validation_samples: int
    feature_count: int
    profile_hash: str


@dataclass(frozen=True, slots=True)
class LearningTrainingJobSnapshot:
    """Read model job/attempt chỉ gồm metadata vận hành đã lưu."""

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


class LearningTrainingAdminRepository:
    """Admin chỉ đọc metadata profile và quản lý lifecycle job trong workspace."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_eligible_folds(
        self, *, workspace_id: UUID
    ) -> tuple[LearningTrainingFoldOption, ...]:
        await self._bind_workspace(workspace_id)
        rows = await self._session.execute(
            select(
                LearningFoldResourceProfileRecord.normalized_dataset_id,
                LearningFoldResourceProfileRecord.manifest_hash,
                LearningFoldResourceProfileRecord.fold_plan_hash,
                LearningFoldResourceProfileRecord.fold_id,
                LearningFoldResourceProfileRecord.training_samples,
                LearningFoldResourceProfileRecord.validation_samples,
                LearningFoldResourceProfileRecord.feature_count,
                LearningFoldResourceProfileRecord.profile_hash,
                NormalizedLearningDatasetRecord.split_policy_version,
                NormalizedLearningDatasetRecord.approval_hash,
            )
            .join(
                NormalizedLearningDatasetRecord,
                and_(
                    NormalizedLearningDatasetRecord.workspace_id
                    == LearningFoldResourceProfileRecord.workspace_id,
                    NormalizedLearningDatasetRecord.id
                    == LearningFoldResourceProfileRecord.normalized_dataset_id,
                    NormalizedLearningDatasetRecord.manifest_hash
                    == LearningFoldResourceProfileRecord.manifest_hash,
                    NormalizedLearningDatasetRecord.fold_plan_hash
                    == LearningFoldResourceProfileRecord.fold_plan_hash,
                ),
            )
            .join(
                LearningSplitApprovalRecord,
                and_(
                    LearningSplitApprovalRecord.workspace_id
                    == NormalizedLearningDatasetRecord.workspace_id,
                    LearningSplitApprovalRecord.manifest_hash
                    == NormalizedLearningDatasetRecord.manifest_hash,
                    LearningSplitApprovalRecord.fold_plan_hash
                    == NormalizedLearningDatasetRecord.fold_plan_hash,
                    LearningSplitApprovalRecord.split_policy_version
                    == NormalizedLearningDatasetRecord.split_policy_version,
                    LearningSplitApprovalRecord.approval_hash
                    == NormalizedLearningDatasetRecord.approval_hash,
                ),
            )
            .where(
                LearningFoldResourceProfileRecord.workspace_id == workspace_id,
            )
            .order_by(
                LearningFoldResourceProfileRecord.created_at.desc(),
                LearningFoldResourceProfileRecord.fold_id,
            )
            .limit(200)
        )
        options = []
        for row in rows.all():
            options.append(
                LearningTrainingFoldOption(
                    normalized_dataset_id=row.normalized_dataset_id,
                    manifest_hash=row.manifest_hash,
                    split_policy_version=row.split_policy_version,
                    fold_plan_hash=row.fold_plan_hash,
                    approval_hash=row.approval_hash,
                    fold_id=row.fold_id,
                    training_samples=row.training_samples,
                    validation_samples=row.validation_samples,
                    feature_count=row.feature_count,
                    profile_hash=row.profile_hash,
                )
            )
        return tuple(options)

    async def list_jobs(self, *, workspace_id: UUID) -> tuple[LearningTrainingJobSnapshot, ...]:
        await self._bind_workspace(workspace_id)
        result = await self._session.execute(
            select(LearningTrainingJobRecord, LearningTrainingAttemptRecord)
            .join(
                LearningTrainingAttemptRecord,
                and_(
                    LearningTrainingAttemptRecord.workspace_id
                    == LearningTrainingJobRecord.workspace_id,
                    LearningTrainingAttemptRecord.id == LearningTrainingJobRecord.attempt_id,
                ),
            )
            .where(LearningTrainingJobRecord.workspace_id == workspace_id)
            .order_by(
                LearningTrainingJobRecord.created_at.desc(),
                LearningTrainingJobRecord.id,
            )
            .limit(100)
        )
        return tuple(_job_snapshot(job, attempt) for job, attempt in result.all())

    async def lock_idempotency_scope(self, *, workspace_id: UUID, idempotency_key: UUID) -> None:
        await self._bind_workspace(workspace_id)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"learning-attempt-idempotency:{workspace_id}:{idempotency_key}"},
        )

    async def lock_trial_family(
        self, *, workspace_id: UUID, family_hash: str, fold_id: str
    ) -> None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"learning-attempt-family-next:{workspace_id}:{family_hash}:{fold_id}"},
        )

    async def get_attempt_by_idempotency(
        self, *, workspace_id: UUID, idempotency_key: UUID
    ) -> LearningTrainingAttemptRecord | None:
        return await self._session.scalar(
            select(LearningTrainingAttemptRecord).where(
                LearningTrainingAttemptRecord.workspace_id == workspace_id,
                LearningTrainingAttemptRecord.idempotency_key == idempotency_key,
            )
        )

    async def get_fold_option(
        self,
        *,
        workspace_id: UUID,
        normalized_dataset_id: UUID,
        fold_id: str,
    ) -> LearningTrainingFoldOption | None:
        await self._bind_workspace(workspace_id)
        result = await self._session.execute(
            select(
                LearningFoldResourceProfileRecord.normalized_dataset_id,
                LearningFoldResourceProfileRecord.manifest_hash,
                LearningFoldResourceProfileRecord.fold_plan_hash,
                LearningFoldResourceProfileRecord.fold_id,
                LearningFoldResourceProfileRecord.training_samples,
                LearningFoldResourceProfileRecord.validation_samples,
                LearningFoldResourceProfileRecord.feature_count,
                LearningFoldResourceProfileRecord.profile_hash,
                NormalizedLearningDatasetRecord.split_policy_version,
                NormalizedLearningDatasetRecord.approval_hash,
            )
            .join(
                NormalizedLearningDatasetRecord,
                and_(
                    NormalizedLearningDatasetRecord.workspace_id
                    == LearningFoldResourceProfileRecord.workspace_id,
                    NormalizedLearningDatasetRecord.id
                    == LearningFoldResourceProfileRecord.normalized_dataset_id,
                    NormalizedLearningDatasetRecord.manifest_hash
                    == LearningFoldResourceProfileRecord.manifest_hash,
                    NormalizedLearningDatasetRecord.fold_plan_hash
                    == LearningFoldResourceProfileRecord.fold_plan_hash,
                ),
            )
            .join(
                LearningSplitApprovalRecord,
                and_(
                    LearningSplitApprovalRecord.workspace_id
                    == NormalizedLearningDatasetRecord.workspace_id,
                    LearningSplitApprovalRecord.manifest_hash
                    == NormalizedLearningDatasetRecord.manifest_hash,
                    LearningSplitApprovalRecord.fold_plan_hash
                    == NormalizedLearningDatasetRecord.fold_plan_hash,
                    LearningSplitApprovalRecord.split_policy_version
                    == NormalizedLearningDatasetRecord.split_policy_version,
                    LearningSplitApprovalRecord.approval_hash
                    == NormalizedLearningDatasetRecord.approval_hash,
                ),
            )
            .where(
                LearningFoldResourceProfileRecord.workspace_id == workspace_id,
                LearningFoldResourceProfileRecord.normalized_dataset_id == normalized_dataset_id,
                LearningFoldResourceProfileRecord.fold_id == fold_id,
            )
        )
        row = result.one_or_none()
        if row is None:
            return None
        return LearningTrainingFoldOption(
            normalized_dataset_id=row.normalized_dataset_id,
            manifest_hash=row.manifest_hash,
            split_policy_version=row.split_policy_version,
            fold_plan_hash=row.fold_plan_hash,
            approval_hash=row.approval_hash,
            fold_id=row.fold_id,
            training_samples=row.training_samples,
            validation_samples=row.validation_samples,
            feature_count=row.feature_count,
            profile_hash=row.profile_hash,
        )

    async def next_trial_index(self, *, workspace_id: UUID, family_hash: str, fold_id: str) -> int:
        latest = await self._session.scalar(
            select(func.max(LearningTrainingAttemptRecord.trial_index)).where(
                LearningTrainingAttemptRecord.workspace_id == workspace_id,
                LearningTrainingAttemptRecord.parameter_family_hash == family_hash,
                LearningTrainingAttemptRecord.fold_id == fold_id,
            )
        )
        return 0 if latest is None else int(latest) + 1

    async def get_job_for_attempt(
        self, *, workspace_id: UUID, attempt_id: UUID
    ) -> LearningTrainingJobRecord | None:
        return await self._session.scalar(
            select(LearningTrainingJobRecord).where(
                LearningTrainingJobRecord.workspace_id == workspace_id,
                LearningTrainingJobRecord.attempt_id == attempt_id,
            )
        )

    async def cancel_job(
        self,
        *,
        workspace_id: UUID,
        job_id: UUID,
        actor_membership_id: UUID,
    ) -> LearningTrainingJobRecord | None:
        now: object = await self._session.scalar(select(func.clock_timestamp()))
        if not isinstance(now, datetime):
            raise RuntimeError("Không đọc được đồng hồ database để ghi yêu cầu hủy.")
        return await LearningTrainingJobRepository(self._session).request_cancel(
            workspace_id=workspace_id,
            job_id=job_id,
            requested_by=actor_membership_id,
            now=now,
        )

    async def get_job_snapshot(
        self, *, workspace_id: UUID, job_id: UUID
    ) -> LearningTrainingJobSnapshot | None:
        result = await self._session.execute(
            select(LearningTrainingJobRecord, LearningTrainingAttemptRecord)
            .join(
                LearningTrainingAttemptRecord,
                and_(
                    LearningTrainingAttemptRecord.workspace_id
                    == LearningTrainingJobRecord.workspace_id,
                    LearningTrainingAttemptRecord.id == LearningTrainingJobRecord.attempt_id,
                ),
            )
            .where(
                LearningTrainingJobRecord.workspace_id == workspace_id,
                LearningTrainingJobRecord.id == job_id,
            )
        )
        row = result.one_or_none()
        return _job_snapshot(*row) if row is not None else None

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )


def _job_snapshot(
    job: LearningTrainingJobRecord,
    attempt: LearningTrainingAttemptRecord,
) -> LearningTrainingJobSnapshot:
    return LearningTrainingJobSnapshot(
        job_id=job.id,
        attempt_id=attempt.id,
        normalized_dataset_id=attempt.normalized_dataset_id,
        manifest_hash=attempt.manifest_hash,
        fold_plan_hash=attempt.fold_plan_hash,
        fold_id=attempt.fold_id,
        status=job.status,
        stage=job.stage,
        progress=job.progress,
        attempt_count=job.attempt_count,
        lease_generation=job.lease_generation,
        cancel_requested=job.cancel_requested,
        error_code=job.error_code,
        training_samples=attempt.resource_request["training_samples"],
        validation_samples=attempt.resource_request["validation_samples"],
        feature_count=attempt.resource_request["feature_count"],
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


__all__ = [
    "LearningTrainingAdminRepository",
    "LearningTrainingFoldOption",
    "LearningTrainingJobSnapshot",
]
