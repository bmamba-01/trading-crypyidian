"""Orchestrator ghi/commit rồi quan sát ở transaction mới; hỗ trợ retry theo cursor."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import (
    SourceItemObservationRecord,
    SourcePolicyObservationRecord,
)
from trading_bot.adapters.database.repositories.source_items import (
    SourceItemAppendResult,
    SourceItemRepository,
)
from trading_bot.adapters.database.repositories.source_registry import (
    SourceQuotaReservationResult,
    SourceRegistryAppendResult,
    SourceRegistryRepository,
)
from trading_bot.domain.research.source_items import SourceItemRevision
from trading_bot.domain.research.source_policy import SourceUsePurpose
from trading_bot.domain.research.source_registry import SourcePolicyRevision


@dataclass(frozen=True, slots=True)
class ObservationProgress:
    """Cursor là sequence receipt đã commit; complete chỉ đúng khi chain đã quan sát hết."""

    last_sequence: int
    complete: bool


class PostCommitSourceObserver:
    """Dùng factory đúng role: admin cho policy, ingestion cho item.

    Appender thoát transaction ghi trước khi recovery mở transaction mới. Nếu
    observer lỗi sau commit, row audit vẫn bền vững nhưng access/quota fail closed.
    Caller retry recover_* với cursor đã trả, không dùng flush làm commit.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        quota_sessions: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._sessions = sessions
        self._quota_sessions = quota_sessions

    def validate_application_port(self) -> None:
        """App quota cần factory ingestion tường minh, không fallback sang role ghi policy."""
        if self._quota_sessions is None:
            raise ValueError("App source registry cần quota_sessions ingestion tường minh.")

    async def append_revision_and_observe(
        self, *, workspace_id: UUID, revision: SourcePolicyRevision
    ) -> SourceRegistryAppendResult:
        """Port bắt buộc cho app Principal-bound; lỗi observer không bị nuốt sau commit."""
        result, progress = await self.append_policy(workspace_id=workspace_id, revision=revision)
        if not progress.complete:
            raise LookupError(
                "Source đã commit nhưng receipt policy chưa hoàn tất; cần retry/recovery."
            )
        return result

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
        if self._quota_sessions is None:
            raise ValueError("reserve_quota cần quota_sessions ingestion tường minh.")
        async with self._quota_sessions() as session, session.begin():
            return await SourceRegistryRepository(session).reserve_quota(
                workspace_id=workspace_id,
                source_id=source_id,
                idempotency_key=idempotency_key,
                purpose=purpose,
                reserved_bytes=reserved_bytes,
                reserved_cost=reserved_cost,
            )

    async def append_policy(
        self,
        *,
        workspace_id: UUID,
        revision: SourcePolicyRevision,
    ) -> tuple[SourceRegistryAppendResult, ObservationProgress]:
        async with self._sessions() as session, session.begin():
            result = await SourceRegistryRepository(session).append_revision(
                workspace_id=workspace_id, revision=revision
            )
        progress = await self._finish_policy(
            workspace_id=workspace_id, source_id=revision.policy.source_id
        )
        return result, progress

    async def append_item(
        self,
        *,
        workspace_id: UUID,
        revision: SourceItemRevision,
        predecessor_revision_id: str | None,
        retention_expires_at: datetime | None = None,
    ) -> tuple[SourceItemAppendResult, ObservationProgress]:
        async with self._sessions() as session, session.begin():
            result = await SourceItemRepository(session).append_revision(
                workspace_id=workspace_id,
                revision=revision,
                predecessor_revision_id=predecessor_revision_id,
                retention_expires_at=retention_expires_at,
            )
        progress = await self._finish_item(
            workspace_id=workspace_id, source_id=revision.source_id, item_id=revision.item_id
        )
        return result, progress

    async def _finish_policy(self, *, workspace_id: UUID, source_id: str) -> ObservationProgress:
        cursor = 0
        for _ in range(1000):
            progress = await self.recover_policy(
                workspace_id=workspace_id, source_id=source_id, after_sequence=cursor
            )
            if progress.complete:
                return progress
            if progress.last_sequence <= cursor:
                raise LookupError("Observer policy không tiến cursor; source giữ inaccessible.")
            cursor = progress.last_sequence
        raise TimeoutError(f"Observer policy hết page budget tại sequence {cursor}; cần retry.")

    async def _finish_item(
        self, *, workspace_id: UUID, source_id: str, item_id: str
    ) -> ObservationProgress:
        cursor = 0
        for _ in range(1000):
            progress = await self.recover_item(
                workspace_id=workspace_id,
                source_id=source_id,
                item_id=item_id,
                after_sequence=cursor,
            )
            if progress.complete:
                return progress
            if progress.last_sequence <= cursor:
                raise LookupError("Observer item không tiến cursor; source giữ inaccessible.")
            cursor = progress.last_sequence
        raise TimeoutError(f"Observer item hết page budget tại sequence {cursor}; cần retry.")

    async def recover_policy(
        self,
        *,
        workspace_id: UUID,
        source_id: str,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> ObservationProgress:
        self._validate_cursor(after_sequence, limit)
        async with self._sessions() as session, session.begin():
            repository = SourceRegistryRepository(session)
            history = await repository.get_entry(workspace_id=workspace_id, source_id=source_id)
            if history is None:
                return ObservationProgress(after_sequence, False)
            if after_sequence > len(history.revisions):
                raise ValueError("Cursor vượt history policy.")
            if after_sequence:
                receipt = await session.scalar(
                    select(SourcePolicyObservationRecord).where(
                        SourcePolicyObservationRecord.workspace_id == workspace_id,
                        SourcePolicyObservationRecord.source_id == source_id,
                        SourcePolicyObservationRecord.sequence == after_sequence,
                    )
                )
                if receipt is None:
                    raise LookupError("Cursor policy chưa có receipt đã commit.")
        cursor = after_sequence
        for entry in history.revisions[after_sequence : after_sequence + limit]:
            async with self._sessions() as session, session.begin():
                await SourceRegistryRepository(session).finalize_policy(
                    workspace_id=workspace_id,
                    source_id=source_id,
                    policy_id=entry.policy.policy_id,
                )
            cursor = entry.sequence
        async with self._sessions() as session, session.begin():
            latest = await SourceRegistryRepository(session).get_observed_entry(
                workspace_id=workspace_id, source_id=source_id
            )
            complete = latest is not None and cursor == len(latest.history.revisions)
        return ObservationProgress(cursor, complete)

    async def recover_item(
        self,
        *,
        workspace_id: UUID,
        source_id: str,
        item_id: str,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> ObservationProgress:
        self._validate_cursor(after_sequence, limit)
        async with self._sessions() as session, session.begin():
            history = await SourceItemRepository(session).get_history(
                workspace_id=workspace_id, source_id=source_id, item_id=item_id
            )
            if not history.entries:
                return ObservationProgress(after_sequence, False)
            if after_sequence > len(history.entries):
                raise ValueError("Cursor vượt history item.")
            if after_sequence:
                receipt = await session.scalar(
                    select(SourceItemObservationRecord).where(
                        SourceItemObservationRecord.workspace_id == workspace_id,
                        SourceItemObservationRecord.source_id == source_id,
                        SourceItemObservationRecord.item_id == item_id,
                        SourceItemObservationRecord.sequence == after_sequence,
                    )
                )
                if receipt is None:
                    raise LookupError("Cursor item chưa có receipt đã commit.")
        cursor = after_sequence
        for entry in history.entries[after_sequence : after_sequence + limit]:
            async with self._sessions() as session, session.begin():
                receipt = await SourceItemRepository(session).finalize_revision(
                    workspace_id=workspace_id,
                    source_id=source_id,
                    item_id=item_id,
                    revision_id=entry.revision.revision_id,
                )
            cursor = receipt.sequence
        async with self._sessions() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"research-source:{workspace_id}:{source_id}"},
            )
            latest = await SourceItemRepository(session).get_history(
                workspace_id=workspace_id, source_id=source_id, item_id=item_id
            )
            complete = cursor == len(latest.entries)
        return ObservationProgress(cursor, complete)

    @staticmethod
    def _validate_cursor(after_sequence: int, limit: int) -> None:
        if (
            type(after_sequence) is not int
            or after_sequence < 0
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise ValueError("Cursor không âm và limit từ 1 đến 1000.")
