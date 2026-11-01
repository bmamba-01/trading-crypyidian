"""Application service cho preview và confirm flatten-all PAPER."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.flatten_all import (
    FlattenAllRepository,
    FlattenOperationReceipt,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.service import RECENT_MFA_LIFETIME
from trading_bot.domain.execution.flatten_all import FlattenSnapshot

PREVIEW_TTL = timedelta(seconds=90)


class FlattenAllError(RuntimeError):
    """Reason code an toàn cho lỗi flatten-all."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class FlattenSnapshotProvider(Protocol):
    async def capture(self, uow: UnitOfWork) -> FlattenSnapshot: ...


@dataclass(frozen=True, slots=True)
class FlattenPreviewResult:
    preview_id: UUID
    preview_hash: str
    snapshot_hash: str
    expires_at: datetime
    bot_count: int
    position_count: int
    open_order_count: int
    targets: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class FlattenConfirmResult:
    operation_id: UUID
    status: str
    created: bool


class FlattenAllService:
    """Factory theo principal; xác thực step-up và gắn confirm vào snapshot đã xem."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession] | None,
        *,
        snapshot_provider: FlattenSnapshotProvider,
        repository_factory: Callable[[Any], Any] = FlattenAllRepository,
        unit_of_work_factory: Callable[..., Any] = UnitOfWork,
        operations_factory: Callable[[Principal], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._snapshot_provider = snapshot_provider
        self._repository_factory = repository_factory
        self._unit_of_work_factory = unit_of_work_factory
        self._operations_factory = operations_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def for_principal(self, principal: Principal) -> PrincipalFlattenAll:
        return PrincipalFlattenAll(self, principal)


class PrincipalFlattenAll:
    """Preview, confirm và trạng thái operation trong workspace đã xác thực."""

    def __init__(self, service: FlattenAllService, principal: Principal) -> None:
        self._service = service
        self._principal = principal

    async def create_preview(self, reason: str) -> FlattenPreviewResult:
        principal = self._principal
        now = self._service._clock()
        if not _aware(now):
            raise FlattenAllError("FLATTEN_CLOCK_INVALID")
        _authorize(principal, reason, now)
        expires_at = now + PREVIEW_TTL
        async with self._uow() as uow:
            snapshot = await _capture_snapshot(self._service._snapshot_provider, uow)
            if snapshot.workspace_id != principal.workspace_id:
                raise FlattenAllError("FLATTEN_SNAPSHOT_SCOPE_INVALID")
            row = await self._repository(uow).create_preview(
                uow,
                membership_id=principal.membership_id,
                snapshot=snapshot,
                expires_at=expires_at,
            )
            uow.record_audit(
                action="workspace.flatten.preview_created",
                target_type="workspace",
                target_id=principal.workspace_id,
                reason=reason.strip(),
                after_state={
                    "preview_hash": row.preview_hash,
                    "snapshot_hash": row.snapshot_hash,
                    "bot_count": len(snapshot.bots),
                    "target_count": sum(len(bot.targets) for bot in snapshot.bots),
                },
            )
            return FlattenPreviewResult(
                preview_id=row.id,
                preview_hash=row.preview_hash,
                snapshot_hash=row.snapshot_hash,
                expires_at=row.expires_at,
                bot_count=len(snapshot.bots),
                position_count=sum(
                    target.target_type == "POSITION"
                    for bot in snapshot.bots
                    for target in bot.targets
                ),
                open_order_count=sum(
                    target.target_type == "OPEN_ORDER"
                    for bot in snapshot.bots
                    for target in bot.targets
                ),
                targets=tuple(
                    {
                        "bot_id": str(bot.bot_id),
                        "target_type": target.target_type,
                        "target_id": target.target_id,
                        "symbol": target.symbol,
                        "status": target.status,
                        "quantity": str(target.quantity),
                        "mark_price": (
                            str(target.mark_price) if target.mark_price is not None else ""
                        ),
                        "error_code": target.mark_error_code or "",
                    }
                    for bot in snapshot.bots
                    for target in bot.targets
                ),
            )

    async def confirm(
        self,
        *,
        preview_id: UUID,
        preview_hash: str,
        idempotency_key: str,
        reason: str,
    ) -> FlattenConfirmResult:
        principal = self._principal
        now = self._service._clock()
        if not _aware(now):
            raise FlattenAllError("FLATTEN_CLOCK_INVALID")
        _authorize(principal, reason, now)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise FlattenAllError("FLATTEN_IDEMPOTENCY_KEY_REQUIRED")
        if len(idempotency_key) > 128:
            raise FlattenAllError("FLATTEN_IDEMPOTENCY_KEY_INVALID")
        if not _is_sha256(preview_hash):
            raise FlattenAllError("FLATTEN_PREVIEW_INVALID")
        request_hash = _request_hash(preview_id, preview_hash, reason.strip())
        receipt: FlattenOperationReceipt
        bot_ids: tuple[UUID, ...]
        async with self._uow() as uow:
            repository = self._repository(uow)
            existing = await repository.get_operation_by_key(
                uow, membership_id=principal.membership_id, idempotency_key=idempotency_key
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise FlattenAllError("FLATTEN_IDEMPOTENCY_KEY_REUSED")
                receipt = FlattenOperationReceipt(existing.id, existing.status, False)
                previous_preview = await repository.get_preview_for_update(
                    uow, membership_id=principal.membership_id, preview_id=existing.preview_id
                )
                if previous_preview is None:
                    raise FlattenAllError("FLATTEN_PREVIEW_NOT_FOUND")
                bot_ids = tuple(
                    UUID(str(bot["bot_id"]))
                    for bot in previous_preview.snapshot_payload.get("bots", [])
                )
            else:
                preview = await repository.get_preview_for_update(
                    uow, membership_id=principal.membership_id, preview_id=preview_id
                )
                now = self._service._clock()
                if preview is None:
                    raise FlattenAllError("FLATTEN_PREVIEW_NOT_FOUND")
                if not _aware(now) or preview.expires_at <= now:
                    raise FlattenAllError("FLATTEN_PREVIEW_EXPIRED")
                expected_preview_hash = _preview_hash(
                    preview_id=preview.id,
                    workspace_id=principal.workspace_id,
                    membership_id=principal.membership_id,
                    snapshot_hash=preview.snapshot_hash,
                    expires_at=preview.expires_at,
                )
                if preview_hash != preview.preview_hash or (
                    preview.preview_hash != expected_preview_hash
                ):
                    raise FlattenAllError("FLATTEN_PREVIEW_INVALID")
                if preview.consumed_at is not None:
                    raise FlattenAllError("FLATTEN_PREVIEW_ALREADY_USED")
                snapshot = await _capture_snapshot(self._service._snapshot_provider, uow)
                if snapshot.content_hash != preview.snapshot_hash:
                    raise FlattenAllError("FLATTEN_PREVIEW_STALE")
                operation = await repository.create_operation(
                    uow,
                    membership_id=principal.membership_id,
                    preview=preview,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    reason=reason.strip(),
                    snapshot=snapshot,
                )
                bot_ids = tuple(bot.bot_id for bot in snapshot.bots)
                receipt = FlattenOperationReceipt(operation.id, operation.status, True)
                uow.record_audit(
                    action="workspace.flatten.confirmed",
                    target_type="flatten_operation",
                    target_id=operation.id,
                    reason=reason.strip(),
                    after_state={
                        "preview_hash": preview.preview_hash,
                        "snapshot_hash": snapshot.content_hash,
                        "target_count": sum(len(bot.targets) for bot in snapshot.bots),
                    },
                )

        operations = self._service._operations_factory
        if operations is not None:
            principal_operations = operations(principal)
            for bot_id in bot_ids:
                await principal_operations.pause_bot(bot_id, f"Flatten-all {receipt.operation_id}")
        return FlattenConfirmResult(receipt.operation_id, receipt.status, receipt.created)

    async def get_operation(self, operation_id: UUID) -> dict[str, Any]:
        async with self._uow() as uow:
            result = await self._repository(uow).get_operation(
                uow, membership_id=self._principal.membership_id, operation_id=operation_id
            )
            if result is None:
                raise FlattenAllError("FLATTEN_OPERATION_NOT_FOUND")
            operation, items = result
            return {
                "id": str(operation.id),
                "status": operation.status,
                "created_at": operation.created_at.isoformat(),
                "items": [
                    {
                        "bot_id": str(item.bot_id),
                        "target_type": item.target_type,
                        "target_id": item.target_id,
                        "symbol": item.symbol,
                        "quantity": str(item.quantity),
                        "status": item.status,
                        "result": item.result,
                        "error_code": item.last_error_code,
                    }
                    for item in items
                ],
            }

    def _repository(self, uow: UnitOfWork) -> Any:
        return self._service._repository_factory(uow.session)

    def _uow(self) -> Any:
        return self._service._unit_of_work_factory(
            self._service._sessions,
            workspace_id=self._principal.workspace_id,
            actor_membership_id=self._principal.membership_id,
        )


def _authorize(principal: Principal, reason: str, now: datetime) -> None:
    if Role.OWNER not in principal.roles:
        raise FlattenAllError("CONTROL_PERMISSION_DENIED")
    verified = principal.mfa_verified_at
    if (
        verified is None
        or verified.tzinfo is None
        or verified > now
        or now - verified > RECENT_MFA_LIFETIME
    ):
        raise FlattenAllError("RECENT_MFA_REQUIRED")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise FlattenAllError("CONTROL_REASON_REQUIRED")


async def _capture_snapshot(provider: FlattenSnapshotProvider, uow: UnitOfWork) -> FlattenSnapshot:
    try:
        return await provider.capture(uow)
    except FlattenAllError:
        raise
    except Exception as error:
        raise FlattenAllError("FLATTEN_SNAPSHOT_UNAVAILABLE") from error


def _request_hash(preview_id: UUID, preview_hash: str, reason: str) -> str:
    payload = {
        "preview_id": str(preview_id),
        "preview_hash": preview_hash,
        "reason": reason,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _preview_hash(
    *,
    preview_id: UUID,
    workspace_id: UUID,
    membership_id: UUID,
    snapshot_hash: str,
    expires_at: datetime,
) -> str:
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


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None
