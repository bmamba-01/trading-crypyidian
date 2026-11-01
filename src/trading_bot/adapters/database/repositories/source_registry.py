"""Persistence tenant-scoped cho policy registry và quota research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import SourcePolicyRevisionRecord, SourceQuotaEventRecord
from trading_bot.domain.research.source_policy import (
    SourcePolicyState,
    SourceRightsPolicy,
    SourceUsePurpose,
)
from trading_bot.domain.research.source_registry import (
    SourceBudget,
    SourcePolicyRevision,
    SourceQuotaExceeded,
    SourceRegistryEntry,
    SourceUsage,
    normalize_source_cost,
)


class SourceRegistryConflict(RuntimeError):
    """Từ chối policy sequence hoặc idempotency key có payload khác."""


class SourceQuotaExceededError(RuntimeError):
    """Reservation bị từ chối trước connector khi quota/cost đã hết."""


@dataclass(frozen=True, slots=True)
class SourceRegistryAppendResult:
    """Kết quả append policy; retry đồng nhất trả revision đã lưu."""

    revision: SourcePolicyRevision
    is_duplicate: bool


@dataclass(frozen=True, slots=True)
class SourceQuotaReservationResult:
    """Reservation quota bất biến; duplicate không được gọi lại connector."""

    event_id: UUID
    window_started_at: datetime
    policy_sequence: int
    is_duplicate: bool


class SourceRegistryRepository:
    """Mọi truy vấn nhận workspace tường minh và giữ transaction của caller."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_revision(
        self, *, workspace_id: UUID, revision: SourcePolicyRevision
    ) -> SourceRegistryAppendResult:
        """Append policy revision sau khóa workspace/source và kiểm tra history hiện tại."""
        await self._bind_workspace(workspace_id)
        await self._lock_source(workspace_id, revision.policy.source_id)
        duplicate = await self._session.scalar(
            select(SourcePolicyRevisionRecord).where(
                SourcePolicyRevisionRecord.workspace_id == workspace_id,
                SourcePolicyRevisionRecord.policy_id == revision.policy.policy_id,
            )
        )
        if duplicate is not None:
            stored = _to_domain_revision(duplicate)
            if stored == revision:
                return SourceRegistryAppendResult(stored, True)
            raise SourceRegistryConflict("idempotency policy_id đã có payload khác.")

        rows = await self._policy_rows(
            workspace_id=workspace_id, source_id=revision.policy.source_id
        )
        history = tuple(_to_domain_revision(row) for row in rows)
        expected_sequence = len(history) + 1
        if revision.sequence != expected_sequence:
            raise SourceRegistryConflict(
                f"sequence policy phải liên tiếp; cần {expected_sequence}."
            )
        SourceRegistryEntry(revision.policy.source_id, (*history, revision))
        self._session.add(SourcePolicyRevisionRecord.from_domain(workspace_id, revision))
        await self._session.flush()
        return SourceRegistryAppendResult(revision, False)

    async def get_entry(self, *, workspace_id: UUID, source_id: str) -> SourceRegistryEntry | None:
        """Đọc toàn bộ lịch sử nguồn trong đúng workspace theo thứ tự revision."""
        await self._bind_workspace(workspace_id)
        rows = await self._policy_rows(workspace_id=workspace_id, source_id=source_id)
        if not rows:
            return None
        return SourceRegistryEntry(
            source_id,
            tuple(_to_domain_revision(row) for row in rows),
        )

    async def reserve_quota(
        self,
        *,
        workspace_id: UUID,
        source_id: str,
        idempotency_key: str,
        purpose: SourceUsePurpose,
        reserved_bytes: int,
        reserved_cost: Decimal | str,
    ) -> SourceQuotaReservationResult:
        """Ghi reservation trước network; khóa transaction ngăn race vượt budget.

        Số byte/chi phí là mức reserve ước lượng hoặc giới hạn caller khai báo, không phải mức
        thực tế đã phát sinh. Reservation append-only không được hoàn lại khi fetch lỗi/hụt.
        Caller gặp `is_duplicate=True` không được gọi lại connector cho cùng key.
        """
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id không được để trống.")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key không được để trống.")
        if not idempotency_key.strip() or len(idempotency_key) > 240:
            raise ValueError("idempotency_key phải có từ 1 đến 240 ký tự.")
        if type(purpose) is not SourceUsePurpose:
            raise ValueError("purpose phải là SourceUsePurpose.")
        if type(reserved_bytes) is not int or reserved_bytes < 0:
            raise ValueError("reserved_bytes phải là số nguyên không âm.")
        normalized_cost = normalize_source_cost(reserved_cost, "reserved_cost")
        await self._bind_workspace(workspace_id)
        await self._lock_source(workspace_id, source_id)

        duplicate = await self._session.scalar(
            select(SourceQuotaEventRecord).where(
                SourceQuotaEventRecord.workspace_id == workspace_id,
                SourceQuotaEventRecord.source_id == source_id,
                SourceQuotaEventRecord.idempotency_key == idempotency_key,
            )
        )
        if duplicate is not None:
            if (
                duplicate.purpose != purpose.value
                or duplicate.reserved_bytes != reserved_bytes
                or duplicate.reserved_cost != normalized_cost
            ):
                raise SourceRegistryConflict("idempotency quota key đã có reservation khác.")
            return SourceQuotaReservationResult(
                duplicate.id, duplicate.window_started_at, duplicate.policy_sequence, True
            )

        now = await self._session.scalar(text("SELECT clock_timestamp()"))
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("database clock_timestamp() không trả thời gian có múi giờ.")
        now = now.astimezone(UTC)

        policy_rows = await self._policy_rows(workspace_id=workspace_id, source_id=source_id)
        if not policy_rows:
            raise PermissionError("source policy không tồn tại hoặc chưa được duyệt.")
        entry = SourceRegistryEntry(
            source_id,
            tuple(_to_domain_revision(row) for row in policy_rows),
        )
        current = entry.current
        if not entry.permits(purpose, at=now):
            raise PermissionError(
                "source policy không cho phép mục đích tại thời điểm reservation."
            )

        events = await self._quota_events(workspace_id=workspace_id, source_id=source_id)
        usage, window_started_at = _current_usage(events, at=now, budget=current.budget)
        try:
            next_usage = current.budget.consume(
                usage,
                at=now,
                byte_count=reserved_bytes,
                cost=normalized_cost,
            )
        except SourceQuotaExceeded as error:
            raise SourceQuotaExceededError(str(error)) from error

        event = SourceQuotaEventRecord(
            workspace_id=workspace_id,
            source_id=source_id,
            policy_sequence=current.sequence,
            idempotency_key=idempotency_key,
            purpose=purpose.value,
            window_started_at=next_usage.window_started_at,
            occurred_at=now,
            reserved_requests=1,
            reserved_bytes=reserved_bytes,
            reserved_cost=normalized_cost,
            cost_unit=current.budget.cost_unit,
        )
        self._session.add(event)
        await self._session.flush()
        return SourceQuotaReservationResult(event.id, window_started_at, current.sequence, False)

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )

    async def _lock_source(self, workspace_id: UUID, source_id: str) -> None:
        """Serialize policy sequence and quota accounting for one workspace/source."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"research-source:{workspace_id}:{source_id}"},
        )

    async def _policy_rows(
        self, *, workspace_id: UUID, source_id: str
    ) -> list[SourcePolicyRevisionRecord]:
        result = await self._session.scalars(
            select(SourcePolicyRevisionRecord)
            .where(
                SourcePolicyRevisionRecord.workspace_id == workspace_id,
                SourcePolicyRevisionRecord.source_id == source_id,
            )
            .order_by(SourcePolicyRevisionRecord.sequence.asc())
        )
        return list(result.all())

    async def _quota_events(
        self, *, workspace_id: UUID, source_id: str
    ) -> list[SourceQuotaEventRecord]:
        result = await self._session.scalars(
            select(SourceQuotaEventRecord)
            .where(
                SourceQuotaEventRecord.workspace_id == workspace_id,
                SourceQuotaEventRecord.source_id == source_id,
            )
            .order_by(SourceQuotaEventRecord.occurred_at.asc())
        )
        return list(result.all())


def _to_domain_revision(row: SourcePolicyRevisionRecord) -> SourcePolicyRevision:
    policy = SourceRightsPolicy(
        policy_id=row.policy_id,
        source_id=row.source_id,
        state=SourcePolicyState(row.state),
        allowed_purposes=frozenset(SourceUsePurpose(item) for item in row.allowed_purposes),
        evidence_ref=row.evidence_ref,
        reviewed_at=row.reviewed_at,
        valid_until=row.valid_until,
    )
    return SourcePolicyRevision(
        sequence=row.sequence,
        policy=policy,
        publisher=row.publisher,
        endpoint_url=row.endpoint_url,
        access_method=row.access_method,
        auth_scope=row.auth_scope,
        attribution=row.attribution,
        retention_ref=row.retention_ref,
        budget=SourceBudget(
            window=timedelta(seconds=row.quota_window_seconds),
            max_requests=row.quota_max_requests,
            max_bytes=row.quota_max_bytes,
            max_cost=row.quota_max_cost,
            cost_unit=row.quota_cost_unit,
        ),
        created_at=row.created_at,
        actor_id=row.actor_id,
        reason=row.reason,
        approved_by=row.approved_by,
    )


def _current_usage(
    events: list[SourceQuotaEventRecord], *, at: datetime, budget: SourceBudget
) -> tuple[SourceUsage | None, datetime]:
    """Tính usage dưới transaction lock; fail closed nếu event nằm sau database clock."""
    instant = at.astimezone(UTC)
    if not events:
        return None, instant
    latest = events[-1]
    if instant < latest.occurred_at:
        raise ValueError("database clock is before the latest quota event; reservation denied.")
    if instant - latest.window_started_at >= budget.window:
        return None, instant
    active = [event for event in events if event.window_started_at == latest.window_started_at]
    if latest.cost_unit != budget.cost_unit:
        raise ValueError("cost_unit changed within the active quota window; reservation denied.")
    usage = SourceUsage(
        window_started_at=latest.window_started_at,
        requests_used=sum(event.reserved_requests for event in active),
        bytes_used=sum(event.reserved_bytes for event in active),
        cost_used=sum((event.reserved_cost for event in active), Decimal(0)),
        cost_unit=latest.cost_unit,
    )
    return usage, latest.window_started_at
