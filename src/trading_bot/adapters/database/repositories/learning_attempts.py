"""Repository append-only cho attempt learning và các event đo lường."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    LearningAttemptEventRecord,
    LearningFoldResourceProfileRecord,
    LearningSplitApprovalRecord,
    LearningTrainingAttemptRecord,
)
from trading_bot.domain.learning.accounting import (
    LearningAttemptActor,
    LearningAttemptEvent,
    LearningAttemptReasonCode,
    LearningAttemptState,
    LearningTrainingAttempt,
    validate_learning_attempt_transition,
)
from trading_bot.domain.learning.resources import (
    DEFAULT_LEARNING_RESOURCE_LIMITS,
    LearningResourceLimits,
    LearningResourceRejected,
    assess_learning_resource_request,
)


class LearningAttemptConflict(RuntimeError):
    """Attempt, idempotency key, approval hoặc DB-derived profile bị lệch."""


@dataclass(frozen=True, slots=True)
class LearningAttemptWriteResult:
    """Kết quả append lần đầu hoặc retry cùng canonical request."""

    attempt: LearningTrainingAttempt
    is_duplicate: bool


class LearningAttemptRepository:
    """Admin tạo attempt; trainer chỉ có thể nối lifecycle event."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        resource_limits: LearningResourceLimits = DEFAULT_LEARNING_RESOURCE_LIMITS,
    ) -> None:
        if type(resource_limits) is not LearningResourceLimits:
            raise ValueError("resource_limits phải do service tin cậy cung cấp.")
        self._session = session
        self._resource_limits = resource_limits

    async def append_attempt(
        self,
        *,
        attempt: LearningTrainingAttempt,
        normalized_dataset_id: UUID,
    ) -> LearningAttemptWriteResult:
        """Kiểm approval/profile authoritative rồi append attempt và event đầu."""
        if type(attempt) is not LearningTrainingAttempt or type(normalized_dataset_id) is not UUID:
            raise ValueError("Cần LearningTrainingAttempt và normalized dataset UUID typed.")
        await self._bind_workspace(attempt.workspace_id)
        lock_keys = sorted(
            (
                f"learning-attempt-idempotency:{attempt.workspace_id}:{attempt.idempotency_key}",
                "learning-attempt-trial:"
                f"{attempt.workspace_id}:{attempt.parameter_family_hash}:"
                f"{attempt.trial_index}:{attempt.fold_id}",
            )
        )
        for lock_key in lock_keys:
            await self._session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )
        await self._verify_approval_and_profile(attempt, normalized_dataset_id)
        self._verify_resource_budget(attempt)

        existing = await self._session.scalar(
            select(LearningTrainingAttemptRecord).where(
                LearningTrainingAttemptRecord.workspace_id == attempt.workspace_id,
                LearningTrainingAttemptRecord.idempotency_key == attempt.idempotency_key,
            )
        )
        if existing is not None:
            stored = existing.to_domain()
            if (
                stored.canonical_json != attempt.canonical_json
                or existing.normalized_dataset_id != normalized_dataset_id
            ):
                raise LearningAttemptConflict(
                    "Idempotency key đã gắn attempt hoặc normalized dataset khác."
                )
            return LearningAttemptWriteResult(stored, True)

        existing_trial = await self._session.scalar(
            select(LearningTrainingAttemptRecord.id).where(
                LearningTrainingAttemptRecord.workspace_id == attempt.workspace_id,
                LearningTrainingAttemptRecord.parameter_family_hash
                == attempt.parameter_family_hash,
                LearningTrainingAttemptRecord.trial_index == attempt.trial_index,
                LearningTrainingAttemptRecord.fold_id == attempt.fold_id,
            )
        )
        if existing_trial is not None:
            raise LearningAttemptConflict(
                "Trial/fold đã được cấp một attempt khác trong parameter family."
            )

        record = LearningTrainingAttemptRecord.from_domain(
            attempt, normalized_dataset_id=normalized_dataset_id
        )
        self._session.add(record)
        await self._session.flush()
        first_event = LearningAttemptEvent(
            attempt_id=attempt.id,
            sequence=1,
            state=attempt.initial_state,
            occurred_at=datetime.now(UTC),
            actor_principal=LearningAttemptActor.ADMIN_WEB,
            reason_code=attempt.rejection_reason_code,
            peak_memory_bytes=0 if attempt.initial_state is LearningAttemptState.REJECTED else None,
            actual_operation_units=(
                0 if attempt.initial_state is LearningAttemptState.REJECTED else None
            ),
        )
        self._session.add(
            LearningAttemptEventRecord.from_domain(
                workspace_id=attempt.workspace_id,
                event=first_event,
            )
        )
        await self._session.flush()
        if attempt.initial_state is LearningAttemptState.QUEUED:
            from trading_bot.adapters.database.repositories.learning_training_jobs import (
                LearningTrainingJobRepository,
            )

            await LearningTrainingJobRepository(self._session).enqueue(
                workspace_id=attempt.workspace_id,
                attempt_id=attempt.id,
            )
        return LearningAttemptWriteResult(attempt, False)

    async def append_event(
        self,
        *,
        workspace_id: UUID,
        event: LearningAttemptEvent,
    ) -> LearningAttemptEvent:
        """Nối event nếu đúng transition; DB trigger khóa cạnh tranh và kiểm actor."""
        if type(workspace_id) is not UUID or type(event) is not LearningAttemptEvent:
            raise ValueError("Cần workspace UUID và LearningAttemptEvent typed.")
        await self._bind_workspace(workspace_id)
        attempt = await self._session.scalar(
            select(LearningTrainingAttemptRecord).where(
                LearningTrainingAttemptRecord.workspace_id == workspace_id,
                LearningTrainingAttemptRecord.id == event.attempt_id,
            )
        )
        if attempt is None:
            raise LearningAttemptConflict("Attempt không tồn tại trong workspace.")
        previous_row = await self._session.scalar(
            select(LearningAttemptEventRecord)
            .where(
                LearningAttemptEventRecord.workspace_id == workspace_id,
                LearningAttemptEventRecord.attempt_id == event.attempt_id,
            )
            .order_by(LearningAttemptEventRecord.sequence.desc())
            .limit(1)
        )
        previous = previous_row.to_domain() if previous_row is not None else None
        validate_learning_attempt_transition(previous, event)
        row = LearningAttemptEventRecord.from_domain(workspace_id=workspace_id, event=event)
        self._session.add(row)
        await self._session.flush()
        return event

    async def _verify_approval_and_profile(
        self,
        attempt: LearningTrainingAttempt,
        normalized_dataset_id: UUID,
    ) -> None:
        approval = await self._session.scalar(
            select(LearningSplitApprovalRecord).where(
                LearningSplitApprovalRecord.workspace_id == attempt.workspace_id,
                LearningSplitApprovalRecord.manifest_hash == attempt.manifest_hash,
                LearningSplitApprovalRecord.split_policy_version == attempt.split_policy_version,
                LearningSplitApprovalRecord.fold_plan_hash == attempt.fold_plan_hash,
            )
        )
        if approval is None or approval.approval_hash != attempt.approval_hash:
            raise LearningAttemptConflict("Approval hiện hành không khớp attempt.")
        profile = await self._session.scalar(
            select(LearningFoldResourceProfileRecord).where(
                LearningFoldResourceProfileRecord.workspace_id == attempt.workspace_id,
                LearningFoldResourceProfileRecord.normalized_dataset_id == normalized_dataset_id,
                LearningFoldResourceProfileRecord.manifest_hash == attempt.manifest_hash,
                LearningFoldResourceProfileRecord.fold_plan_hash == attempt.fold_plan_hash,
                LearningFoldResourceProfileRecord.fold_id == attempt.fold_id,
            )
        )
        if profile is None:
            raise LearningAttemptConflict("Fold resource profile authoritative không tồn tại.")
        if (
            attempt.resource_request.training_samples != profile.training_samples
            or attempt.resource_request.validation_samples != profile.validation_samples
            or attempt.resource_request.feature_count != profile.feature_count
        ):
            raise LearningAttemptConflict("Counts attempt không khớp profile derived từ DB rows.")

    def _verify_resource_budget(self, attempt: LearningTrainingAttempt) -> None:
        try:
            expected = assess_learning_resource_request(
                attempt.resource_request,
                limits=self._resource_limits,
            )
        except LearningResourceRejected:
            if (
                attempt.resource_plan is not None
                or attempt.rejection_reason_code is not LearningAttemptReasonCode.RESOURCE_LIMIT
            ):
                raise LearningAttemptConflict(
                    "Yêu cầu vượt ngân sách nhưng attempt không ghi nhận RESOURCE_LIMIT."
                ) from None
        else:
            if attempt.resource_plan != expected or attempt.rejection_reason_code is not None:
                raise LearningAttemptConflict(
                    "Resource plan không được tính từ policy máy chủ hiện hành."
                )

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )


__all__ = [
    "LearningAttemptConflict",
    "LearningAttemptRepository",
    "LearningAttemptWriteResult",
]
