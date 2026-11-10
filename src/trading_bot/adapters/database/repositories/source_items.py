"""Persistence metadata-only và chọn revision có quyền trong đúng workspace."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    SourceItemObservationRecord,
    SourceItemRevisionRecord,
    SourceItemTakedownRecord,
)
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.domain.research.source_item_history import (
    SourceItemHistory,
    SourceItemHistoryConflict,
    SourceItemRevisionEntry,
)
from trading_bot.domain.research.source_items import (
    SourceItemRevision,
    _require_canonical_https_url,
)
from trading_bot.domain.research.source_policy import SourceUsePurpose
from trading_bot.domain.research.source_registry import SourcePolicyRevision, source_rights_hash
from trading_bot.domain.research.source_revision_access import (
    SourceAccessDecision,
    SourceAccessEnvelope,
    SourceAccessMode,
    SourceAccessReason,
    evaluate_source_revision_access,
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


@dataclass(frozen=True, slots=True)
class SourceItemAppendResult:
    """Kết quả append; retry đồng nhất không sinh thêm revision."""

    entry: SourceItemRevisionEntry
    is_duplicate: bool


class SourceItemRepository:
    """Giữ transaction của caller; metadata history không tự cấp quyền sử dụng.

    ``select_accessible`` là đường chọn có quyền: nạp policy và trạng thái hiện
    hành dưới cùng khóa workspace/source, dùng DB clock và pure access gate.
    Chưa nối repository này với connector, HTTP hoặc WP06 verifier.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_revision(
        self,
        *,
        workspace_id: UUID,
        revision: SourceItemRevision,
        predecessor_revision_id: str | None,
        retention_expires_at: datetime | None = None,
    ) -> SourceItemAppendResult:
        """Append metadata; không lưu nội dung và không cấp quyền gọi nguồn ngoài."""
        if type(revision) is not SourceItemRevision:
            raise ValueError("revision phải là SourceItemRevision.")
        retention = (
            _utc(retention_expires_at, "retention_expires_at")
            if retention_expires_at is not None
            else None
        )
        await self._bind(workspace_id, revision.source_id, revision.item_id)
        await self._lock(workspace_id, revision.source_id)
        stored = await self._rows(workspace_id, revision.source_id, revision.item_id)
        history = _history(revision.source_id, revision.item_id, stored)
        updated = history.append(revision, predecessor_revision_id=predecessor_revision_id)
        if updated is history:
            row = next(row for row in stored if row.revision_id == revision.revision_id)
            if row.retention_expires_at != retention:
                raise SourceItemHistoryConflict("revision_id đã có retention khác.")
            return SourceItemAppendResult(row.to_entry(), True)
        if await self._takedown(workspace_id, revision.source_id, revision.item_id) is not None:
            raise SourceItemHistoryConflict("Không thể append sau takedown của item.")
        entry = updated.entries[-1]
        self._session.add(
            SourceItemRevisionRecord.from_entry(
                workspace_id,
                len(updated.entries),
                entry,
                retention_expires_at=retention,
            )
        )
        await self._session.flush()
        return SourceItemAppendResult(entry, False)

    async def get_history(
        self,
        *,
        workspace_id: UUID,
        source_id: str,
        item_id: str,
    ) -> SourceItemHistory:
        """Đọc lineage metadata; caller không được coi hash chain là quyền nguồn."""
        await self._bind(workspace_id, source_id, item_id)
        return _history(source_id, item_id, await self._rows(workspace_id, source_id, item_id))

    async def find_group(
        self,
        *,
        workspace_id: UUID,
        source_id: str,
        canonical_url: str,
    ) -> tuple[str, ...]:
        """Nhóm item có URL revision cuối giống hệt; không merge hoặc sửa URL."""
        _require_canonical_https_url(canonical_url)
        await self._bind(workspace_id, source_id)
        result = await self._session.scalars(
            select(SourceItemRevisionRecord)
            .where(
                SourceItemRevisionRecord.workspace_id == workspace_id,
                SourceItemRevisionRecord.source_id == source_id,
            )
            .order_by(SourceItemRevisionRecord.sequence.asc())
        )
        latest = {row.item_id: row for row in result.all()}
        return tuple(
            sorted(item_id for item_id, row in latest.items() if row.canonical_url == canonical_url)
        )

    async def take_down(
        self,
        *,
        workspace_id: UUID,
        source_id: str,
        item_id: str,
        reason_code: str,
    ) -> SourceItemTakedownRecord:
        """Rút item vĩnh viễn; mã lý do không chứa body và thời điểm lấy từ DB."""
        if not isinstance(reason_code, str) or not _REASON.fullmatch(reason_code):
            raise ValueError("reason_code phải là mã ASCII chữ thường, số hoặc dấu gạch dưới.")
        await self._bind(workspace_id, source_id, item_id)
        await self._lock(workspace_id, source_id)
        existing = await self._takedown(workspace_id, source_id, item_id)
        if existing is not None:
            if existing.reason_code != reason_code:
                raise SourceItemHistoryConflict("Takedown hiện hành đã có reason_code khác.")
            return existing
        row = SourceItemTakedownRecord(
            workspace_id=workspace_id,
            source_id=source_id,
            item_id=item_id,
            taken_down_at=await self._now(),
            reason_code=reason_code,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def select_accessible(
        self,
        *,
        workspace_id: UUID,
        source_id: str,
        item_id: str,
        purpose: SourceUsePurpose,
        as_of: datetime | None = None,
    ) -> SourceItemRevisionEntry | None:
        """Chọn revision khả dụng; giữ hợp đồng tương thích cho consumer cũ."""
        envelope = await self.select_access_envelope(
            workspace_id=workspace_id,
            source_id=source_id,
            item_id=item_id,
            purpose=purpose,
            as_of=as_of,
        )
        return envelope.revision

    async def select_access_envelope(
        self,
        *,
        workspace_id: UUID,
        source_id: str,
        item_id: str,
        purpose: SourceUsePurpose,
        as_of: datetime | None = None,
    ) -> SourceAccessEnvelope:
        """Trả quyết định quyền cùng lineage policy, receipt và thời điểm DB."""
        if type(purpose) is not SourceUsePurpose:
            raise ValueError("purpose phải là SourceUsePurpose tường minh.")
        await self._bind(workspace_id, source_id, item_id)
        await self._lock(workspace_id, source_id)
        now = await self._now()
        instant = _utc(as_of, "as_of") if as_of is not None else now
        if instant > now:
            raise ValueError("as_of không được sau database clock.")
        mode = SourceAccessMode.CURRENT if instant == now else SourceAccessMode.HISTORICAL_REPLAY

        def denied(
            reason: SourceAccessReason,
            *,
            policy_id: str | None = None,
            revision_known_at_upper_bound: datetime | None = None,
            historical_policy: SourcePolicyRevision | None = None,
            current_policy: SourcePolicyRevision | None = None,
            rights_hash: str | None = None,
            retention_expires_at: datetime | None = None,
            takedown_at: datetime | None = None,
        ) -> SourceAccessEnvelope:
            return SourceAccessEnvelope(
                decision=SourceAccessDecision(
                    permitted=False,
                    mode=mode,
                    reason=reason,
                    policy_id=policy_id,
                    as_of=instant,
                ),
                source_id=source_id,
                item_id=item_id,
                revision=None,
                purpose=purpose,
                as_of=instant,
                evaluated_at=now,
                revision_known_at_upper_bound=revision_known_at_upper_bound,
                historical_policy_id=(
                    historical_policy.policy.policy_id if historical_policy else None
                ),
                historical_policy_sequence=(
                    historical_policy.sequence if historical_policy else None
                ),
                current_policy_id=current_policy.policy.policy_id if current_policy else None,
                current_policy_sequence=current_policy.sequence if current_policy else None,
                rights_hash=rights_hash,
                retention_expires_at=retention_expires_at,
                takedown_at=takedown_at,
            )

        stored = await self._rows(workspace_id, source_id, item_id)
        if not stored:
            return denied(SourceAccessReason.REVISION_NOT_FOUND)
        history = _history(source_id, item_id, stored)
        takedown = await self._takedown(workspace_id, source_id, item_id)
        result = await self._session.scalars(
            select(SourceItemObservationRecord)
            .where(
                SourceItemObservationRecord.workspace_id == workspace_id,
                SourceItemObservationRecord.source_id == source_id,
                SourceItemObservationRecord.item_id == item_id,
            )
            .order_by(SourceItemObservationRecord.sequence)
        )
        receipts = {row.sequence: row for row in result.all()}
        known_at: dict[str, datetime] = {}
        previous = None
        for number, row in enumerate(stored, 1):
            receipt = receipts.get(number)
            if receipt is None or receipt.revision_id != row.revision_id:
                return denied(
                    SourceAccessReason.OBSERVATION_RECEIPT_MISSING,
                    takedown_at=takedown.taken_down_at if takedown else None,
                )
            known = _utc(receipt.known_at, "known_at")
            if (
                known < row.recorded_at
                or known < row.created_at
                or (previous is not None and known < previous)
            ):
                return denied(
                    SourceAccessReason.OBSERVATION_RECEIPT_MISSING,
                    takedown_at=takedown.taken_down_at if takedown else None,
                )
            previous = known
            known_at[row.revision_id] = known
        policies = await SourceRegistryRepository(self._session).get_observed_entry(
            workspace_id=workspace_id,
            source_id=source_id,
        )
        deadlines = [
            row.retention_expires_at for row in stored if row.retention_expires_at is not None
        ]
        retention_deadline = min(deadlines) if deadlines else None
        if policies is None:
            return denied(
                SourceAccessReason.POLICY_NOT_OBSERVED,
                retention_expires_at=retention_deadline,
                takedown_at=takedown.taken_down_at if takedown else None,
            )
        # Timestamp audit không chứng minh commit. Receipt từ transaction khác
        # đóng dấu quan sát bảo thủ; hash chain đầy đủ được kiểm trước khi lọc.
        known_entries = [
            entry
            for row, entry in zip(stored, history.entries, strict=True)
            if known_at[row.revision_id] < instant
            and entry.revision.available_at is not None
            and entry.revision.available_at <= instant
        ]
        current_policy = policies.history.current
        if not known_entries:
            return denied(
                SourceAccessReason.NOT_AVAILABLE_AS_OF,
                current_policy=current_policy,
                retention_expires_at=retention_deadline,
                takedown_at=takedown.taken_down_at if takedown else None,
            )
        selected_entry = known_entries[-1]
        selected = selected_entry.revision
        # Thu hồi hiện hành không bị lọc theo cutoff; tombstone đến muộn vẫn
        # chặn phát lại revision trước khi DB biết tombstone đó.
        current_tombstoned = any(
            row.tombstoned and row.available_at is not None and row.available_at <= now
            for row in stored
        )
        # Retention hiện hành không được bị né bằng cách replay revision cũ.
        decision = evaluate_source_revision_access(
            revision=selected,
            policy_history=policies.history,
            policy_known_at=policies.known_at,
            purpose=purpose,
            mode=mode,
            as_of=instant,
            evaluated_at=now,
            current_tombstoned=current_tombstoned,
            retention_expires_at=retention_deadline,
            takedown_at=takedown.taken_down_at if takedown else None,
        )
        policy_at = now if mode is SourceAccessMode.CURRENT else instant
        historical_policies = [
            entry
            for entry in policies.history.revisions
            if policies.known_at[entry.policy.policy_id] < policy_at
        ]
        historical_policy = historical_policies[-1] if historical_policies else None
        rights_hash = source_rights_hash(historical_policy) if historical_policy else None
        return SourceAccessEnvelope(
            decision=decision,
            source_id=source_id,
            item_id=item_id,
            revision=selected_entry if decision.permitted else None,
            purpose=purpose,
            as_of=instant,
            evaluated_at=now,
            revision_known_at_upper_bound=known_at[selected.revision_id],
            historical_policy_id=(
                historical_policy.policy.policy_id if historical_policy else None
            ),
            historical_policy_sequence=(historical_policy.sequence if historical_policy else None),
            current_policy_id=current_policy.policy.policy_id,
            current_policy_sequence=current_policy.sequence,
            rights_hash=rights_hash,
            retention_expires_at=retention_deadline,
            takedown_at=takedown.taken_down_at if takedown else None,
        )

    async def finalize_revision(
        self,
        *,
        workspace_id: UUID,
        source_id: str,
        item_id: str,
        revision_id: str,
    ) -> SourceItemObservationRecord:
        """Quan sát revision ở transaction mới; retry giữ nguyên receipt/known_at."""
        await self._bind(workspace_id, source_id, item_id)
        source = await self._session.scalar(
            select(SourceItemRevisionRecord).where(
                SourceItemRevisionRecord.workspace_id == workspace_id,
                SourceItemRevisionRecord.source_id == source_id,
                SourceItemRevisionRecord.item_id == item_id,
                SourceItemRevisionRecord.revision_id == revision_id,
            )
        )
        if source is None:
            raise LookupError("Revision chưa visible trong workspace của observer.")
        await self._lock(workspace_id, source_id)
        existing = await self._session.scalar(
            select(SourceItemObservationRecord).where(
                SourceItemObservationRecord.workspace_id == workspace_id,
                SourceItemObservationRecord.source_id == source_id,
                SourceItemObservationRecord.item_id == item_id,
                SourceItemObservationRecord.revision_id == revision_id,
            )
        )
        if existing is not None:
            return existing
        receipt = SourceItemObservationRecord(
            workspace_id=workspace_id,
            source_id=source_id,
            item_id=item_id,
            revision_id=revision_id,
            sequence=source.sequence,
        )
        self._session.add(receipt)
        await self._session.flush()
        await self._session.refresh(receipt)
        return receipt

    async def _bind(self, workspace_id: UUID, source_id: str, item_id: str | None = None) -> None:
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        for name, value in (("source_id", source_id), ("item_id", item_id)):
            if name == "item_id" and value is None:
                continue
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{name} không hợp lệ.")
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )

    async def _lock(self, workspace_id: UUID, source_id: str) -> None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"research-source:{workspace_id}:{source_id}"},
        )

    async def _now(self) -> datetime:
        return _utc(await self._session.scalar(text("SELECT clock_timestamp()")), "database clock")

    async def _rows(
        self, workspace_id: UUID, source_id: str, item_id: str
    ) -> list[SourceItemRevisionRecord]:
        result = await self._session.scalars(
            select(SourceItemRevisionRecord)
            .where(
                SourceItemRevisionRecord.workspace_id == workspace_id,
                SourceItemRevisionRecord.source_id == source_id,
                SourceItemRevisionRecord.item_id == item_id,
            )
            .order_by(SourceItemRevisionRecord.sequence.asc())
        )
        return list(result.all())

    async def _takedown(
        self, workspace_id: UUID, source_id: str, item_id: str
    ) -> SourceItemTakedownRecord | None:
        return await self._session.scalar(
            select(SourceItemTakedownRecord).where(
                SourceItemTakedownRecord.workspace_id == workspace_id,
                SourceItemTakedownRecord.source_id == source_id,
                SourceItemTakedownRecord.item_id == item_id,
            )
        )


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")
    return value.astimezone(UTC)


def _history(
    source_id: str, item_id: str, rows: list[SourceItemRevisionRecord]
) -> SourceItemHistory:
    if any(row.sequence != number for number, row in enumerate(rows, 1)):
        raise ValueError("sequence lịch sử metadata không liên tục.")
    return SourceItemHistory(source_id, item_id, tuple(row.to_entry() for row in rows))
