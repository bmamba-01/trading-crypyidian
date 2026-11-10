"""Claim, lease và recovery cho hàng đợi trainer theo workspace."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    LearningAttemptEventRecord,
    LearningTrainingArtifactRecord,
    LearningTrainingAttemptRecord,
    LearningTrainingJobRecord,
)
from trading_bot.adapters.database.repositories.learning_attempts import LearningAttemptRepository
from trading_bot.domain.learning.accounting import (
    LearningAttemptActor,
    LearningAttemptEvent,
    LearningAttemptReasonCode,
    LearningAttemptState,
)
from trading_bot.domain.learning.jobs import (
    LearningTrainingJobClaim,
    LearningTrainingJobRecoveryAction,
    recover_learning_training_job,
)

MAX_LEARNING_TRAINING_JOB_ATTEMPTS = 3
DEFAULT_LEARNING_TRAINING_JOB_LEASE = timedelta(seconds=360)


class LearningTrainingJobConflict(RuntimeError):
    """Job hoặc lease không còn ở trạng thái authoritative."""


class LearningTrainingJobRepository:
    """DB queue dùng row lock để cấp một generation duy nhất cho worker."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        *,
        workspace_id: UUID,
        attempt_id: UUID,
    ) -> tuple[LearningTrainingJobRecord, bool]:
        """Tạo job đúng một lần cho attempt đã có event queued."""
        if type(workspace_id) is not UUID or type(attempt_id) is not UUID:
            raise ValueError("Cần workspace/attempt UUID.")
        attempt = await self._session.scalar(
            select(LearningTrainingAttemptRecord).where(
                LearningTrainingAttemptRecord.workspace_id == workspace_id,
                LearningTrainingAttemptRecord.id == attempt_id,
            )
        )
        if attempt is None:
            raise LearningTrainingJobConflict("Attempt trainer không tồn tại trong workspace.")
        latest = await self._latest_event(workspace_id=workspace_id, attempt_id=attempt_id)
        if latest is None or latest.state != LearningAttemptState.QUEUED.value:
            raise LearningTrainingJobConflict("Chỉ attempt queued mới được đưa vào hàng đợi.")
        existing = await self._session.scalar(
            select(LearningTrainingJobRecord).where(
                LearningTrainingJobRecord.workspace_id == workspace_id,
                LearningTrainingJobRecord.attempt_id == attempt_id,
            )
        )
        if existing is not None:
            return existing, True
        record = LearningTrainingJobRecord(
            id=uuid4(),
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            status="queued",
            stage="queued",
            progress=0,
            attempt_count=0,
            lease_generation=0,
            cancel_requested=False,
        )
        self._session.add(record)
        await self._session.flush()
        return record, False

    async def cancel_requested_queued(
        self,
        *,
        workspace_id: UUID,
        now: datetime,
    ) -> int:
        """Chốt cancellation đã ghi cho queued jobs trước khi claim job mới."""
        if type(workspace_id) is not UUID or not isinstance(now, datetime):
            raise ValueError("Workspace UUID và thời điểm hợp lệ là bắt buộc.")
        rows = (
            await self._session.scalars(
                select(LearningTrainingJobRecord)
                .where(
                    LearningTrainingJobRecord.workspace_id == workspace_id,
                    LearningTrainingJobRecord.status == "queued",
                    LearningTrainingJobRecord.cancel_requested.is_(True),
                )
                .order_by(LearningTrainingJobRecord.created_at, LearningTrainingJobRecord.id)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for row in rows:
            latest = await self._latest_event(
                workspace_id=workspace_id,
                attempt_id=row.attempt_id,
            )
            if latest is None or latest.state != LearningAttemptState.QUEUED.value:
                raise LearningTrainingJobConflict("Job queued không khớp attempt event queued.")
            await self._append_terminal_event(
                row,
                previous=latest,
                state=LearningAttemptState.CANCELLED,
                reason=LearningAttemptReasonCode.CANCELLED_BY_OPERATOR,
                now=now,
            )
            row.status = "cancelled"
            row.stage = "cancelled"
            row.progress = 100
            row.updated_at = now
        await self._session.flush()
        return len(rows)

    async def claim_next(
        self,
        *,
        workspace_id: UUID,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta = DEFAULT_LEARNING_TRAINING_JOB_LEASE,
    ) -> LearningTrainingJobClaim | None:
        """Claim một job queued bằng `SKIP LOCKED`; claim commit tách khỏi fit."""
        _validate_claim_inputs(workspace_id, worker_id, now, lease_duration)
        while True:
            row = await self._session.scalar(
                select(LearningTrainingJobRecord)
                .where(
                    LearningTrainingJobRecord.workspace_id == workspace_id,
                    LearningTrainingJobRecord.status == "queued",
                )
                .order_by(LearningTrainingJobRecord.created_at, LearningTrainingJobRecord.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            latest = await self._latest_event(workspace_id=workspace_id, attempt_id=row.attempt_id)
            if latest is None or latest.state != LearningAttemptState.QUEUED.value:
                raise LearningTrainingJobConflict("Job queued không khớp attempt event queued.")
            if row.cancel_requested:
                await self._append_terminal_event(
                    row,
                    previous=latest,
                    state=LearningAttemptState.CANCELLED,
                    reason=LearningAttemptReasonCode.CANCELLED_BY_OPERATOR,
                    now=now,
                )
                row.status = "cancelled"
                row.stage = "cancelled"
                row.progress = 100
                row.updated_at = now
                await self._session.flush()
                continue
            if row.attempt_count >= MAX_LEARNING_TRAINING_JOB_ATTEMPTS:
                await self._append_terminal_event(
                    row,
                    previous=latest,
                    state=LearningAttemptState.FAILED,
                    reason=LearningAttemptReasonCode.ADAPTER_ERROR,
                    now=now,
                )
                row.status = "failed"
                row.stage = "failed"
                row.progress = 100
                row.error_code = "TRAINER_ATTEMPTS_EXHAUSTED"
                row.updated_at = now
                await self._session.flush()
                continue
            row.status = "running"
            row.stage = "training"
            row.progress = max(row.progress, 5)
            row.attempt_count += 1
            row.lease_generation += 1
            row.lease_owner = worker_id
            row.lease_until = now + lease_duration
            row.updated_at = now
            await self._session.flush()
            return LearningTrainingJobClaim(
                job_id=row.id,
                workspace_id=row.workspace_id,
                attempt_id=row.attempt_id,
                worker_id=worker_id,
                generation=row.lease_generation,
                lease_until=row.lease_until,
            )

    async def reclaim_expired(
        self,
        *,
        workspace_id: UUID,
        now: datetime,
    ) -> int:
        """Requeue only pre-receipt leases; post-receipt crash fails that attempt."""
        if type(workspace_id) is not UUID or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Workspace UUID và thời điểm có timezone là bắt buộc.")
        rows = (
            await self._session.scalars(
                select(LearningTrainingJobRecord)
                .where(
                    LearningTrainingJobRecord.workspace_id == workspace_id,
                    LearningTrainingJobRecord.status == "running",
                    LearningTrainingJobRecord.lease_until <= now,
                )
                .order_by(LearningTrainingJobRecord.created_at, LearningTrainingJobRecord.id)
                .with_for_update(skip_locked=True)
            )
        ).all()
        recovered = 0
        for row in rows:
            latest = await self._latest_event(
                workspace_id=workspace_id,
                attempt_id=row.attempt_id,
            )
            if latest is None:
                raise LearningTrainingJobConflict("Job lease không có attempt event.")
            artifact = await self._session.scalar(
                select(LearningTrainingArtifactRecord).where(
                    LearningTrainingArtifactRecord.workspace_id == workspace_id,
                    LearningTrainingArtifactRecord.attempt_id == row.attempt_id,
                )
            )
            action = recover_learning_training_job(
                has_receipt=latest.state != LearningAttemptState.QUEUED.value,
                has_artifact=artifact is not None,
            )
            if action is LearningTrainingJobRecoveryAction.ALREADY_SUCCEEDED:
                if latest.state != LearningAttemptState.SUCCEEDED.value:
                    raise LearningTrainingJobConflict("Artifact có nhưng attempt chưa succeeded.")
                row.status = "succeeded"
                row.stage = "complete"
                row.progress = 100
                row.lease_owner = None
                row.lease_until = None
                row.updated_at = now
                recovered += 1
                continue
            if action is LearningTrainingJobRecoveryAction.REQUEUE:
                if latest.state != LearningAttemptState.QUEUED.value:
                    raise LearningTrainingJobConflict("Không thể requeue attempt đã bắt đầu.")
                if row.cancel_requested:
                    await self._append_terminal_event(
                        row,
                        previous=latest,
                        state=LearningAttemptState.CANCELLED,
                        reason=LearningAttemptReasonCode.CANCELLED_BY_OPERATOR,
                        now=now,
                    )
                    row.status = "cancelled"
                    row.stage = "cancelled"
                    row.progress = 100
                elif row.attempt_count < MAX_LEARNING_TRAINING_JOB_ATTEMPTS:
                    row.status = "queued"
                    row.stage = "queued"
                    row.progress = 0
                else:
                    await self._append_terminal_event(
                        row,
                        previous=latest,
                        state=LearningAttemptState.FAILED,
                        reason=LearningAttemptReasonCode.ADAPTER_ERROR,
                        now=now,
                    )
                    row.status = "failed"
                    row.stage = "failed"
                    row.progress = 100
                    row.error_code = "TRAINER_ATTEMPTS_EXHAUSTED"
                row.lease_owner = None
                row.lease_until = None
                row.updated_at = now
                recovered += 1
                continue
            if latest.state != LearningAttemptState.RUNNING.value:
                raise LearningTrainingJobConflict("Receipt đã dùng nhưng attempt không running.")
            await self._append_terminal_event(
                row,
                previous=latest,
                state=LearningAttemptState.FAILED,
                reason=LearningAttemptReasonCode.ADAPTER_ERROR,
                now=now,
            )
            row.status = "failed"
            row.stage = "failed"
            row.progress = 100
            row.lease_owner = None
            row.lease_until = None
            row.error_code = "TRAINER_LEASE_EXPIRED_AFTER_RECEIPT"
            row.updated_at = now
            recovered += 1
        await self._session.flush()
        return recovered

    async def lock_for_completion(
        self,
        claim: LearningTrainingJobClaim,
        *,
        now: datetime,
    ) -> LearningTrainingJobRecord:
        """Khóa job trước terminal persist và kiểm fence/cancellation atomically."""
        if type(claim) is not LearningTrainingJobClaim:
            raise ValueError("Cần LearningTrainingJobClaim.")
        row = await self._session.scalar(
            select(LearningTrainingJobRecord)
            .where(
                LearningTrainingJobRecord.workspace_id == claim.workspace_id,
                LearningTrainingJobRecord.id == claim.job_id,
            )
            .with_for_update()
        )
        if (
            row is None
            or row.status != "running"
            or row.lease_owner != claim.worker_id
            or row.lease_generation != claim.generation
            or row.lease_until is None
            or row.lease_until <= now
        ):
            raise LearningTrainingJobConflict("Trainer lease đã hết hoặc bị generation mới fence.")
        return row

    async def heartbeat(
        self,
        claim: LearningTrainingJobClaim,
        *,
        now: datetime,
        lease_duration: timedelta = DEFAULT_LEARNING_TRAINING_JOB_LEASE,
    ) -> bool:
        """Gia hạn lease hiện hành và trả trạng thái yêu cầu hủy đã lưu."""
        _validate_claim_inputs(claim.workspace_id, claim.worker_id, now, lease_duration)
        row = await self.lock_for_completion(claim, now=now)
        row.lease_until = now + lease_duration
        row.updated_at = now
        await self._session.flush()
        return row.cancel_requested

    async def cancel_if_requested(
        self,
        claim: LearningTrainingJobClaim,
        *,
        now: datetime,
    ) -> bool:
        """Chốt cancellation khi worker đã dừng child hoặc chưa cấp batch."""
        row = await self.lock_for_completion(claim, now=now)
        if not row.cancel_requested:
            return False
        latest = await self._latest_event(
            workspace_id=row.workspace_id,
            attempt_id=row.attempt_id,
        )
        if latest is None or latest.state not in {
            LearningAttemptState.QUEUED.value,
            LearningAttemptState.RUNNING.value,
        }:
            raise LearningTrainingJobConflict("Attempt không thể hủy ở trạng thái hiện hành.")
        await self._append_terminal_event(
            row,
            previous=latest,
            state=LearningAttemptState.CANCELLED,
            reason=LearningAttemptReasonCode.CANCELLED_BY_OPERATOR,
            now=now,
        )
        await self.finish(row=row, status="cancelled", now=now)
        return True

    async def finish(
        self,
        *,
        row: LearningTrainingJobRecord,
        status: str,
        now: datetime,
        error_code: str | None = None,
    ) -> None:
        """Ghi trạng thái cuối; attempt event phải được flush trước lời gọi này."""
        if type(row) is not LearningTrainingJobRecord or status not in {
            "succeeded",
            "failed",
            "cancelled",
        }:
            raise ValueError("Job hoặc terminal status không hợp lệ.")
        latest = await self._latest_event(
            workspace_id=row.workspace_id,
            attempt_id=row.attempt_id,
        )
        if latest is None or latest.state != status:
            raise LearningTrainingJobConflict("Job terminal cần attempt event tương ứng.")
        row.status = status
        row.stage = "complete" if status == "succeeded" else status
        row.progress = 100
        row.lease_owner = None
        row.lease_until = None
        row.error_code = error_code
        row.updated_at = now
        await self._session.flush()

    async def request_cancel(
        self,
        *,
        workspace_id: UUID,
        job_id: UUID,
        requested_by: UUID,
        now: datetime,
    ) -> LearningTrainingJobRecord | None:
        """Ghi danh tính operator; worker mới có quyền chốt trạng thái cancelled."""
        if any(type(value) is not UUID for value in (workspace_id, job_id, requested_by)):
            raise ValueError("Workspace, job và operator membership phải là UUID.")
        row = await self._session.scalar(
            select(LearningTrainingJobRecord)
            .where(
                LearningTrainingJobRecord.workspace_id == workspace_id,
                LearningTrainingJobRecord.id == job_id,
            )
            .with_for_update()
        )
        if row is None or row.status not in {"queued", "running"}:
            return row
        if not row.cancel_requested:
            row.cancel_requested = True
            row.cancel_requested_at = now
            row.cancel_requested_by = requested_by
            row.updated_at = now
            await self._session.flush()
        return row

    async def get_by_attempt(
        self,
        *,
        workspace_id: UUID,
        attempt_id: UUID,
    ) -> LearningTrainingJobRecord | None:
        return await self._session.scalar(
            select(LearningTrainingJobRecord).where(
                LearningTrainingJobRecord.workspace_id == workspace_id,
                LearningTrainingJobRecord.attempt_id == attempt_id,
            )
        )

    async def _latest_event(
        self,
        *,
        workspace_id: UUID,
        attempt_id: UUID,
    ) -> LearningAttemptEventRecord | None:
        return await self._session.scalar(
            select(LearningAttemptEventRecord)
            .where(
                LearningAttemptEventRecord.workspace_id == workspace_id,
                LearningAttemptEventRecord.attempt_id == attempt_id,
            )
            .order_by(LearningAttemptEventRecord.sequence.desc())
            .limit(1)
        )

    async def _append_terminal_event(
        self,
        row: LearningTrainingJobRecord,
        *,
        previous: LearningAttemptEventRecord,
        state: LearningAttemptState,
        reason: LearningAttemptReasonCode,
        now: datetime,
    ) -> None:
        elapsed = max(0, round((now - previous.occurred_at).total_seconds() * 1000))
        await LearningAttemptRepository(self._session).append_event(
            workspace_id=row.workspace_id,
            event=LearningAttemptEvent(
                attempt_id=row.attempt_id,
                sequence=previous.sequence + 1,
                state=state,
                occurred_at=now,
                actor_principal=LearningAttemptActor.TRAINER_WORKER,
                reason_code=reason,
                elapsed_milliseconds=elapsed,
            ),
        )


def _validate_claim_inputs(
    workspace_id: UUID,
    worker_id: str,
    now: datetime,
    lease_duration: timedelta,
) -> None:
    if type(workspace_id) is not UUID or workspace_id.int == 0:
        raise ValueError("Cần workspace UUID khác zero.")
    if not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 120:
        raise ValueError("worker_id phải có từ 1 tới 120 ký tự.")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now phải có múi giờ.")
    if not isinstance(lease_duration, timedelta) or not timedelta(seconds=30) <= lease_duration:
        raise ValueError("Lease trainer phải dài ít nhất 30 giây.")
    if lease_duration > timedelta(seconds=600):
        raise ValueError("Lease trainer không được vượt quá 600 giây.")


__all__ = [
    "DEFAULT_LEARNING_TRAINING_JOB_LEASE",
    "MAX_LEARNING_TRAINING_JOB_ATTEMPTS",
    "LearningTrainingJobConflict",
    "LearningTrainingJobRepository",
]
