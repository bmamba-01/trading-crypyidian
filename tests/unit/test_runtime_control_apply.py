"""MB05.6: apply-pause/kill/resume trong một transaction có fence; giữ UNKNOWN, vị thế, stop."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.unit.test_paper_execution import _plan
from tests.unit.test_paper_lifecycle import NOW, _bar, _gateway
from tests.unit.test_paper_runtime_application import _application_with_fakes, _FakeUnitOfWork
from tests.unit.test_runtime_control_invariant import _snapshot

from trading_bot.application.paper_lifecycle import PaperLifecycleCoordinator
from trading_bot.application.paper_runtime_application import PaperRuntimeApplicationError
from trading_bot.domain.execution.paper import OrderStatus
from trading_bot.domain.execution.runtime_control import FencingToken, RuntimeControlError


class _Ledger:
    def __init__(self, keys: dict[str, UUID]) -> None:
        self.keys = keys
        self.cancelled: list[UUID] = []

    async def find_intent_id(self, _uow: object, *, account_id: UUID, idempotency_key: str):
        return self.keys.get(idempotency_key)

    async def cancel_remainder(self, _uow: object, *, intent_id: UUID, occurred_at: object):
        self.cancelled.append(intent_id)
        return True


class _Checkpoints:
    def __init__(self, gateway: Any) -> None:
        self.snapshot = gateway.export_state()
        self.appends: list[dict[str, Any]] = []
        self.locked = 0

    async def lock_account(self, _uow: object, *, account_id: UUID) -> None:
        self.locked += 1

    async def load_latest(self, _uow: object, *, account_id: UUID):
        return 5, self.snapshot

    async def verify_ledger_consistency(self, _uow: object, **_: object) -> None:
        return None

    async def append(self, _uow: object, **kwargs: Any) -> SimpleNamespace:
        self.appends.append(kwargs)
        return SimpleNamespace(revision=6, snapshot=kwargs["snapshot"], duplicate=False)


class _Uow:
    def __init__(self) -> None:
        self.callbacks: list[Any] = []

    def after_commit(self, callback: Any) -> None:
        self.callbacks.append(callback)


def _gateway_with_orders():
    gateway = _gateway()
    accepted = gateway.submit(_plan(), "key-accepted", submitted_at=NOW, eligible_after=NOW)
    partial = gateway.submit(_plan(), "key-partial", submitted_at=NOW, eligible_after=NOW)
    gateway.apply_market_event(_bar("bar-partial", 1), intent_id=partial.intent_id)
    unknown = gateway.submit(_plan(), "key-unknown", submitted_at=NOW, eligible_after=NOW)
    gateway.mark_unknown(unknown.intent_id)
    assert gateway.order(partial.intent_id).status is OrderStatus.PARTIALLY_FILLED
    return gateway, accepted, partial, unknown


def test_apply_entry_pause_cancels_unfilled_releases_no_fill_and_keeps_unknown_and_position():
    gateway, accepted, partial, unknown = _gateway_with_orders()
    keys = {"key-accepted": uuid4(), "key-partial": uuid4(), "key-unknown": uuid4()}
    ledger = _Ledger(keys)
    checkpoints = _Checkpoints(gateway)
    coordinator = PaperLifecycleCoordinator(gateway, ledger, checkpoints)  # type: ignore[arg-type]
    released: list[UUID] = []

    async def release(intent_id: UUID) -> None:
        released.append(intent_id)

    uow = _Uow()
    result = asyncio.run(
        coordinator.apply_entry_pause(
            uow,  # type: ignore[arg-type]
            account_id=uuid4(),
            event_key="control:pause:0:1",
            occurred_at=NOW,
            fencing_token=FencingToken(4),
            release_reservation=release,
        )
    )

    assert set(ledger.cancelled) == {keys["key-accepted"], keys["key-partial"]}
    assert released == [keys["key-accepted"]]
    candidate = result.gateway
    assert candidate.entries_paused is True
    assert candidate.order(accepted.intent_id).status is OrderStatus.CANCELLED
    assert candidate.order(partial.intent_id).status is OrderStatus.CANCELLED
    assert candidate.order(unknown.intent_id).status is OrderStatus.UNKNOWN
    position = candidate.position(partial.intent_id)
    assert position is not None
    assert not position.is_closed
    assert position.stop_price == gateway.position(partial.intent_id).stop_price
    append = checkpoints.appends[0]
    assert append["fencing_token"] == FencingToken(4)
    assert append["event_key"] == "control:pause:0:1"
    restored = type(gateway).restore(gateway.policy, append["snapshot"])
    assert restored.entries_paused is True
    # Gateway thật chỉ đổi sau commit.
    assert gateway.entries_paused is False
    for callback in uow.callbacks:
        callback()
    assert gateway.entries_paused is True


def test_apply_entry_resume_reopens_entries_without_touching_orders() -> None:
    gateway, _, partial, _ = _gateway_with_orders()
    gateway.pause_entries()
    checkpoints = _Checkpoints(gateway)
    coordinator = PaperLifecycleCoordinator(gateway, _Ledger({}), checkpoints)  # type: ignore[arg-type]

    result = asyncio.run(
        coordinator.apply_entry_resume(
            _Uow(),  # type: ignore[arg-type]
            account_id=uuid4(),
            event_key="control:resume:2:1",
            occurred_at=NOW,
            fencing_token=FencingToken(9),
        )
    )

    assert result.entries_paused is False
    assert result.gateway.order(partial.intent_id).status is OrderStatus.CANCELLED
    assert checkpoints.appends[0]["fencing_token"] == FencingToken(9)


class _States:
    def __init__(self, applied: tuple[int, int] = (0, 0)) -> None:
        self.applied = applied
        self.upserts: list[dict[str, Any]] = []

    def __call__(self, _session: object) -> _States:
        return self

    async def get(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(
            applied_workspace_control_revision=self.applied[0],
            applied_bot_control_revision=self.applied[1],
            reason_code=None,
        )

    async def upsert(self, **values: Any) -> None:
        self.upserts.append(values)


def _apply_service(snapshot: Any, states: _States, **overrides: Any):
    events: list[str] = []

    async def control(_session: object, **_: object) -> Any:
        events.append("control")
        return snapshot

    built = _application_with_fakes(
        fail_submission=False,
        lock_events=events,
        control_reader=control,
        state_repository_factory=states,
        **overrides,
    )
    service, command, *_ = built
    account = service._active_accounts[command.account_id]
    calls: dict[str, list[dict[str, Any]]] = {"pause": [], "resume": []}

    async def pause(uow: object, **kwargs: Any) -> SimpleNamespace:
        events.append("L4:pause")
        calls["pause"].append(kwargs)
        return SimpleNamespace(
            cancelled_intent_ids=("paper:x",), released_reservation_intent_ids=(uuid4(),)
        )

    async def resume(uow: object, **kwargs: Any) -> SimpleNamespace:
        events.append("L4:resume")
        calls["resume"].append(kwargs)
        return SimpleNamespace(cancelled_intent_ids=(), released_reservation_intent_ids=())

    account.coordinator.apply_entry_pause = pause
    account.coordinator.apply_entry_resume = resume
    return built, events, calls


def test_apply_pause_runs_in_one_fenced_transaction_with_lock_order_and_state_update() -> None:
    states = _States()
    (service, command, _, _, counters, *_), events, calls = _apply_service(
        _snapshot(bot_rev=2, paused=True), states
    )
    _FakeUnitOfWork.instances = []

    result = asyncio.run(service.apply_runtime_control(command.account_id))

    assert result.changed
    assert result.bot_paused
    assert not result.workspace_killed
    assert len(calls["pause"]) == 1
    assert calls["resume"] == []
    assert calls["pause"][0]["fencing_token"] == FencingToken(1)
    assert events == ["control", "L1", "control", "L2-L3", "L4:pause"]
    assert len(counters.fences) == 1
    # Kiểm nhanh (không khóa) + đúng một transaction ghi.
    assert len(_FakeUnitOfWork.instances) == 2
    assert all(uow.committed for uow in _FakeUnitOfWork.instances)
    assert states.upserts[-1]["applied_bot_control_revision"] == 2
    assert states.upserts[-1]["applied_workspace_control_revision"] == 0


def test_apply_is_noop_without_l1_when_control_already_applied() -> None:
    states = _States(applied=(0, 0))
    (service, command, *_), events, calls = _apply_service(_snapshot(), states)

    result = asyncio.run(service.apply_runtime_control(command.account_id))

    assert not result.changed
    assert events == ["control"]
    assert calls == {"pause": [], "resume": []}


def test_kill_blocks_entries_and_release_does_not_resume_individually_paused_bot() -> None:
    states = _States(applied=(1, 1))
    killed, _, kill_calls = _apply_service(_snapshot(ws_rev=1, kill=True), _States())
    asyncio.run(killed[0].apply_runtime_control(killed[1].account_id))
    assert len(kill_calls["pause"]) == 1
    assert kill_calls["pause"][0]["event_key"] == "control:pause:1:0"

    # Workspace release (rev 2) nhưng bot vẫn PAUSED riêng (rev 1): không mở entry.
    (service, command, *_), _, calls = _apply_service(
        _snapshot(ws_rev=2, kill=False, bot_rev=1, paused=True), states
    )
    result = asyncio.run(service.apply_runtime_control(command.account_id))

    assert result.bot_paused
    assert not result.workspace_killed
    assert calls["resume"] == []
    assert calls["pause"][0]["event_key"] == "control:pause:2:1"


def test_resume_after_release_reopens_entries() -> None:
    states = _States(applied=(1, 0))
    (service, command, *_), _, calls = _apply_service(_snapshot(ws_rev=2), states)
    service._active_accounts[command.account_id].gateway.entries_paused = True

    result = asyncio.run(service.apply_runtime_control(command.account_id))

    assert result.changed
    assert not result.bot_paused
    assert not result.workspace_killed
    assert calls["pause"] == []
    assert len(calls["resume"]) == 1
    assert calls["resume"][0]["event_key"] == "control:resume:2:0"


class _StaleGuard:
    def __init__(self, _session: object) -> None:
        pass

    async def require(self, **_: object) -> None:
        raise RuntimeControlError("STALE_FENCING_TOKEN")


def test_apply_pause_with_stale_token_is_rejected_before_any_write() -> None:
    """C6: apply-pause có fence; token cũ bị từ chối, không hủy lệnh/checkpoint nào."""
    lost: list[tuple[UUID, str]] = []
    states = _States()
    (service, command, *_), _, calls = _apply_service(
        _snapshot(bot_rev=1, paused=True),
        states,
        fence_guard_factory=_StaleGuard,
        lease_lost_callback=lambda account, code: lost.append((account, code)),
    )

    with pytest.raises(PaperRuntimeApplicationError, match="STALE_FENCING_TOKEN"):
        asyncio.run(service.apply_runtime_control(command.account_id))

    assert calls["pause"] == []
    assert states.upserts == []
    assert lost == [(command.account_id, "STALE_FENCING_TOKEN")]
    assert not service.is_available_for(command.account_id)


def test_expire_reservations_with_stale_token_is_rejected_before_any_write() -> None:
    """C6: expire/release reservation chạy trong command có fence."""
    expired: list[object] = []

    class _Risk:
        async def expire_due(self, **kwargs: object) -> int:
            expired.append(kwargs)
            return 1

    service, command, *_ = _application_with_fakes(
        fail_submission=False,
        fence_guard_factory=_StaleGuard,
        risk_repository_factory=lambda _session: _Risk(),
    )

    with pytest.raises(PaperRuntimeApplicationError, match="STALE_FENCING_TOKEN"):
        asyncio.run(service.expire_reservations(command.account_id, as_of=NOW))
    assert expired == []


def test_expire_reservations_runs_after_l1_fence_and_coordinator_locks() -> None:
    events: list[str] = []

    class _Risk:
        async def expire_due(self, **kwargs: object) -> int:
            events.append("expire")
            return 2

    service, command, _, _, counters, *_ = _application_with_fakes(
        fail_submission=False,
        lock_events=events,
        risk_repository_factory=lambda _session: _Risk(),
    )

    assert asyncio.run(service.expire_reservations(command.account_id, as_of=NOW)) == 2
    assert events == ["L1", "L2-L3", "expire"]
    assert len(counters.fences) == 1


def test_apply_failure_of_one_bot_does_not_block_sibling_application() -> None:
    states = _States()
    (service, command, *_), _, calls = _apply_service(_snapshot(ws_rev=1, kill=True), states)
    first = service._active_accounts[command.account_id]
    sibling_id = uuid4()
    sibling_calls: list[dict[str, Any]] = []

    async def sibling_pause(uow: object, **kwargs: Any) -> SimpleNamespace:
        sibling_calls.append(kwargs)
        return SimpleNamespace(cancelled_intent_ids=(), released_reservation_intent_ids=())

    async def broken(uow: object, **kwargs: Any) -> None:
        raise RuntimeError("synthetic checkpoint failure")

    sibling = SimpleNamespace(
        **{
            **vars(first),
            "account_id": sibling_id,
            "bot_id": uuid4(),
            "coordinator": SimpleNamespace(apply_entry_pause=sibling_pause),
        }
    )
    service._active_accounts[sibling_id] = sibling
    first.coordinator.apply_entry_pause = broken

    with pytest.raises(RuntimeError):
        asyncio.run(service.apply_runtime_control(command.account_id))
    result = asyncio.run(service.apply_runtime_control(sibling_id))

    assert result.changed
    assert result.workspace_killed
    assert len(sibling_calls) == 1
