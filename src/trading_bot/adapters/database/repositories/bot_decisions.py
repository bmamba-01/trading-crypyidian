"""Job/quyết định theo bot và store PostgreSQL cho fan-out shared evidence.

Evidence + outbox event ghi trong một transaction workspace (không bot scope).
Mỗi job/quyết định ghi trong transaction riêng đã bind đúng một bot qua account
(quy tắc một transaction một bot scope của MB-01); RLS chặn ghi chéo bot.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models.signal_entities import (
    BotDecisionJobRecord,
    BotDecisionRecord,
)
from trading_bot.adapters.database.repositories.signal_evidence import (
    SharedSignalEvidenceRepository,
)
from trading_bot.application.bot_dispatcher import (
    EVIDENCE_RECORDED_EVENT,
    BotDecision,
    BotDecisionConflict,
    BotDecisionJob,
    DecisionEvaluator,
    EvidenceRecordedEvent,
    EvidenceScopeError,
)
from trading_bot.application.outbox import OutboxRepository, UnitOfWork
from trading_bot.domain.events import EventEnvelope
from trading_bot.domain.market.signal_evidence import SharedSignalEvidence

_PENDING = "PENDING"
_COMPLETED = "COMPLETED"


class BotDecisionRepository:
    """Truy vấn job/quyết định luôn kèm workspace và bot; transaction do caller quản lý."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_job(self, job: BotDecisionJob) -> BotDecisionJob:
        """Ghi job mới; cùng key + cùng payload trả job cũ, khác payload là conflict."""
        existing = await self._find_job(job)
        if existing is not None:
            return _same_job(existing, job)
        row = BotDecisionJobRecord(
            id=job.job_id,
            workspace_id=job.workspace_id,
            bot_id=job.bot_id,
            account_id=job.account_id,
            evidence_id=job.evidence_id,
            evidence_hash=job.evidence_hash,
            bot_config_version_id=job.bot_config_version_id,
            evaluated_at=job.evaluated_at,
            context_hash=job.context.context_hash,
            context_payload=job.canonical_payload(),
            status=_PENDING,
            completed_at=None,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError as error:
            existing = await self._find_job(job)
            if existing is None:
                raise BotDecisionConflict(
                    "Không thể ghi job do xung đột ràng buộc khác."
                ) from error
            return _same_job(existing, job)
        return job

    async def lock_job(self, job: BotDecisionJob) -> BotDecisionJobRecord:
        """Khóa hàng job (FOR UPDATE) để chỉ một worker hoàn tất; kiểm payload đã ghim."""
        row = await self._session.scalar(
            select(BotDecisionJobRecord)
            .where(
                BotDecisionJobRecord.workspace_id == job.workspace_id,
                BotDecisionJobRecord.bot_id == job.bot_id,
                BotDecisionJobRecord.id == job.job_id,
            )
            .with_for_update()
        )
        if row is None:
            raise BotDecisionConflict("Không tìm thấy job trong scope bot hiện tại.")
        _same_job(row, job)
        return row

    async def pending_jobs(
        self, *, workspace_id: UUID, bot_id: UUID, account_id: UUID, limit: int
    ) -> tuple[BotDecisionJob, ...]:
        """Job còn chờ của một bot theo thứ tự thời điểm đánh giá."""
        if limit < 1:
            raise ValueError("limit phải lớn hơn 0.")
        rows = await self._session.scalars(
            select(BotDecisionJobRecord)
            .where(
                BotDecisionJobRecord.workspace_id == workspace_id,
                BotDecisionJobRecord.bot_id == bot_id,
                BotDecisionJobRecord.account_id == account_id,
                BotDecisionJobRecord.status == _PENDING,
            )
            .order_by(BotDecisionJobRecord.evaluated_at, BotDecisionJobRecord.id)
            .limit(limit)
        )
        return tuple(restore_job(row) for row in rows.all())

    async def decision_for_job(self, job: BotDecisionJob) -> BotDecision | None:
        """Quyết định đã lưu của job (hash và scope được xác minh)."""
        row = await self._session.scalar(
            select(BotDecisionRecord).where(
                BotDecisionRecord.workspace_id == job.workspace_id,
                BotDecisionRecord.bot_id == job.bot_id,
                BotDecisionRecord.job_id == job.job_id,
            )
        )
        if row is None:
            return None
        decision = restore_decision(row)
        if decision.idempotency_key != job.idempotency_key:
            raise BotDecisionConflict("Quyết định đã lưu không khớp idempotency key của job.")
        return decision

    async def insert_decision(self, decision: BotDecision) -> BotDecision:
        """Ghi quyết định bất biến; ghi lặp cùng hash trả bản cũ, khác hash là conflict."""
        existing = await self._session.scalar(
            select(BotDecisionRecord).where(
                BotDecisionRecord.workspace_id == decision.workspace_id,
                BotDecisionRecord.bot_id == decision.bot_id,
                BotDecisionRecord.idempotency_key == decision.idempotency_key,
            )
        )
        if existing is not None:
            stored = restore_decision(existing)
            if stored.decision_hash != decision.decision_hash:
                raise BotDecisionConflict("Cùng idempotency key nhưng quyết định khác.")
            return stored
        self._session.add(
            BotDecisionRecord(
                id=decision.decision_id,
                workspace_id=decision.workspace_id,
                bot_id=decision.bot_id,
                account_id=decision.account_id,
                job_id=decision.job_id,
                evidence_id=decision.evidence_id,
                evidence_hash=decision.evidence_hash,
                bot_config_version_id=decision.bot_config_version_id,
                bot_config_hash=decision.bot_config_hash,
                root_settings_version_id=decision.root_settings_version_id,
                root_settings_hash=decision.root_settings_hash,
                strategy_release_id=decision.strategy_release_id,
                strategy_release_hash=decision.strategy_release_hash,
                model_release_id=decision.model_release_id,
                evaluated_at=decision.evaluated_at,
                status=decision.status.value,
                reason_code=decision.reason_code,
                candidate_id=decision.candidate_id,
                idempotency_key=decision.idempotency_key,
                decision_hash=decision.decision_hash,
                payload=decision.canonical_payload(),
            )
        )
        await self._session.flush()
        return decision

    async def _find_job(self, job: BotDecisionJob) -> BotDecisionJobRecord | None:
        return await self._session.scalar(
            select(BotDecisionJobRecord).where(
                BotDecisionJobRecord.workspace_id == job.workspace_id,
                BotDecisionJobRecord.bot_id == job.bot_id,
                BotDecisionJobRecord.evidence_id == job.evidence_id,
                BotDecisionJobRecord.bot_config_version_id == job.bot_config_version_id,
            )
        )


class PostgresSignalDecisionStore:
    """Hiện thực `SignalDecisionStore` bằng UnitOfWork, RLS và outbox hiện có."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._clock = clock

    async def record_evidence(
        self, evidence: SharedSignalEvidence, *, evaluated_at: datetime
    ) -> bool:
        """Evidence + outbox event cùng transaction workspace; event chỉ ghi khi evidence mới."""
        event = EvidenceRecordedEvent.for_evidence(evidence, evaluated_at=evaluated_at)
        async with UnitOfWork(self._sessions, workspace_id=evidence.workspace_id) as uow:
            result = await SharedSignalEvidenceRepository(uow.session).append(evidence)
            if result.duplicate:
                return False
            uow.enqueue(
                EventEnvelope(
                    event_type=EVIDENCE_RECORDED_EVENT,
                    workspace_id=evidence.workspace_id,
                    aggregate_type="shared_signal_evidence",
                    aggregate_id=evidence.evidence_id,
                    occurred_at=evidence.available_at,
                    correlation_id=evidence.evidence_id,
                    idempotency_key=str(evidence.evidence_id),
                    schema_version=1,
                    payload=event.payload(),
                    event_id=event.event_id,
                )
            )
            return True

    async def load_evidence(
        self, *, workspace_id: UUID, evidence_id: UUID
    ) -> SharedSignalEvidence | None:
        async with UnitOfWork(self._sessions, workspace_id=workspace_id) as uow:
            return await SharedSignalEvidenceRepository(uow.session).get(
                workspace_id=workspace_id, evidence_id=evidence_id
            )

    async def enqueue_job(self, job: BotDecisionJob) -> BotDecisionJob:
        async with UnitOfWork(self._sessions, workspace_id=job.workspace_id) as uow:
            await _bind_job_bot(uow, job.account_id, job.bot_id)
            return await BotDecisionRepository(uow.session).insert_job(job)

    async def pending_jobs(
        self, *, workspace_id: UUID, account_id: UUID, limit: int
    ) -> tuple[BotDecisionJob, ...]:
        async with UnitOfWork(self._sessions, workspace_id=workspace_id) as uow:
            bot_id = await uow.bind_account(account_id)
            return await BotDecisionRepository(uow.session).pending_jobs(
                workspace_id=workspace_id, bot_id=bot_id, account_id=account_id, limit=limit
            )

    async def complete_job(self, job: BotDecisionJob, evaluate: DecisionEvaluator) -> BotDecision:
        """Khóa job, đánh giá đúng một lần, ghi quyết định và đánh dấu COMPLETED cùng commit."""
        async with UnitOfWork(self._sessions, workspace_id=job.workspace_id) as uow:
            await _bind_job_bot(uow, job.account_id, job.bot_id)
            repository = BotDecisionRepository(uow.session)
            record = await repository.lock_job(job)
            if record.status == _COMPLETED:
                stored = await repository.decision_for_job(job)
                if stored is None:
                    raise BotDecisionConflict("Job đã COMPLETED nhưng thiếu quyết định.")
                return stored
            evidence = await SharedSignalEvidenceRepository(uow.session).get(
                workspace_id=job.workspace_id, evidence_id=job.evidence_id
            )
            if evidence is None or evidence.evidence_hash != job.evidence_hash:
                raise EvidenceScopeError("Evidence của job không tồn tại hoặc sai hash.")
            decision = await evaluate(evidence, job)
            if decision.job_id != job.job_id or decision.idempotency_key != job.idempotency_key:
                raise BotDecisionConflict("Quyết định không thuộc job đang xử lý.")
            stored_decision = await repository.insert_decision(decision)
            record.status = _COMPLETED
            record.completed_at = self._clock()
            return stored_decision

    async def claim_evidence_events(
        self,
        *,
        workspace_id: UUID,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
        limit: int,
    ) -> tuple[EvidenceRecordedEvent, ...]:
        async with UnitOfWork(self._sessions, workspace_id=workspace_id) as uow:
            rows = await OutboxRepository(uow.session).claim_ready(
                workspace_id=workspace_id,
                worker_id=worker_id,
                now=now,
                lease_duration=lease_duration,
                max_attempts=max_attempts,
                limit=limit,
                event_types=(EVIDENCE_RECORDED_EVENT,),
            )
            return tuple(
                EvidenceRecordedEvent.from_payload(
                    event_id=row.id, workspace_id=row.workspace_id, payload=row.payload
                )
                for row in rows
            )

    async def acknowledge_event(
        self,
        *,
        workspace_id: UUID,
        event_id: UUID,
        worker_id: str,
        consumer_name: str,
        now: datetime,
    ) -> None:
        async with UnitOfWork(self._sessions, workspace_id=workspace_id) as uow:
            await OutboxRepository(uow.session).acknowledge(
                workspace_id=workspace_id,
                event_id=event_id,
                worker_id=worker_id,
                consumer_name=consumer_name,
                now=now,
            )

    async def record_event_failure(
        self,
        *,
        workspace_id: UUID,
        event_id: UUID,
        worker_id: str,
        now: datetime,
        error: BaseException,
        max_attempts: int,
    ) -> str:
        async with UnitOfWork(self._sessions, workspace_id=workspace_id) as uow:
            return await OutboxRepository(uow.session).record_failure(
                workspace_id=workspace_id,
                event_id=event_id,
                worker_id=worker_id,
                now=now,
                error=error,
                max_attempts=max_attempts,
            )


def restore_job(row: BotDecisionJobRecord) -> BotDecisionJob:
    """Dựng lại job từ payload đã ghim và đối chiếu cột quan hệ/hash context."""
    try:
        job = BotDecisionJob.from_payload(row.context_payload)
    except (KeyError, TypeError, ValueError) as error:
        raise BotDecisionConflict("Payload job đã lưu không hợp lệ.") from error
    stored = (
        row.id,
        row.workspace_id,
        row.bot_id,
        row.account_id,
        row.evidence_id,
        row.evidence_hash,
        row.bot_config_version_id,
        row.evaluated_at,
        row.context_hash,
    )
    restored = (
        job.job_id,
        job.workspace_id,
        job.bot_id,
        job.account_id,
        job.evidence_id,
        job.evidence_hash,
        job.bot_config_version_id,
        job.evaluated_at,
        job.context.context_hash,
    )
    if stored != restored:
        raise BotDecisionConflict("Cột job không khớp payload/context hash đã ghim.")
    return job


def restore_decision(row: BotDecisionRecord) -> BotDecision:
    """Dựng lại quyết định từ payload và xác minh hash cùng scope cột."""
    try:
        decision = BotDecision.from_payload(row.payload, row.decision_hash)
    except (KeyError, TypeError, ValueError) as error:
        raise BotDecisionConflict(
            "Payload quyết định đã lưu không hợp lệ hoặc sai hash."
        ) from error
    if (row.id, row.workspace_id, row.bot_id, row.account_id, row.job_id) != (
        decision.decision_id,
        decision.workspace_id,
        decision.bot_id,
        decision.account_id,
        decision.job_id,
    ):
        raise BotDecisionConflict("Cột quyết định không khớp payload đã băm.")
    return decision


def _same_job(row: BotDecisionJobRecord, job: BotDecisionJob) -> BotDecisionJob:
    stored = restore_job(row)
    if stored != job:
        raise BotDecisionConflict("Job cùng key nhưng khác evidence/context/thời điểm đánh giá.")
    return stored


async def _bind_job_bot(uow: UnitOfWork, account_id: UUID, bot_id: UUID) -> None:
    """Resolve bot từ account bằng mapping DB; context không được tự khai bot khác."""
    resolved = await uow.bind_account(account_id)
    if resolved != bot_id:
        raise EvidenceScopeError("Account của job không thuộc bot đã khai trong context.")
