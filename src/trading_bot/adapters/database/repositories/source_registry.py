"""Persistence tenant-scoped cho policy registry và quota research."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    SourcePolicyObservationRecord,
    SourcePolicyRevisionRecord,
    SourceQuotaEventRecord,
    UserMembership,
)
from trading_bot.domain.research.source_policy import (
    SourcePolicyState,
    SourceRightsPolicy,
    SourceUsePurpose,
)
from trading_bot.domain.research.source_registry import (
    ObservedSourcePolicy,
    SourceBudget,
    SourcePolicyRevision,
    SourceQuotaExceeded,
    SourceQuotaSnapshot,
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
        policy_id = revision.policy.policy_id
        match = re.fullmatch(r"policy:([0-9a-f]{32}):[0-9a-f]{64}", policy_id)
        if match is not None:
            key = UUID(hex=match.group(1))
            await self._session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"research-policy-key:{workspace_id}:{key.hex}"},
            )
            matches = await self.find_revisions_by_idempotency_key(
                workspace_id=workspace_id, key=key
            )
            if len(matches) > 1 or any(
                stored.policy.policy_id != policy_id
                or stored.policy.source_id != revision.policy.source_id
                for stored in matches
            ):
                raise SourceRegistryConflict("idempotency key đã có revision khác trong workspace.")
        await self._lock_source(workspace_id, revision.policy.source_id)
        duplicate = await self._session.scalar(
            select(SourcePolicyRevisionRecord).where(
                SourcePolicyRevisionRecord.workspace_id == workspace_id,
                SourcePolicyRevisionRecord.policy_id == revision.policy.policy_id,
            )
        )
        if duplicate is not None:
            stored = _to_domain_revision(duplicate)
            if _same_revision_payload(stored, revision):
                return SourceRegistryAppendResult(stored, True)
            raise SourceRegistryConflict("idempotency policy_id đã có payload khác.")

        now = await self._session.scalar(text("SELECT clock_timestamp()"))
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("database clock_timestamp() không trả thời gian có múi giờ.")
        now = now.astimezone(UTC)
        policy = revision.policy
        if policy.state is SourcePolicyState.VERIFIED:
            if policy.valid_until is not None and policy.valid_until <= now:
                raise SourceRegistryConflict("valid_until phải sau database clock.")
        if policy.reviewed_at is not None:
            policy = replace(policy, reviewed_at=now)
        canonical_revision = replace(revision, policy=policy, created_at=now)

        rows = await self._policy_rows(
            workspace_id=workspace_id, source_id=canonical_revision.policy.source_id
        )
        history = tuple(_to_domain_revision(row) for row in rows)
        expected_sequence = len(history) + 1
        if canonical_revision.sequence != expected_sequence:
            raise SourceRegistryConflict(
                f"sequence policy phải liên tiếp; cần {expected_sequence}."
            )
        SourceRegistryEntry(canonical_revision.policy.source_id, (*history, canonical_revision))
        self._session.add(SourcePolicyRevisionRecord.from_domain(workspace_id, canonical_revision))
        await self._session.flush()
        return SourceRegistryAppendResult(canonical_revision, False)

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

    async def list_entries(self, *, workspace_id: UUID) -> tuple[SourceRegistryEntry, ...]:
        """Liệt kê tối đa 200 nguồn trong workspace, lịch sử có thứ tự."""
        await self._bind_workspace(workspace_id)
        result = await self._session.scalars(
            select(SourcePolicyRevisionRecord.source_id)
            .where(SourcePolicyRevisionRecord.workspace_id == workspace_id)
            .distinct()
            .order_by(SourcePolicyRevisionRecord.source_id)
            .limit(200)
        )
        entries = []
        for source_id in result.all():
            entry = await self.get_entry(workspace_id=workspace_id, source_id=source_id)
            if entry is not None:
                entries.append(entry)
        return tuple(entries)

    async def get_quota_snapshot(
        self, *, workspace_id: UUID, source_id: str
    ) -> SourceQuotaSnapshot | None:
        """Đọc usage dưới source lock và clock DB, không ghi reservation."""
        await self._bind_workspace(workspace_id)
        await self._lock_source(workspace_id, source_id)
        entry = await self.get_entry(workspace_id=workspace_id, source_id=source_id)
        if entry is None:
            return None
        return await self._quota_snapshot_locked(
            workspace_id=workspace_id, source_id=source_id, entry=entry
        )

    async def _quota_snapshot_locked(
        self, *, workspace_id: UUID, source_id: str, entry: SourceRegistryEntry
    ) -> SourceQuotaSnapshot:
        now = await self.database_clock(workspace_id=workspace_id)
        current = entry.current
        events = await self._quota_events(workspace_id=workspace_id, source_id=source_id)
        usage, started = _current_usage(events, at=now, budget=current.budget)
        if usage is None:
            usage = SourceUsage(started, 0, 0, Decimal(0), current.budget.cost_unit)
        return SourceQuotaSnapshot(source_id, current.sequence, now, started, current.budget, usage)

    async def get_source_snapshot(
        self, *, workspace_id: UUID, source_id: str
    ) -> tuple[SourceRegistryEntry, SourceQuotaSnapshot | None] | None:
        """Đọc history và quota dưới cùng source lock/transaction."""
        await self._bind_workspace(workspace_id)
        await self._lock_source(workspace_id, source_id)
        entry = await self.get_entry(workspace_id=workspace_id, source_id=source_id)
        if entry is None:
            return None
        quota = await self._quota_snapshot_locked(
            workspace_id=workspace_id, source_id=source_id, entry=entry
        )
        return entry, quota

    async def find_revisions_by_idempotency_key(
        self, *, workspace_id: UUID, key: UUID
    ) -> tuple[SourcePolicyRevision, ...]:
        """Tìm tối đa hai revision cùng key trên toàn workspace để phát hiện bất nhất."""
        if not isinstance(key, UUID):
            raise ValueError("idempotency_key phải là UUID.")
        await self._bind_workspace(workspace_id)
        result = await self._session.scalars(
            select(SourcePolicyRevisionRecord)
            .where(
                SourcePolicyRevisionRecord.workspace_id == workspace_id,
                SourcePolicyRevisionRecord.policy_id.like(f"policy:{key.hex}:%"),
            )
            .limit(2)
        )
        return tuple(_to_domain_revision(row) for row in result.all())

    async def membership_id_for_user(self, *, workspace_id: UUID, user_id: UUID) -> UUID | None:
        """Tra membership legacy trong workspace; dữ liệu thiếu/mơ hồ trả None."""
        await self._bind_workspace(workspace_id)
        result = await self._session.scalars(
            select(UserMembership.id)
            .where(UserMembership.workspace_id == workspace_id, UserMembership.user_id == user_id)
            .limit(2)
        )
        memberships = result.all()
        return memberships[0] if len(memberships) == 1 else None

    async def database_clock(self, *, workspace_id: UUID) -> datetime:
        """Đọc clock PostgreSQL trong workspace thay cho đồng hồ caller khi xét quyền."""
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        await self._bind_workspace(workspace_id)
        now = await self._session.scalar(text("SELECT clock_timestamp()"))
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("database clock_timestamp() không trả thời gian có múi giờ.")
        return now.astimezone(UTC)

    async def enqueue_expired_policy_events(self, *, workspace_id: UUID, batch_size: int) -> int:
        """Dùng DB clock để append idempotent expiry events cho một batch trong workspace."""
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        if type(batch_size) is not int or not 1 <= batch_size <= 500:
            raise ValueError("batch_size phải là số nguyên từ 1 đến 500.")
        await self._bind_workspace(workspace_id)
        inserted = await self._session.scalar(
            text("SELECT public.enqueue_expired_source_policy_events(:batch_size)"),
            {"batch_size": batch_size},
        )
        if type(inserted) is not int:
            raise RuntimeError("database expiry scanner không trả số event hợp lệ.")
        return inserted

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

        observed = await self.get_observed_entry(workspace_id=workspace_id, source_id=source_id)
        if observed is None:
            raise PermissionError("Policy hiện hành chưa có chuỗi receipt đầy đủ.")

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

        entry = observed.history
        current = entry.current
        if observed.known_at[current.policy.policy_id] >= now:
            raise PermissionError("Receipt policy chưa khả dụng theo database clock.")
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

    async def finalize_policy(
        self,
        *,
        workspace_id: UUID,
        source_id: str,
        policy_id: str,
    ) -> SourcePolicyObservationRecord:
        """Chỉ gọi ở transaction mới sau source commit; DB chặn cùng top-level xid."""
        await self._bind_workspace(workspace_id)
        source = await self._session.scalar(
            select(SourcePolicyRevisionRecord).where(
                SourcePolicyRevisionRecord.workspace_id == workspace_id,
                SourcePolicyRevisionRecord.source_id == source_id,
                SourcePolicyRevisionRecord.policy_id == policy_id,
            )
        )
        if source is None:
            raise LookupError("Policy chưa visible trong workspace của observer.")
        await self._lock_source(workspace_id, source_id)
        existing = await self._session.scalar(
            select(SourcePolicyObservationRecord).where(
                SourcePolicyObservationRecord.workspace_id == workspace_id,
                SourcePolicyObservationRecord.policy_id == policy_id,
            )
        )
        if existing is not None:
            return existing
        receipt = SourcePolicyObservationRecord(
            workspace_id=workspace_id,
            source_id=source_id,
            policy_id=policy_id,
            sequence=source.sequence,
        )
        self._session.add(receipt)
        await self._session.flush()
        await self._session.refresh(receipt)
        return receipt

    async def get_observed_entry(
        self,
        *,
        workspace_id: UUID,
        source_id: str,
    ) -> ObservedSourcePolicy | None:
        """Không fallback policy VERIFIED cũ nếu latest hoặc predecessor thiếu receipt."""
        await self._bind_workspace(workspace_id)
        await self._lock_source(workspace_id, source_id)
        rows = await self._policy_rows(workspace_id=workspace_id, source_id=source_id)
        if not rows:
            return None
        result = await self._session.scalars(
            select(SourcePolicyObservationRecord)
            .where(
                SourcePolicyObservationRecord.workspace_id == workspace_id,
                SourcePolicyObservationRecord.source_id == source_id,
            )
            .order_by(SourcePolicyObservationRecord.sequence)
        )
        receipts = {row.sequence: row for row in result.all()}
        known_at: dict[str, datetime] = {}
        previous = None
        for number, row in enumerate(rows, 1):
            receipt = receipts.get(number)
            if row.sequence != number or receipt is None or receipt.policy_id != row.policy_id:
                return None
            known = receipt.known_at
            if (
                known < row.created_at
                or (row.reviewed_at is not None and known < row.reviewed_at)
                or (previous is not None and known < previous)
            ):
                return None
            previous = known
            known_at[row.policy_id] = known
        history = SourceRegistryEntry(source_id, tuple(_to_domain_revision(row) for row in rows))
        return ObservedSourcePolicy(history, MappingProxyType(known_at))

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


def _same_revision_payload(stored: SourcePolicyRevision, candidate: SourcePolicyRevision) -> bool:
    """So sánh request idempotent bỏ qua timestamp do DB tự đóng dấu."""
    comparable_policy = stored.policy
    if candidate.policy.state is SourcePolicyState.VERIFIED:
        comparable_policy = replace(comparable_policy, reviewed_at=candidate.policy.reviewed_at)
    comparable = replace(
        stored,
        policy=comparable_policy,
        created_at=candidate.created_at,
    )
    return comparable == candidate


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
