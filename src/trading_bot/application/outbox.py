from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Self
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import AuditEvent, ConsumerReceipt, OutboxEvent
from trading_bot.domain.events import EventEnvelope, state_hash


class AfterCommitCallbackError(RuntimeError):
    """Báo callback RAM thất bại sau khi dữ liệu đã commit bền vững."""


def retry_delay_seconds(attempt: int, *, base_seconds: int, max_seconds: int) -> int:
    """Tính khoảng chờ exponential backoff có giới hạn trước lần giao tiếp tiếp theo."""
    if attempt < 1 or base_seconds < 1 or max_seconds < base_seconds:
        raise ValueError("Số lần thử và ngưỡng backoff không hợp lệ")
    if attempt - 1 >= (max_seconds // base_seconds).bit_length():
        return max_seconds
    return min(base_seconds << (attempt - 1), max_seconds)


class UnitOfWork:
    """Gom thay đổi nghiệp vụ, audit và outbox trong một transaction."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        workspace_id: UUID,
        actor_membership_id: UUID | None = None,
        request_id: UUID | None = None,
    ) -> None:
        self._sessions = sessions
        self.workspace_id = workspace_id
        self.actor_membership_id = actor_membership_id
        self.request_id = request_id or uuid4()
        self._session: AsyncSession | None = None
        self._after_commit: list[Callable[[], None]] = []

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("UnitOfWork chỉ dùng được trong async with")
        return self._session

    async def __aenter__(self) -> Self:
        self._session = self._sessions()
        self._after_commit.clear()
        try:
            await self._session.begin()
            await self._session.execute(
                text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(self.workspace_id)},
            )
        except BaseException:
            await self._session.close()
            self._session = None
            raise
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        session = self.session
        committed = False
        try:
            if exc_type is None:
                await session.commit()
                committed = True
                callbacks = tuple(self._after_commit)
                self._after_commit.clear()
                for callback in callbacks:
                    try:
                        callback()
                    except Exception:
                        raise AfterCommitCallbackError(
                            "Callback công bố state thất bại sau commit"
                        ) from None
            else:
                await session.rollback()
        except BaseException:
            if not committed and exc_type is None:
                await session.rollback()
            raise
        finally:
            self._after_commit.clear()
            await session.close()
            self._session = None

    def after_commit(self, callback: Callable[[], None]) -> None:
        """Đăng ký cập nhật state RAM chỉ sau khi transaction DB đã commit."""
        if self._session is None:
            raise RuntimeError("Callback commit chỉ đăng ký được trong async with")
        self._after_commit.append(callback)

    def enqueue(self, envelope: EventEnvelope) -> None:
        if envelope.workspace_id != self.workspace_id:
            raise ValueError("Sự kiện phải thuộc workspace của UnitOfWork")
        self.session.add(
            OutboxEvent(
                id=envelope.event_id,
                workspace_id=envelope.workspace_id,
                event_type=envelope.event_type,
                aggregate_type=envelope.aggregate_type,
                aggregate_id=envelope.aggregate_id,
                occurred_at=envelope.occurred_at,
                correlation_id=envelope.correlation_id,
                idempotency_key=envelope.idempotency_key,
                schema_version=envelope.schema_version,
                payload=dict(envelope.payload),
            )
        )

    def record_audit(
        self,
        *,
        action: str,
        target_type: str,
        target_id: UUID | None = None,
        reason: str | None = None,
        before_state: object | None = None,
        after_state: object | None = None,
    ) -> None:
        self.session.add(
            AuditEvent(
                workspace_id=self.workspace_id,
                actor_membership_id=self.actor_membership_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                reason=reason,
                before_hash=state_hash(before_state) if before_state is not None else None,
                after_hash=state_hash(after_state) if after_state is not None else None,
                request_id=self.request_id,
                occurred_at=datetime.now(UTC),
            )
        )


class OutboxRepository:
    """Claim và hoàn tất event trong transaction ngắn, ngoài transaction mới gọi API."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_ready(
        self,
        *,
        workspace_id: UUID,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
        limit: int = 20,
    ) -> list[OutboxEvent]:
        _validate_claim_options(now, lease_duration, max_attempts, limit, worker_id)
        rows = await self._session.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.workspace_id == workspace_id,
                OutboxEvent.status.in_(("PENDING", "PROCESSING")),
                OutboxEvent.available_at <= now,
                OutboxEvent.attempts < max_attempts,
                (OutboxEvent.lease_expires_at.is_(None)) | (OutboxEvent.lease_expires_at <= now),
            )
            .order_by(OutboxEvent.available_at, OutboxEvent.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        events = list(rows.all())
        for event in events:
            event.status = "PROCESSING"
            event.attempts += 1
            event.lease_owner = worker_id
            event.lease_expires_at = now + lease_duration
        return events

    async def acknowledge(
        self,
        *,
        workspace_id: UUID,
        event_id: UUID,
        worker_id: str,
        consumer_name: str,
        now: datetime,
    ) -> bool:
        _validate_time(now)
        event = await self._locked_event(workspace_id, event_id)
        _validate_active_lease(event, worker_id, now)
        receipt = await self._session.scalar(
            select(ConsumerReceipt).where(
                ConsumerReceipt.workspace_id == workspace_id,
                ConsumerReceipt.outbox_event_id == event_id,
                ConsumerReceipt.consumer_name == consumer_name,
            )
        )
        created = receipt is None
        if created:
            self._session.add(
                ConsumerReceipt(
                    workspace_id=workspace_id,
                    outbox_event_id=event_id,
                    consumer_name=consumer_name,
                    processed_at=now,
                )
            )
        event.status = "DELIVERED"
        event.completed_at = now
        event.lease_owner = None
        event.lease_expires_at = None
        event.last_error_code = None
        return created

    async def record_failure(
        self,
        *,
        workspace_id: UUID,
        event_id: UUID,
        worker_id: str,
        now: datetime,
        error: BaseException,
        max_attempts: int,
        base_delay_seconds: int = 2,
        max_delay_seconds: int = 300,
    ) -> str:
        _validate_time(now)
        if max_attempts < 1:
            raise ValueError("Số lần thử tối đa phải lớn hơn 0")
        event = await self._locked_event(workspace_id, event_id)
        _validate_active_lease(event, worker_id, now)
        event.last_error_code = type(error).__name__[:120]
        event.lease_owner = None
        event.lease_expires_at = None
        if event.attempts >= max_attempts:
            event.status = "DEAD"
            return event.status
        delay = retry_delay_seconds(
            event.attempts,
            base_seconds=base_delay_seconds,
            max_seconds=max_delay_seconds,
        )
        event.status = "PENDING"
        event.available_at = now + timedelta(seconds=delay)
        return event.status

    async def dead_letter_exhausted(
        self, *, workspace_id: UUID, now: datetime, max_attempts: int, limit: int = 100
    ) -> int:
        _validate_time(now)
        if max_attempts < 1 or limit < 1:
            raise ValueError("Số lần thử tối đa và batch size phải lớn hơn 0")
        rows = await self._session.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.workspace_id == workspace_id,
                OutboxEvent.status.in_(("PENDING", "PROCESSING")),
                OutboxEvent.attempts >= max_attempts,
                (OutboxEvent.lease_expires_at.is_(None)) | (OutboxEvent.lease_expires_at <= now),
            )
            .order_by(OutboxEvent.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        events = list(rows.all())
        for event in events:
            event.status = "DEAD"
            event.last_error_code = event.last_error_code or "max_attempts_exceeded"
            event.lease_owner = None
            event.lease_expires_at = None
        return len(events)

    async def _locked_event(self, workspace_id: UUID, event_id: UUID) -> OutboxEvent:
        event = await self._session.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.workspace_id == workspace_id,
                OutboxEvent.id == event_id,
            )
            .with_for_update()
        )
        if event is None:
            raise LookupError("Không tìm thấy outbox event trong workspace")
        return event


def _validate_claim_options(
    now: datetime,
    lease_duration: timedelta,
    max_attempts: int,
    limit: int,
    worker_id: str,
) -> None:
    _validate_time(now)
    if (
        lease_duration.total_seconds() <= 0
        or max_attempts < 1
        or limit < 1
        or not worker_id.strip()
    ):
        raise ValueError("Thông số claim outbox không hợp lệ")


def _validate_active_lease(event: OutboxEvent, worker_id: str, now: datetime) -> None:
    if (
        event.status != "PROCESSING"
        or event.lease_owner != worker_id
        or event.lease_expires_at is None
        or event.lease_expires_at <= now
    ):
        raise RuntimeError("Lease outbox không còn hiệu lực cho worker này")


def _validate_time(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Thời điểm outbox phải có timezone")
