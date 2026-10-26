"""MB05.6 ruling 27: entry/reservation đọc lại control trong chính transaction (spy UoW)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from tests.unit.test_paper_runtime_application import _application_with_fakes, _FakeUnitOfWork

from trading_bot.application.paper_runtime_application import (
    EntryGateState,
    PaperEntryEventCommand,
    PaperRuntimeApplicationError,
)
from trading_bot.domain.execution.paper import OrderStatus, PaperBar
from trading_bot.domain.execution.runtime_control import (
    BotControlState,
    BotEntryState,
    BotReadiness,
    RuntimeActivityStatus,
    RuntimeControlError,
    RuntimeControlSnapshot,
    WorkspaceControlState,
    WorkspaceKillState,
)


def _snapshot(
    *, ws_rev: int = 0, kill: bool = False, bot_rev: int = 0, paused: bool = False
) -> RuntimeControlSnapshot:
    return RuntimeControlSnapshot(
        workspace=WorkspaceControlState(
            ws_rev, WorkspaceKillState.KILLED if kill else WorkspaceKillState.ACTIVE
        )
        if ws_rev
        else None,
        bot=BotControlState(bot_rev, BotEntryState.PAUSED if paused else BotEntryState.ENABLED)
        if bot_rev
        else None,
    )


class _Controls:
    """Control reader giả trả lần lượt các snapshot; ghi lại session của từng lần đọc."""

    def __init__(self, *snapshots: RuntimeControlSnapshot, events: list[str]) -> None:
        self.snapshots = list(snapshots)
        self.sessions: list[object] = []
        self.events = events

    async def __call__(self, session: object, **_: object) -> RuntimeControlSnapshot:
        self.events.append("control")
        self.sessions.append(session)
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]


def _service(*snapshots: RuntimeControlSnapshot, **overrides: Any):
    events: list[str] = []
    controls = _Controls(*snapshots, events=events)
    built = _application_with_fakes(
        fail_submission=False, lock_events=events, control_reader=controls, **overrides
    )
    return built, controls, events


def test_entry_reads_control_inside_same_transaction_after_l1_before_any_write() -> None:
    (service, command, *_), controls, events = _service(_snapshot())
    _FakeUnitOfWork.instances = []

    asyncio.run(service.evaluate_and_submit(command))

    assert events.index("L1") < events.index("control")
    assert events.index("control") < events.index("L2-L4:reserve")
    assert events.index("control") < events.index("L4:submit")
    assert controls.sessions == [_FakeUnitOfWork.instances[0].session]


@pytest.mark.parametrize(
    ("snapshot", "code"),
    [
        (_snapshot(bot_rev=1, paused=True), "BOT_PAUSED"),
        (_snapshot(ws_rev=1, kill=True), "WORKSPACE_KILLED"),
    ],
)
def test_control_change_between_calls_blocks_next_entry_without_cache(
    snapshot: RuntimeControlSnapshot, code: str
) -> None:
    (service, command, _, _, counters, *_), _, _ = _service(_snapshot(), snapshot)

    asyncio.run(service.evaluate_and_submit(command))
    reserves_after_first = counters.reserves
    with pytest.raises(PaperRuntimeApplicationError, match=code):
        asyncio.run(service.evaluate_and_submit(command))

    assert reserves_after_first == 1
    assert counters.reserves == 1
    assert counters.submissions == 1


def test_control_read_failure_is_fail_closed() -> None:
    async def broken(_session: object, **_: object) -> RuntimeControlSnapshot:
        raise RuntimeControlError("RUNTIME_CONTROL_UNAVAILABLE")

    service, command, _, _, counters, *_ = _application_with_fakes(
        fail_submission=False, control_reader=broken
    )

    with pytest.raises(PaperRuntimeApplicationError, match="RUNTIME_CONTROL_UNAVAILABLE"):
        asyncio.run(service.evaluate_and_submit(command))
    assert counters.context_loads == 0
    assert counters.reserves == 0


def test_intent_and_reservation_evidence_record_control_revisions_read() -> None:
    (service, command, callback_state, _, counters, *_), _, _ = _service(
        _snapshot(ws_rev=2, bot_rev=5)
    )

    result = asyncio.run(service.evaluate_and_submit(command))

    expected = {"workspace": 2, "bot": 5}
    assert callback_state.submitted_evidence["runtime_control_revisions"] == expected
    assert counters.budget_kwargs["evidence"] == {"runtime_control_revisions": expected}
    assert result.runtime_control_revisions == expected


def _gate(**changes: Any):
    values = {
        "account_enabled": True,
        "bot_enabled": True,
        "readiness": BotReadiness.READY,
        "activity_status": RuntimeActivityStatus.VERIFIED,
        "activity_checked_at": datetime.now(UTC),
        "open_incident": False,
    } | changes

    async def loader(_uow: object, **_: object) -> EntryGateState:
        return EntryGateState(**values)

    return loader


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"bot_enabled": False}, "BOT_DISABLED"),
        ({"readiness": BotReadiness.RECOVERY_REQUIRED}, "BOT_RECOVERY_REQUIRED"),
        ({"open_incident": True}, "EXTERNAL_ACTIVITY_UNRECONCILED"),
        ({"activity_status": RuntimeActivityStatus.UNVERIFIED}, "ACCOUNT_ACTIVITY_UNVERIFIED"),
        (
            {"activity_checked_at": datetime.now(UTC) - timedelta(hours=1)},
            "ACCOUNT_ACTIVITY_UNVERIFIED",
        ),
    ],
)
def test_entry_gate_blocks_with_reason_and_writes_nothing(
    changes: dict[str, Any], code: str
) -> None:
    service, command, _, _, counters, *_ = _application_with_fakes(
        fail_submission=False, gate_state_loader=_gate(**changes)
    )

    with pytest.raises(PaperRuntimeApplicationError, match=code):
        asyncio.run(service.evaluate_and_submit(command))
    assert counters.context_loads == 0
    assert counters.reserves == 0
    assert counters.submissions == 0


class _StateRepository:
    def __init__(self, store: list[dict[str, Any]]) -> None:
        self.store = store

    def __call__(self, _session: object) -> _StateRepository:
        return self

    async def get(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(
            applied_workspace_control_revision=0, applied_bot_control_revision=0, reason_code=None
        )

    async def upsert(self, **values: Any) -> None:
        self.store.append(values)


def test_entry_event_under_pause_cancels_remainder_instead_of_filling() -> None:
    states: list[dict[str, Any]] = []
    (service, command, *_), _, events = _service(
        _snapshot(bot_rev=3, paused=True), state_repository_factory=_StateRepository(states)
    )
    account = service._active_accounts[command.account_id]
    paused_calls: list[dict[str, Any]] = []
    cancelled_order = SimpleNamespace(intent_id="paper:key-1", status=OrderStatus.CANCELLED)

    async def fill(*_: object, **__: object) -> None:
        raise AssertionError("Không được khớp entry khi bot đã pause")

    async def pause(uow: object, **kwargs: Any) -> SimpleNamespace:
        events.append("L4:pause")
        paused_calls.append(kwargs)
        return SimpleNamespace(
            gateway=SimpleNamespace(order=lambda _id: cancelled_order, position=lambda _id: None),
            cancelled_intent_ids=("paper:key-1",),
            released_reservation_intent_ids=(),
        )

    account.coordinator.apply_entry_market_event = fill
    account.coordinator.apply_entry_pause = pause
    opened = datetime.now(UTC)
    intent = SimpleNamespace(idempotency_key="key-1", symbol="BTCUSDT")
    _FakeUnitOfWork.scalar_results = [intent]
    bar = PaperBar(
        "bar-1",
        "BTCUSDT",
        opened,
        opened + timedelta(minutes=1),
        opened + timedelta(minutes=1),
        Decimal("100"),
        Decimal("101"),
        Decimal("99"),
        Decimal("100"),
    )

    result = asyncio.run(
        service.apply_entry_event(
            PaperEntryEventCommand(command.account_id, uuid4(), "paper:key-1", bar)
        )
    )

    assert result.order.status is OrderStatus.CANCELLED
    assert result.persisted_fill_count == 0
    assert paused_calls[0]["event_key"] == "control:pause:0:3"
    assert events.index("control") < events.index("L2-L3") < events.index("L4:pause")
    assert states[-1]["applied_bot_control_revision"] == 3


def test_monitor_position_never_reads_entry_control() -> None:
    import ast
    import inspect

    from trading_bot.application import paper_runtime_application

    tree = ast.parse(inspect.getsource(paper_runtime_application))
    service = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PaperRuntimeApplicationService"
    )
    monitor = next(
        node
        for node in service.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "monitor_position"
    )
    calls = {
        node.func.attr
        for node in ast.walk(monitor)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    # Exit/monitor chỉ kiểm fence/lease (ruling 14), không qua cổng entry/control.
    assert "_require_fence" in calls
    assert not calls & {"_read_control", "_enforce_entry_gate", "_apply_pause_in_uow"}


def test_reserve_budget_uses_single_coordinator_path_without_caller_caps() -> None:
    """Một đường reservation (MB-04 `reserve`): request từ engine decision, không truyền trần
    hay scope bot khác từ caller; chỉ thêm evidence revision control (ruling 27)."""
    captured: dict[str, Any] = {}

    class _Coordinator:
        def __init__(self, _session: object) -> None:
            pass

        async def reserve(self, request: Any, **kwargs: Any) -> Any:
            captured["request"] = request
            captured["kwargs"] = kwargs
            return SimpleNamespace(
                approved=False,
                reason_code="WORKSPACE_CAP_EXCEEDED",
                reservation_id=None,
                decision_id=uuid4(),
                decision=SimpleNamespace(approved=False),
            )

    service, command, _, _, counters, *_ = _application_with_fakes(
        fail_submission=False,
        workspace_risk_coordinator_factory=_Coordinator,
    )

    result = asyncio.run(service.evaluate_and_submit(command))

    request = captured["request"]
    assert request.requested_risk_usdt == Decimal("100")
    assert request.requested_notional == Decimal("1000")
    assert request.requested_initial_margin == Decimal("200")
    assert request.as_of == command.evaluated_at.astimezone(UTC)
    assert set(captured["kwargs"]) == {"evidence"}
    assert set(captured["kwargs"]["evidence"]) == {"runtime_control_revisions"}
    # Ngân sách MB-04 từ chối: không intent/submit, lý do trả về cho caller.
    assert result.entry_block_reason == "WORKSPACE_CAP_EXCEEDED"
    assert result.decision.outcome.value == "RISK_REJECTED"
    assert result.submission is None
    assert counters.submissions == 0


# --- Fix round 1 / I1: entry event sau khi pause đã được áp dụng -------------------------------


class _AppliedStates(_StateRepository):
    def __init__(self, store: list[dict[str, Any]], applied: tuple[int, int]) -> None:
        super().__init__(store)
        self.applied = applied

    async def get(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(
            applied_workspace_control_revision=self.applied[0],
            applied_bot_control_revision=self.applied[1],
            reason_code=None,
        )


def _entry_command(account_id: Any, paper_intent_id: str = "paper:key-1") -> Any:
    opened = datetime.now(UTC)
    bar = PaperBar(
        "bar-after-pause",
        "BTCUSDT",
        opened,
        opened + timedelta(minutes=1),
        opened + timedelta(minutes=1),
        Decimal("100"),
        Decimal("101"),
        Decimal("99"),
        Decimal("100"),
    )
    return PaperEntryEventCommand(account_id, uuid4(), paper_intent_id, bar)


def test_entry_event_after_supervisor_applied_pause_returns_cancelled_without_new_checkpoint():
    """I1: supervisor đã áp pause (revision khớp, gateway paused) -> entry event trả CANCELLED.

    Trước bản sửa: entry event gọi lại apply-pause, append checkpoint cùng event key
    `control:pause:<ws>:<bot>` với `event_at` khác -> `PaperCheckpointConflict` không được ánh xạ.
    """
    states: list[dict[str, Any]] = []
    (service, command, *_), _, _ = _service(
        _snapshot(bot_rev=3, paused=True),
        state_repository_factory=_AppliedStates(states, (0, 3)),
    )
    account = service._active_accounts[command.account_id]
    cancelled = SimpleNamespace(intent_id="paper:key-1", status=OrderStatus.CANCELLED)
    account.gateway.entries_paused = True
    account.gateway.order = lambda _id: cancelled
    account.gateway.position = lambda _id: None

    async def must_not_pause(*_: object, **__: object) -> None:
        raise AssertionError("Pause đã áp: không được append checkpoint control lần nữa")

    account.coordinator.apply_entry_pause = must_not_pause
    _FakeUnitOfWork.scalar_results = [SimpleNamespace(idempotency_key="key-1", symbol="BTCUSDT")]

    result = asyncio.run(service.apply_entry_event(_entry_command(command.account_id)))

    assert result.order.status is OrderStatus.CANCELLED
    assert result.persisted_fill_count == 0
    assert states == []


@pytest.mark.parametrize("path", ["fill", "pause"])
def test_checkpoint_conflict_is_mapped_to_command_rejected(path: str) -> None:
    from trading_bot.adapters.database.repositories.paper_checkpoints import (
        PaperCheckpointConflict,
    )

    states: list[dict[str, Any]] = []
    snapshot = _snapshot(bot_rev=1, paused=True) if path == "pause" else _snapshot()
    (service, command, *_), _, _ = _service(
        snapshot, state_repository_factory=_AppliedStates(states, (0, 0))
    )
    account = service._active_accounts[command.account_id]

    async def conflict(*_: object, **__: object) -> None:
        raise PaperCheckpointConflict("Checkpoint event key đã có payload khác.")

    account.coordinator.apply_entry_market_event = conflict
    account.coordinator.apply_entry_pause = conflict
    _FakeUnitOfWork.scalar_results = [SimpleNamespace(idempotency_key="key-1", symbol="BTCUSDT")]

    with pytest.raises(PaperRuntimeApplicationError, match="PAPER_COMMAND_REJECTED"):
        asyncio.run(service.apply_entry_event(_entry_command(command.account_id)))
