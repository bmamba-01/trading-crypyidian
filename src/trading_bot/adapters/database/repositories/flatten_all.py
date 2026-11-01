"""Repository PostgreSQL cho preview và operation ledger flatten-all."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select

from trading_bot.adapters.database.models.flatten_entities import (
    FlattenOperationItemRecord,
    FlattenOperationRecord,
    FlattenPreviewRecord,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.execution.flatten_all import FlattenSnapshot


class FlattenRepositoryConflict(RuntimeError):
    """Preview hoặc idempotency key không còn phù hợp trạng thái DB."""


@dataclass(frozen=True, slots=True)
class FlattenOperationReceipt:
    operation_id: UUID
    status: str
    created: bool


class FlattenAllRepository:
    """Ghi ledger trong transaction của caller; không xử lý quyền hoặc side effect runtime."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def create_preview(
        self,
        uow: UnitOfWork,
        *,
        membership_id: UUID,
        snapshot: FlattenSnapshot,
        expires_at: datetime,
    ) -> FlattenPreviewRecord:
        if snapshot.workspace_id != uow.workspace_id or not _aware(expires_at):
            raise FlattenRepositoryConflict("Preview scope hoặc thời điểm hết hạn sai.")
        preview_id = uuid4()
        payload = snapshot.to_payload()
        preview_hash = _preview_hash(
            preview_id=preview_id,
            workspace_id=uow.workspace_id,
            membership_id=membership_id,
            snapshot_hash=snapshot.content_hash,
            expires_at=expires_at,
        )
        row = FlattenPreviewRecord(
            id=preview_id,
            workspace_id=uow.workspace_id,
            membership_id=membership_id,
            preview_hash=preview_hash,
            snapshot_hash=snapshot.content_hash,
            snapshot_payload=payload,
            expires_at=expires_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_preview_for_update(
        self, uow: UnitOfWork, *, membership_id: UUID, preview_id: UUID
    ) -> FlattenPreviewRecord | None:
        return cast(
            FlattenPreviewRecord | None,
            await self._session.scalar(
                select(FlattenPreviewRecord)
                .where(
                    FlattenPreviewRecord.workspace_id == uow.workspace_id,
                    FlattenPreviewRecord.membership_id == membership_id,
                    FlattenPreviewRecord.id == preview_id,
                )
                .with_for_update()
            ),
        )

    async def get_operation_by_key(
        self, uow: UnitOfWork, *, membership_id: UUID, idempotency_key: str
    ) -> FlattenOperationRecord | None:
        return cast(
            FlattenOperationRecord | None,
            await self._session.scalar(
                select(FlattenOperationRecord).where(
                    FlattenOperationRecord.workspace_id == uow.workspace_id,
                    FlattenOperationRecord.membership_id == membership_id,
                    FlattenOperationRecord.idempotency_key == idempotency_key,
                )
            ),
        )

    async def create_operation(
        self,
        uow: UnitOfWork,
        *,
        membership_id: UUID,
        preview: FlattenPreviewRecord,
        idempotency_key: str,
        request_hash: str,
        reason: str,
        snapshot: FlattenSnapshot,
    ) -> FlattenOperationRecord:
        if preview.workspace_id != uow.workspace_id or preview.membership_id != membership_id:
            raise FlattenRepositoryConflict("Preview ngoài workspace/principal.")
        operation = FlattenOperationRecord(
            workspace_id=uow.workspace_id,
            membership_id=membership_id,
            preview_id=preview.id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            snapshot_hash=snapshot.content_hash,
            status=("QUEUED" if any(bot.targets for bot in snapshot.bots) else "COMPLETED"),
            reason=reason,
        )
        self._session.add(operation)
        await self._session.flush()
        items: list[FlattenOperationItemRecord] = []
        for bot in snapshot.bots:
            for target in bot.targets:
                items.append(
                    FlattenOperationItemRecord(
                        workspace_id=uow.workspace_id,
                        operation_id=operation.id,
                        membership_id=membership_id,
                        bot_id=bot.bot_id,
                        account_id=bot.account_id,
                        target_type=target.target_type,
                        target_id=target.target_id,
                        symbol=target.symbol,
                        target_status=target.status,
                        quantity=target.quantity,
                        mark_price=target.mark_price,
                        mark_record_id=target.mark_record_id,
                        mark_revision=target.mark_revision,
                        mark_checksum=target.mark_checksum,
                        mark_close_time=target.mark_close_time,
                        reservation_id=target.reservation_id,
                        status="BLOCKED" if target.mark_error_code else "PENDING",
                        last_error_code=target.mark_error_code,
                    )
                )
        self._session.add_all(items)
        preview.consumed_at = datetime.now(UTC)
        await self._session.flush()
        return operation

    async def get_operation(
        self, uow: UnitOfWork, *, membership_id: UUID, operation_id: UUID
    ) -> tuple[FlattenOperationRecord, list[FlattenOperationItemRecord]] | None:
        operation = await self._session.scalar(
            select(FlattenOperationRecord).where(
                FlattenOperationRecord.workspace_id == uow.workspace_id,
                FlattenOperationRecord.membership_id == membership_id,
                FlattenOperationRecord.id == operation_id,
            )
        )
        if operation is None:
            return None
        rows = await self._session.scalars(
            select(FlattenOperationItemRecord)
            .where(
                FlattenOperationItemRecord.workspace_id == uow.workspace_id,
                FlattenOperationItemRecord.membership_id == membership_id,
                FlattenOperationItemRecord.operation_id == operation_id,
            )
            .order_by(
                FlattenOperationItemRecord.bot_id,
                FlattenOperationItemRecord.target_type,
                FlattenOperationItemRecord.target_id,
            )
        )
        return operation, list(rows.all())


def _preview_hash(
    *,
    preview_id: UUID,
    workspace_id: UUID,
    membership_id: UUID,
    snapshot_hash: str,
    expires_at: datetime,
) -> str:
    import hashlib
    import json

    payload = {
        "preview_id": str(preview_id),
        "workspace_id": str(workspace_id),
        "membership_id": str(membership_id),
        "snapshot_hash": snapshot_hash,
        "expires_at": expires_at.isoformat(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None
