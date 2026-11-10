"""Worker một attempt; batch/receipt, CPU process và persist terminal state."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import (
    LearningAttemptEventRecord,
    LearningTrainingArtifactRecord,
)
from trading_bot.adapters.database.repositories.learning_attempts import (
    LearningAttemptRepository,
)
from trading_bot.adapters.database.repositories.learning_scorer_artifacts import (
    LearningScorerArtifactRepository,
)
from trading_bot.adapters.database.repositories.learning_training_artifacts import (
    LearningTrainingArtifactRepository,
)
from trading_bot.adapters.database.repositories.learning_training_batches import (
    LearningTrainingBatchRepository,
)
from trading_bot.adapters.database.repositories.learning_training_jobs import (
    DEFAULT_LEARNING_TRAINING_JOB_LEASE,
    LearningTrainingJobConflict,
    LearningTrainingJobRepository,
)
from trading_bot.application.learning_scorer_artifact import (
    VerifiedLearningScorerArtifact,
)
from trading_bot.application.learning_training_artifact import (
    VerifiedLearningTrainingArtifact,
)
from trading_bot.application.learning_training_batch import AuthorizedTrainerBatch
from trading_bot.application.learning_training_input import (
    LearningTrainingInputRequest,
    LearningTrainingInputService,
)
from trading_bot.application.learning_training_process import (
    LearningCpuTrainingProcessRunner,
    LearningTrainingProcessFailure,
    LearningTrainingProcessFailureCode,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.learning.accounting import (
    LearningAttemptActor,
    LearningAttemptEvent,
    LearningAttemptReasonCode,
    LearningAttemptState,
)
from trading_bot.domain.learning.jobs import LearningTrainingJobClaim


class LearningTrainingWorkerFailure(RuntimeError):
    """Lỗi đã ghi attempt failed với reason code nội bộ đã chuẩn hóa."""

    def __init__(self, reason_code: LearningAttemptReasonCode) -> None:
        self.reason_code = reason_code
        super().__init__(f"Learning trainer thất bại ({reason_code.value}).")


@dataclass(frozen=True, slots=True)
class LearningTrainingWorkerResult:
    """Read model an toàn cho CLI/job supervisor sau khi attempt terminal."""

    attempt_id: UUID
    artifact_id: UUID
    artifact_hash: str
    duplicate: bool
    elapsed_milliseconds: int
    cpu_time_milliseconds: int
    peak_memory_bytes: int
    actual_operation_units: int


@dataclass(frozen=True, slots=True)
class LearningTrainingJobRunResult:
    """Kết quả xử lý một job; `idle` nghĩa là queue hiện không có việc."""

    status: str
    job_id: UUID | None
    attempt_id: UUID | None
    training: LearningTrainingWorkerResult | None
    recovered_jobs: int
    cancelled_jobs: int


class LearningTrainingAttemptWorker:
    """Chạy batch đã claim, rồi atomic persist artifact và terminal event."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        process_runner: LearningCpuTrainingProcessRunner | None = None,
    ) -> None:
        self._sessions = sessions
        self._process_runner = process_runner or LearningCpuTrainingProcessRunner()

    async def run_once(
        self,
        *,
        workspace_id: UUID,
        attempt_id: UUID,
    ) -> LearningTrainingWorkerResult:
        """Xử lý một attempt do dispatcher tin cậy chỉ định; không scan tenant toàn cục."""
        if type(workspace_id) is not UUID or type(attempt_id) is not UUID:
            raise ValueError("workspace_id và attempt_id phải là UUID.")
        existing = await self._existing_result(workspace_id, attempt_id)
        if existing is not None:
            return existing

        batch = await LearningTrainingInputService(self._sessions).claim_batch(
            LearningTrainingInputRequest(workspace_id=workspace_id, attempt_id=attempt_id)
        )
        started_at = time.monotonic()
        try:
            process_result = await asyncio.to_thread(self._process_runner.train, batch)
            verified_artifact = VerifiedLearningTrainingArtifact.from_process_result(
                batch, process_result
            )
            verified_scorer_artifact = VerifiedLearningScorerArtifact.from_process_result(
                batch, process_result
            )
        except LearningTrainingProcessFailure as error:
            reason = _failure_reason(error.code)
            await self._append_failed(
                workspace_id=workspace_id,
                attempt_id=attempt_id,
                reason_code=reason,
                elapsed_milliseconds=max(0, round((time.monotonic() - started_at) * 1000)),
            )
            raise LearningTrainingWorkerFailure(reason) from None
        except Exception:
            reason = LearningAttemptReasonCode.ADAPTER_ERROR
            await self._append_failed(
                workspace_id=workspace_id,
                attempt_id=attempt_id,
                reason_code=reason,
                elapsed_milliseconds=max(0, round((time.monotonic() - started_at) * 1000)),
            )
            raise LearningTrainingWorkerFailure(reason) from None

        async with UnitOfWork(self._sessions, workspace_id=workspace_id) as uow:
            write = await LearningTrainingArtifactRepository(uow.session).append_success(
                workspace_id=workspace_id,
                artifact=verified_artifact,
            )
            await LearningScorerArtifactRepository(uow.session).append_success(
                workspace_id=workspace_id,
                artifact=verified_scorer_artifact,
            )
        return _result_from_artifact(write.record, duplicate=write.is_duplicate)

    async def work_once(
        self,
        *,
        workspace_id: UUID,
        worker_id: str,
    ) -> LearningTrainingJobRunResult:
        """Phục hồi lease cũ, claim tối đa một job và chạy dưới lease heartbeat."""
        if type(workspace_id) is not UUID or workspace_id.int == 0:
            raise ValueError("workspace_id phải là UUID khác zero.")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id là bắt buộc.")
        now = datetime.now(UTC)
        async with UnitOfWork(self._sessions, workspace_id=workspace_id) as uow:
            jobs = LearningTrainingJobRepository(uow.session)
            cancelled = await jobs.cancel_requested_queued(
                workspace_id=workspace_id,
                now=now,
            )
            recovered = await jobs.reclaim_expired(workspace_id=workspace_id, now=now)
            claim = await jobs.claim_next(
                workspace_id=workspace_id,
                worker_id=worker_id,
                now=now,
            )
        if claim is None:
            return LearningTrainingJobRunResult(
                status="cancelled" if cancelled else "idle",
                job_id=None,
                attempt_id=None,
                training=None,
                recovered_jobs=recovered,
                cancelled_jobs=cancelled,
            )

        batch = await self._claim_job_batch(claim)
        if batch is None:
            return LearningTrainingJobRunResult(
                status="cancelled",
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                training=None,
                recovered_jobs=recovered,
                cancelled_jobs=cancelled,
            )

        cancel_signal = Event()
        stop_monitor = asyncio.Event()
        monitor = asyncio.create_task(
            self._monitor_job(claim, cancel_signal, stop_monitor),
            name=f"learning-training-lease-{claim.job_id}",
        )
        started_at = time.monotonic()
        try:
            process_result = await asyncio.to_thread(
                self._process_runner.train,
                batch,
                cancel_event=cancel_signal,
            )
            verified_artifact = VerifiedLearningTrainingArtifact.from_process_result(
                batch, process_result
            )
            verified_scorer_artifact = VerifiedLearningScorerArtifact.from_process_result(
                batch, process_result
            )
        except LearningTrainingProcessFailure as error:
            reason = _failure_reason(error.code)
            if error.code is LearningTrainingProcessFailureCode.CANCELLED:
                was_cancelled = await self._finish_cancelled_job(claim)
                if was_cancelled:
                    return LearningTrainingJobRunResult(
                        status="cancelled",
                        job_id=claim.job_id,
                        attempt_id=claim.attempt_id,
                        training=None,
                        recovered_jobs=recovered,
                        cancelled_jobs=cancelled,
                    )
            await self._finish_failed_job(
                claim,
                reason_code=reason,
                elapsed_milliseconds=max(0, round((time.monotonic() - started_at) * 1000)),
            )
            raise LearningTrainingWorkerFailure(reason) from None
        except Exception:
            reason = LearningAttemptReasonCode.ADAPTER_ERROR
            await self._finish_failed_job(
                claim,
                reason_code=reason,
                elapsed_milliseconds=max(0, round((time.monotonic() - started_at) * 1000)),
            )
            raise LearningTrainingWorkerFailure(reason) from None
        finally:
            stop_monitor.set()
            await monitor

        async with UnitOfWork(self._sessions, workspace_id=workspace_id) as uow:
            jobs = LearningTrainingJobRepository(uow.session)
            row = await jobs.lock_for_completion(claim, now=datetime.now(UTC))
            if row.cancel_requested:
                await jobs.cancel_if_requested(claim, now=datetime.now(UTC))
                return LearningTrainingJobRunResult(
                    status="cancelled",
                    job_id=claim.job_id,
                    attempt_id=claim.attempt_id,
                    training=None,
                    recovered_jobs=recovered,
                    cancelled_jobs=cancelled,
                )
            write = await LearningTrainingArtifactRepository(uow.session).append_success(
                workspace_id=workspace_id,
                artifact=verified_artifact,
            )
            await LearningScorerArtifactRepository(uow.session).append_success(
                workspace_id=workspace_id,
                artifact=verified_scorer_artifact,
            )
            await jobs.finish(row=row, status="succeeded", now=datetime.now(UTC))
        return LearningTrainingJobRunResult(
            status="succeeded",
            job_id=claim.job_id,
            attempt_id=claim.attempt_id,
            training=_result_from_artifact(write.record, duplicate=write.is_duplicate),
            recovered_jobs=recovered,
            cancelled_jobs=cancelled,
        )

    async def _claim_job_batch(
        self,
        claim: LearningTrainingJobClaim,
    ) -> AuthorizedTrainerBatch | None:
        """Kiểm cancellation dưới row lock rồi commit receipt trước khi trả batch."""
        batch = None
        async with UnitOfWork(self._sessions, workspace_id=claim.workspace_id) as uow:
            jobs = LearningTrainingJobRepository(uow.session)
            row = await jobs.lock_for_completion(claim, now=datetime.now(UTC))
            if row.cancel_requested:
                await jobs.cancel_if_requested(claim, now=datetime.now(UTC))
            else:
                batch = await LearningTrainingBatchRepository(uow.session).claim_batch(
                    workspace_id=claim.workspace_id,
                    attempt_id=claim.attempt_id,
                )
        return batch

    async def _monitor_job(
        self,
        claim: LearningTrainingJobClaim,
        cancel_signal: Event,
        stop_monitor: asyncio.Event,
    ) -> None:
        """Gia hạn lease và phản ánh cancellation vào process runner tối đa sau một giây."""
        while not stop_monitor.is_set():
            try:
                await asyncio.wait_for(stop_monitor.wait(), timeout=1.0)
                return
            except TimeoutError:
                pass
            try:
                async with UnitOfWork(self._sessions, workspace_id=claim.workspace_id) as uow:
                    cancel_requested = await LearningTrainingJobRepository(uow.session).heartbeat(
                        claim,
                        now=datetime.now(UTC),
                        lease_duration=DEFAULT_LEARNING_TRAINING_JOB_LEASE,
                    )
            except LearningTrainingJobConflict:
                cancel_signal.set()
                return
            except Exception:
                cancel_signal.set()
                return
            if cancel_requested:
                cancel_signal.set()
                return

    async def _finish_cancelled_job(self, claim: LearningTrainingJobClaim) -> bool:
        async with UnitOfWork(self._sessions, workspace_id=claim.workspace_id) as uow:
            return await LearningTrainingJobRepository(uow.session).cancel_if_requested(
                claim,
                now=datetime.now(UTC),
            )

    async def _finish_failed_job(
        self,
        claim: LearningTrainingJobClaim,
        *,
        reason_code: LearningAttemptReasonCode,
        elapsed_milliseconds: int,
    ) -> None:
        async with UnitOfWork(self._sessions, workspace_id=claim.workspace_id) as uow:
            jobs = LearningTrainingJobRepository(uow.session)
            row = await jobs.lock_for_completion(claim, now=datetime.now(UTC))
            latest = await uow.session.scalar(
                select(LearningAttemptEventRecord)
                .where(
                    LearningAttemptEventRecord.workspace_id == claim.workspace_id,
                    LearningAttemptEventRecord.attempt_id == claim.attempt_id,
                )
                .order_by(LearningAttemptEventRecord.sequence.desc())
                .limit(1)
            )
            if latest is None or latest.state not in {
                LearningAttemptState.QUEUED.value,
                LearningAttemptState.RUNNING.value,
            }:
                raise LearningTrainingJobConflict("Attempt không thể hoàn tất failed.")
            await LearningAttemptRepository(uow.session).append_event(
                workspace_id=claim.workspace_id,
                event=LearningAttemptEvent(
                    attempt_id=claim.attempt_id,
                    sequence=latest.sequence + 1,
                    state=LearningAttemptState.FAILED,
                    occurred_at=datetime.now(UTC),
                    actor_principal=LearningAttemptActor.TRAINER_WORKER,
                    reason_code=reason_code,
                    elapsed_milliseconds=elapsed_milliseconds,
                ),
            )
            await jobs.finish(
                row=row,
                status="failed",
                now=datetime.now(UTC),
                error_code=f"TRAINER_{reason_code.value.upper()}",
            )

    async def _existing_result(
        self,
        workspace_id: UUID,
        attempt_id: UUID,
    ) -> LearningTrainingWorkerResult | None:
        async with UnitOfWork(self._sessions, workspace_id=workspace_id) as uow:
            record = await LearningTrainingArtifactRepository(uow.session).get_by_attempt(
                workspace_id=workspace_id,
                attempt_id=attempt_id,
            )
        return _result_from_artifact(record, duplicate=True) if record is not None else None

    async def _append_failed(
        self,
        *,
        workspace_id: UUID,
        attempt_id: UUID,
        reason_code: LearningAttemptReasonCode,
        elapsed_milliseconds: int,
    ) -> None:
        async with UnitOfWork(self._sessions, workspace_id=workspace_id) as uow:
            latest = await uow.session.scalar(
                select(LearningAttemptEventRecord)
                .where(
                    LearningAttemptEventRecord.workspace_id == workspace_id,
                    LearningAttemptEventRecord.attempt_id == attempt_id,
                )
                .order_by(LearningAttemptEventRecord.sequence.desc())
                .limit(1)
            )
            if latest is None or latest.state != LearningAttemptState.RUNNING.value:
                return
            await LearningAttemptRepository(uow.session).append_event(
                workspace_id=workspace_id,
                event=LearningAttemptEvent(
                    attempt_id=attempt_id,
                    sequence=latest.sequence + 1,
                    state=LearningAttemptState.FAILED,
                    occurred_at=datetime.now(UTC),
                    actor_principal=LearningAttemptActor.TRAINER_WORKER,
                    reason_code=reason_code,
                    elapsed_milliseconds=elapsed_milliseconds,
                ),
            )


def _failure_reason(code: LearningTrainingProcessFailureCode) -> LearningAttemptReasonCode:
    if code in {
        LearningTrainingProcessFailureCode.CPU_LIMIT,
        LearningTrainingProcessFailureCode.MEMORY_LIMIT,
        LearningTrainingProcessFailureCode.RUNTIME_LIMIT,
        LearningTrainingProcessFailureCode.RESOURCE_LIMIT_UNAVAILABLE,
    }:
        return LearningAttemptReasonCode.RESOURCE_LIMIT
    return LearningAttemptReasonCode.ADAPTER_ERROR


def _result_from_artifact(
    record: object,
    *,
    duplicate: bool,
) -> LearningTrainingWorkerResult:
    if type(record) is not LearningTrainingArtifactRecord:
        raise ValueError("Artifact trainer record không hợp lệ.")
    return LearningTrainingWorkerResult(
        attempt_id=record.attempt_id,
        artifact_id=record.id,
        artifact_hash=record.artifact_hash,
        duplicate=duplicate,
        elapsed_milliseconds=record.elapsed_milliseconds,
        cpu_time_milliseconds=record.cpu_time_milliseconds,
        peak_memory_bytes=record.peak_memory_bytes,
        actual_operation_units=record.actual_operation_units,
    )


__all__ = [
    "LearningTrainingAttemptWorker",
    "LearningTrainingWorkerFailure",
    "LearningTrainingWorkerResult",
]
