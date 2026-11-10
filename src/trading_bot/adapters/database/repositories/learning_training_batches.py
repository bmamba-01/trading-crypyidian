"""Cấp batch train/validation qua gateway gắn với attempt đã được cấp."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    LearningAttemptEventRecord,
    LearningTrainingAttemptRecord,
)
from trading_bot.adapters.database.repositories.learning_attempts import (
    LearningAttemptConflict,
    LearningAttemptRepository,
)
from trading_bot.application.learning_training_batch import (
    AuthorizedTrainerBatch,
    authorized_training_batch_from_rows,
)
from trading_bot.domain.learning.accounting import (
    LearningAttemptActor,
    LearningAttemptEvent,
    LearningAttemptState,
)


class LearningTrainingBatchAccessConflict(RuntimeError):
    """Attempt không đủ điều kiện hoặc batch đã được cấp trước đó."""


class LearningTrainingBatchRepository:
    """Khóa attempt, nối trạng thái running và gọi gateway một lần trong UoW."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_batch(
        self,
        *,
        workspace_id: UUID,
        attempt_id: UUID,
    ) -> AuthorizedTrainerBatch:
        """Ghi event running/receipt cùng snapshot trước khi application trả payload."""
        if type(workspace_id) is not UUID or type(attempt_id) is not UUID:
            raise ValueError("Workspace và attempt phải là UUID.")
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:attempt_id, 0))"),
            {"attempt_id": str(attempt_id)},
        )
        record = await self._session.scalar(
            select(LearningTrainingAttemptRecord).where(
                LearningTrainingAttemptRecord.workspace_id == workspace_id,
                LearningTrainingAttemptRecord.id == attempt_id,
            )
        )
        if record is None:
            raise LearningTrainingBatchAccessConflict("Attempt không tồn tại trong workspace.")
        attempt = record.to_domain()
        if record.initial_state != LearningAttemptState.QUEUED.value:
            raise LearningTrainingBatchAccessConflict("Attempt không ở trạng thái queued.")

        latest_event = await self._session.scalar(
            select(LearningAttemptEventRecord)
            .where(
                LearningAttemptEventRecord.workspace_id == workspace_id,
                LearningAttemptEventRecord.attempt_id == attempt_id,
            )
            .order_by(LearningAttemptEventRecord.sequence.desc())
            .limit(1)
        )
        if latest_event is None:
            raise LearningTrainingBatchAccessConflict("Attempt thiếu event trạng thái ban đầu.")
        previous = latest_event.to_domain()
        if previous.state is not LearningAttemptState.QUEUED:
            raise LearningTrainingBatchAccessConflict("Attempt không còn ở trạng thái queued.")

        await LearningAttemptRepository(self._session).append_event(
            workspace_id=workspace_id,
            event=LearningAttemptEvent(
                attempt_id=attempt_id,
                sequence=previous.sequence + 1,
                state=LearningAttemptState.RUNNING,
                occurred_at=datetime.now(UTC),
                actor_principal=LearningAttemptActor.TRAINER_WORKER,
            ),
        )
        try:
            result = await self._session.execute(
                text(
                    "SELECT * FROM public.wp06_claim_learning_training_batch("
                    "target_workspace => CAST(:workspace_id AS uuid), "
                    "target_attempt_id => CAST(:attempt_id AS uuid))"
                ),
                {"workspace_id": str(workspace_id), "attempt_id": str(attempt_id)},
            )
            rows = tuple(result.all())
            lineage_result = await self._session.execute(
                text(
                    "SELECT * FROM public.wp06_claim_learning_training_lineage("
                    "target_workspace => CAST(:workspace_id AS uuid), "
                    "target_attempt_id => CAST(:attempt_id AS uuid))"
                ),
                {"workspace_id": str(workspace_id), "attempt_id": str(attempt_id)},
            )
            lineage_rows = tuple(lineage_result.all())
        except DBAPIError as error:
            message = str(error.orig).upper()
            failure_code = next(
                (
                    token
                    for token in (
                        "WP06_TRAINING_BATCH_CALLER_DENIED",
                        "WP06_TRAINING_BATCH_WORKSPACE_DENIED",
                        "WP06_TRAINING_BATCH_ATTEMPT_NOT_FOUND",
                        "WP06_TRAINING_BATCH_ATTEMPT_NOT_RUNNING",
                        "WP06_TRAINING_BATCH_ALREADY_CONSUMED",
                        "WP06_TRAINING_BATCH_SOURCE_RIGHTS_FAIL_CLOSED",
                        "WP06_SOURCE_TRAINING_RIGHTS_CALLER_DENIED",
                        "WP06_SOURCE_TRAINING_RIGHTS_WORKSPACE_DENIED",
                        "WP06_SOURCE_TRAINING_RIGHTS_SCOPE_INVALID",
                        "WP06_SOURCE_TRAINING_RIGHTS_MANIFEST_MISSING",
                        "WP06_SOURCE_TRAINING_RIGHTS_LINEAGE_INVALID",
                        "WP06_SOURCE_TRAINING_RIGHTS_BUILDER_RECEIPT_MISMATCH",
                        "WP06_SOURCE_TRAINING_RIGHTS_INVALIDATED",
                        "WP06_SOURCE_TRAINING_RIGHTS_OBSERVATION_MISSING",
                        "WP06_SOURCE_TRAINING_RIGHTS_POLICY_DENIED",
                        "WP06_SOURCE_TRAINING_RIGHTS_ITEM_DENIED",
                        "WP06_TRAINING_BATCH_APPROVAL_MISMATCH",
                        "WP06_TRAINING_BATCH_DATASET_MISMATCH",
                        "WP06_TRAINING_BATCH_PROFILE_MISMATCH",
                        "WP06_TRAINING_BATCH_COUNTS_INVALID",
                        "WP06_TRAINING_LINEAGE_CALLER_DENIED",
                        "WP06_TRAINING_LINEAGE_WORKSPACE_DENIED",
                        "WP06_TRAINING_LINEAGE_RECEIPT_REQUIRED",
                    )
                    if token in message
                ),
                None,
            )
            if failure_code is not None:
                raise LearningTrainingBatchAccessConflict(
                    f"Không thể cấp batch trainer (mã {failure_code})."
                ) from None
            raise
        try:
            return authorized_training_batch_from_rows(
                attempt=attempt,
                normalized_dataset_id=record.normalized_dataset_id,
                rows=rows,
                lineage_rows=lineage_rows,
            )
        except (TypeError, ValueError) as error:
            raise LearningAttemptConflict(
                "Batch trainer không khớp attempt hoặc sample hash authoritative."
            ) from error


__all__ = [
    "LearningTrainingBatchAccessConflict",
    "LearningTrainingBatchRepository",
]
