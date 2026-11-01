"""Kiểm tra preview/confirm gắn đúng snapshot, MFA, idempotency và pause."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from trading_bot.application.flatten_all import FlattenAllError, FlattenAllService
from trading_bot.auth.policy import Principal, Role
from trading_bot.domain.execution.flatten_all import (
    FlattenBotSnapshot,
    FlattenSnapshot,
    FlattenTarget,
)

WORKSPACE = UUID("10000000-0000-0000-0000-000000000001")
MEMBERSHIP = UUID("20000000-0000-0000-0000-000000000001")
BOT = UUID("30000000-0000-0000-0000-000000000001")
ACCOUNT = UUID("40000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)


def _snapshot(revision: int = 2) -> FlattenSnapshot:
    target = FlattenTarget(
        target_type="POSITION",
        target_id="paper:entry-key",
        symbol="BTCUSDT",
        status="OPEN",
        quantity=Decimal("0.25"),
        mark_price=Decimal("65000"),
        mark_record_id=UUID("50000000-0000-0000-0000-000000000001"),
        mark_revision=1,
        mark_checksum="a" * 64,
        mark_close_time=NOW - timedelta(seconds=5),
    )
    return FlattenSnapshot(
        workspace_id=WORKSPACE,
        as_of=NOW,
        bots=(
            FlattenBotSnapshot(
                bot_id=BOT,
                account_id=ACCOUNT,
                bot_control_revision=revision,
                workspace_control_revision=1,
                targets=(target,),
                readiness="READY",
                activity_status="VERIFIED",
            ),
        ),
    )


class FakeUnitOfWork:
    def __init__(self, _sessions: object, **kwargs: Any) -> None:
        self.workspace_id = kwargs["workspace_id"]
        self.actor_membership_id = kwargs["actor_membership_id"]
        self.session = object()
        self.audits: list[dict[str, Any]] = []

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def record_audit(self, **kwargs: Any) -> None:
        self.audits.append(kwargs)


class FakeRepository:
    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store

    async def create_preview(self, uow: FakeUnitOfWork, **kwargs: Any) -> Any:
        snapshot = kwargs["snapshot"]
        preview_id = uuid4()
        from trading_bot.application.flatten_all import _preview_hash

        row = SimpleNamespace(
            id=preview_id,
            workspace_id=uow.workspace_id,
            membership_id=kwargs["membership_id"],
            preview_hash=_preview_hash(
                preview_id=preview_id,
                workspace_id=uow.workspace_id,
                membership_id=kwargs["membership_id"],
                snapshot_hash=snapshot.content_hash,
                expires_at=kwargs["expires_at"],
            ),
            snapshot_hash=snapshot.content_hash,
            snapshot_payload=snapshot.to_payload(),
            expires_at=kwargs["expires_at"],
            consumed_at=None,
        )
        self.store["previews"][preview_id] = row
        return row

    async def get_preview_for_update(
        self, _uow: FakeUnitOfWork, *, membership_id: UUID, preview_id: UUID
    ) -> Any:
        row = self.store["previews"].get(preview_id)
        return row if row is not None and row.membership_id == membership_id else None

    async def get_operation_by_key(
        self, _uow: FakeUnitOfWork, *, membership_id: UUID, idempotency_key: str
    ) -> Any:
        return self.store["operations"].get((membership_id, idempotency_key))

    async def create_operation(self, _uow: FakeUnitOfWork, **kwargs: Any) -> Any:
        preview = kwargs["preview"]
        operation = SimpleNamespace(
            id=uuid4(),
            status="QUEUED",
            request_hash=kwargs["request_hash"],
            preview_id=preview.id,
        )
        preview.consumed_at = NOW
        self.store["operations"][(kwargs["membership_id"], kwargs["idempotency_key"])] = operation
        return operation


class SnapshotProvider:
    def __init__(self, snapshot: FlattenSnapshot) -> None:
        self.snapshot = snapshot

    async def capture(self, _uow: FakeUnitOfWork) -> FlattenSnapshot:
        return self.snapshot


class PauseOperations:
    def __init__(self) -> None:
        self.paused: list[UUID] = []

    async def pause_bot(self, bot_id: UUID, _reason: str) -> None:
        self.paused.append(bot_id)


def _principal(*, roles: frozenset[Role] = frozenset({Role.OWNER})) -> Principal:
    return Principal(
        user_id=uuid4(),
        membership_id=MEMBERSHIP,
        workspace_id=WORKSPACE,
        roles=roles,
        mfa_verified_at=NOW - timedelta(minutes=1),
    )


def _service(
    snapshot: FlattenSnapshot,
) -> tuple[FlattenAllService, dict[str, Any], PauseOperations]:
    store: dict[str, Any] = {"previews": {}, "operations": {}}
    pause = PauseOperations()
    service = FlattenAllService(
        object(),  # type: ignore[arg-type]
        snapshot_provider=SnapshotProvider(snapshot),
        repository_factory=lambda _session: FakeRepository(store),
        unit_of_work_factory=FakeUnitOfWork,
        operations_factory=lambda _principal: pause,
        clock=lambda: NOW,
    )
    return service, store, pause


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def test_preview_captures_hash_counts_and_reason_audit() -> None:
    service, _store, _pause = _service(_snapshot())

    result = _run(service.for_principal(_principal()).create_preview("Đóng vị thế thủ công"))

    assert result.bot_count == 1
    assert result.position_count == 1
    assert result.open_order_count == 0
    assert result.snapshot_hash == _snapshot().content_hash
    assert result.expires_at == NOW + timedelta(seconds=90)


def test_confirm_rejects_stale_snapshot_before_operation_or_pause() -> None:
    service, store, pause = _service(_snapshot())
    ops = service.for_principal(_principal())
    preview = _run(ops.create_preview("Rà soát rủi ro"))
    service._snapshot_provider.snapshot = _snapshot(revision=3)  # type: ignore[attr-defined]

    with pytest.raises(FlattenAllError) as error:
        _run(
            ops.confirm(
                preview_id=preview.preview_id,
                preview_hash=preview.preview_hash,
                idempotency_key="flatten-1",
                reason="Rà soát rủi ro",
            )
        )

    assert error.value.reason_code == "FLATTEN_PREVIEW_STALE"
    assert not store["operations"]
    assert pause.paused == []


def test_confirm_replay_returns_same_operation_and_retries_pause_idempotently() -> None:
    service, store, pause = _service(_snapshot())
    ops = service.for_principal(_principal())
    preview = _run(ops.create_preview("Sự cố vận hành"))
    args = {
        "preview_id": preview.preview_id,
        "preview_hash": preview.preview_hash,
        "idempotency_key": "flatten-1",
        "reason": "Sự cố vận hành",
    }

    first = _run(ops.confirm(**args))
    second = _run(ops.confirm(**args))

    assert first.created is True
    assert second.created is False
    assert first.operation_id == second.operation_id
    assert len(store["operations"]) == 1
    assert pause.paused == [BOT, BOT]


@pytest.mark.parametrize(
    ("principal", "reason", "expected"),
    [
        (_principal(roles=frozenset({Role.OPERATOR})), "reason", "CONTROL_PERMISSION_DENIED"),
        (
            replace(_principal(), mfa_verified_at=NOW - timedelta(minutes=16)),
            "reason",
            "RECENT_MFA_REQUIRED",
        ),
        (_principal(), "  ", "CONTROL_REASON_REQUIRED"),
    ],
)
def test_high_risk_preview_requires_owner_recent_mfa_and_reason(
    principal: Principal, reason: str, expected: str
) -> None:
    service, _store, _pause = _service(_snapshot())

    with pytest.raises(FlattenAllError) as error:
        _run(service.for_principal(principal).create_preview(reason))

    assert error.value.reason_code == expected


def test_snapshot_provider_failure_has_stable_fail_closed_error() -> None:
    service, _store, _pause = _service(_snapshot())

    async def fail(_uow: FakeUnitOfWork) -> FlattenSnapshot:
        raise RuntimeError("database internals must not be exposed")

    service._snapshot_provider.capture = fail  # type: ignore[method-assign]

    with pytest.raises(FlattenAllError) as error:
        _run(service.for_principal(_principal()).create_preview("Đóng vị thế"))

    assert error.value.reason_code == "FLATTEN_SNAPSHOT_UNAVAILABLE"
    assert "database internals" not in str(error.value)
