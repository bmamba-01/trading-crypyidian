"""Repository quản lý persistence cho source item revisions và takedowns theo workspace."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.source_item_entities import (
    SourceItemRevisionRecord,
    SourceTakedownRecord,
)
from trading_bot.domain.research.source_item_history import (
    SourceItemHistory,
    SourceItemHistoryConflict,
    SourceItemRevisionEntry,
)
from trading_bot.domain.research.source_items import SourceItemRevision

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def _utc(value: datetime, name: str) -> datetime:
    """Kiểm tra và chuẩn hóa datetime sang múi giờ UTC."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải là thời điểm có múi giờ.")
    return value.astimezone(UTC)


class SourceItemRepository:
    """Repository quản lý revision metadata và takedown của research items."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_revision(
        self,
        *,
        workspace_id: UUID,
        entry: SourceItemRevisionEntry,
        policy_id: str,
    ) -> SourceItemRevisionRecord:
        """Lưu revision vào DB; idempotent nếu cùng hash, từ chối nếu xung đột."""
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        if type(entry) is not SourceItemRevisionEntry:
            raise ValueError("entry phải là SourceItemRevisionEntry.")
        if not isinstance(policy_id, str) or not (1 <= len(policy_id) <= 160):
            raise ValueError("policy_id phải có độ dài từ 1 đến 160 ký tự.")

        rev = entry.revision
        await self._bind_workspace(workspace_id)
        await self._lock_item(workspace_id, rev.source_id, rev.item_id)

        # Kiểm tra trùng revision_id (idempotency check)
        duplicate = await self._session.scalar(
            select(SourceItemRevisionRecord).where(
                SourceItemRevisionRecord.workspace_id == workspace_id,
                SourceItemRevisionRecord.source_id == rev.source_id,
                SourceItemRevisionRecord.item_id == rev.item_id,
                SourceItemRevisionRecord.revision_id == rev.revision_id,
            )
        )
        if duplicate is not None:
            if duplicate.revision_hash == entry.revision_hash and duplicate.policy_id == policy_id:
                return duplicate
            raise SourceItemHistoryConflict("revision_id đã tồn tại với payload hoặc hash khác.")

        # Lấy danh sách revisions hiện có của item để xác minh predecessor chain
        existing_result = await self._session.scalars(
            select(SourceItemRevisionRecord)
            .where(
                SourceItemRevisionRecord.workspace_id == workspace_id,
                SourceItemRevisionRecord.source_id == rev.source_id,
                SourceItemRevisionRecord.item_id == rev.item_id,
            )
            .order_by(SourceItemRevisionRecord.sequence.asc())
        )
        existing_records = list(existing_result.all())

        if existing_records:
            latest = existing_records[-1]
            if latest.is_tombstone:
                raise SourceItemHistoryConflict("Không thể append revision sau tombstone.")
            if (
                entry.predecessor_revision_id != latest.revision_id
                or entry.predecessor_hash != latest.revision_hash
            ):
                raise SourceItemHistoryConflict(
                    "predecessor_revision_id hoặc predecessor_hash không khớp revision mới nhất."
                )
            if rev.observed_at < latest.observed_at.astimezone(UTC):
                raise SourceItemHistoryConflict(
                    "observed_at không được lùi so với revision trước đó."
                )
            next_sequence = latest.sequence + 1
        else:
            if entry.predecessor_revision_id is not None or entry.predecessor_hash is not None:
                raise SourceItemHistoryConflict(
                    "Revision đầu tiên của item không được có predecessor."
                )
            next_sequence = 1

        record = SourceItemRevisionRecord.from_domain(
            workspace_id=workspace_id,
            entry=entry,
            policy_id=policy_id,
            sequence=next_sequence,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def load_history(
        self,
        workspace_id: UUID,
        source_id: str,
        item_id: str,
    ) -> SourceItemHistory:
        """Tải lịch sử revision theo thứ tự sequence, tái dựng và xác minh hash chain."""
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        if not isinstance(source_id, str) or not _IDENTIFIER.fullmatch(source_id):
            raise ValueError("source_id không hợp lệ.")
        if not isinstance(item_id, str) or not _IDENTIFIER.fullmatch(item_id):
            raise ValueError("item_id không hợp lệ.")

        await self._bind_workspace(workspace_id)
        result = await self._session.scalars(
            select(SourceItemRevisionRecord)
            .where(
                SourceItemRevisionRecord.workspace_id == workspace_id,
                SourceItemRevisionRecord.source_id == source_id,
                SourceItemRevisionRecord.item_id == item_id,
            )
            .order_by(SourceItemRevisionRecord.sequence.asc())
        )
        records = list(result.all())
        if not records:
            return SourceItemHistory.empty(source_id=source_id, item_id=item_id)

        entries = tuple(record.to_domain() for record in records)
        return SourceItemHistory(source_id=source_id, item_id=item_id, entries=entries)

    async def record_takedown(
        self,
        *,
        workspace_id: UUID,
        source_id: str,
        item_id: str,
        reason: str,
        requested_by: str,
        requested_at: datetime | None = None,
        effective_at: datetime | None = None,
    ) -> SourceTakedownRecord:
        """Ghi nhận takedown active cho một source item."""
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        if not isinstance(source_id, str) or not _IDENTIFIER.fullmatch(source_id):
            raise ValueError("source_id không hợp lệ.")
        if not isinstance(item_id, str) or not _IDENTIFIER.fullmatch(item_id):
            raise ValueError("item_id không hợp lệ.")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason không được để trống.")
        if not isinstance(requested_by, str) or not requested_by.strip():
            raise ValueError("requested_by không được để trống.")

        await self._bind_workspace(workspace_id)
        await self._lock_item(workspace_id, source_id, item_id)

        if requested_at is None:
            now = await self._session.scalar(text("SELECT clock_timestamp()"))
            if not isinstance(now, datetime) or now.tzinfo is None:
                now = datetime.now(UTC)
            req_at = now.astimezone(UTC)
        else:
            req_at = _utc(requested_at, "requested_at")

        if effective_at is None:
            eff_at = req_at
        else:
            eff_at = _utc(effective_at, "effective_at")

        if eff_at < req_at:
            raise ValueError("effective_at không được trước requested_at.")

        # Kiểm tra active takedown hiện tại
        existing = await self._session.scalar(
            select(SourceTakedownRecord).where(
                SourceTakedownRecord.workspace_id == workspace_id,
                SourceTakedownRecord.source_id == source_id,
                SourceTakedownRecord.item_id == item_id,
                SourceTakedownRecord.is_active.is_(True),
            )
        )
        if existing is not None:
            if (
                existing.reason == reason
                and existing.requested_by == requested_by
                and existing.effective_at.astimezone(UTC) == eff_at
            ):
                return existing
            raise SourceItemHistoryConflict("Đã có takedown đang hoạt động với nội dung khác.")

        takedown = SourceTakedownRecord(
            workspace_id=workspace_id,
            source_id=source_id,
            item_id=item_id,
            reason=reason,
            requested_by=requested_by,
            requested_at=req_at,
            effective_at=eff_at,
            is_active=True,
        )
        self._session.add(takedown)
        await self._session.flush()
        return takedown

    async def is_takedown_active(
        self,
        workspace_id: UUID,
        source_id: str,
        item_id: str,
        *,
        as_of: datetime,
    ) -> bool:
        """Kiểm tra xem item có đang bị takedown tại thời điểm as_of hay không."""
        instant = _utc(as_of, "as_of")
        await self._bind_workspace(workspace_id)
        active_takedown = await self._session.scalar(
            select(SourceTakedownRecord).where(
                SourceTakedownRecord.workspace_id == workspace_id,
                SourceTakedownRecord.source_id == source_id,
                SourceTakedownRecord.item_id == item_id,
                SourceTakedownRecord.is_active.is_(True),
                SourceTakedownRecord.effective_at <= instant,
            )
        )
        return active_takedown is not None

    async def get_latest_usable_revision(
        self,
        workspace_id: UUID,
        source_id: str,
        item_id: str,
        *,
        as_of: datetime,
        evaluated_at: datetime | None = None,
    ) -> SourceItemRevision | None:
        """Trả về revision mới nhất có available_at <= as_of và không bị tombstone/takedown."""
        instant = _utc(as_of, "as_of")
        evaluated = _utc(evaluated_at, "evaluated_at") if evaluated_at is not None else instant
        if instant > evaluated:
            raise ValueError("as_of không được sau evaluated_at.")

        await self._bind_workspace(workspace_id)

        # Lấy active takedown (nếu có)
        active_takedown = await self._session.scalar(
            select(SourceTakedownRecord).where(
                SourceTakedownRecord.workspace_id == workspace_id,
                SourceTakedownRecord.source_id == source_id,
                SourceTakedownRecord.item_id == item_id,
                SourceTakedownRecord.is_active.is_(True),
            )
        )
        current_takedown_at = (
            active_takedown.effective_at.astimezone(UTC) if active_takedown is not None else None
        )

        history = await self.load_history(workspace_id, source_id, item_id)
        return history.select_as_of(
            as_of=instant,
            evaluated_at=evaluated,
            current_takedown_at=current_takedown_at,
        )

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        """Thiết lập workspace scope cho Session qua PostgreSQL setting."""
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )

    async def _lock_item(self, workspace_id: UUID, source_id: str, item_id: str) -> None:
        """Khóa transaction theo workspace/source/item để tránh race condition khi append."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"research-source-item:{workspace_id}:{source_id}:{item_id}"},
        )
