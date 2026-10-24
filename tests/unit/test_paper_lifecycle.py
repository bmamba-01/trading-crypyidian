from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from tests.unit.test_paper_execution import _plan

from trading_bot.application.paper_lifecycle import PaperLifecycleCoordinator
from trading_bot.domain.execution.paper import (
    PaperBar,
    PaperExecutionGateway,
    PaperExecutionPolicy,
)

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


class MemoryLedger:
    def __init__(self) -> None:
        self.entries: dict[str, tuple[object, ...]] = {}
        self.positions: dict[str, tuple[object, ...]] = {}
        self.exits: dict[str, tuple[object, ...]] = {}
        self.closed: list[tuple[UUID, Decimal, datetime]] = []
        self.reservation_transfers: list[tuple[UUID, UUID]] = []
        self.reservation_releases: list[UUID] = []
        self.reservation_unknown: list[UUID] = []
        self.order_transitions: list[tuple[UUID, str]] = []
        self.trade_id = uuid4()

    async def append_fill(self, _uow: object, **values: object) -> bool:
        fill_key = str(values["fill_key"])
        if fill_key in self.entries:
            assert self.entries[fill_key] == tuple(values.items())
            return False
        self.entries[fill_key] = tuple(values.items())
        return True

    async def append_position_event(self, _uow: object, **values: object) -> bool:
        event_key = str(values["event_key"])
        if event_key in self.positions:
            assert self.positions[event_key] == tuple(values.items())
            return False
        self.positions[event_key] = tuple(values.items())
        return True

    async def open_trade(self, _uow: object, **_values: object) -> SimpleNamespace:
        return SimpleNamespace(trade_id=self.trade_id, duplicate=bool(self.entries))

    async def record_order_transition(self, _uow: object, **values: object) -> bool:
        self.order_transitions.append((values["intent_id"], str(values["status"])))
        return True

    async def transfer_reservation_to_open_trade(
        self, _uow: object, *, intent_id: UUID, trade_id: UUID
    ) -> None:
        self.reservation_transfers.append((intent_id, trade_id))

    async def release_reservation_for_terminal_no_fill(
        self, _uow: object, *, intent_id: UUID
    ) -> None:
        self.reservation_releases.append(intent_id)

    async def mark_reservation_unknown(self, _uow: object, *, intent_id: UUID) -> None:
        self.reservation_unknown.append(intent_id)

    async def append_exit_fill(self, _uow: object, **values: object) -> bool:
        fill_key = str(values["fill_key"])
        if fill_key in self.exits:
            assert self.exits[fill_key] == tuple(values.items())
            return False
        self.exits[fill_key] = tuple(values.items())
        return True

    async def close_trade(
        self, _uow: object, *, trade_id: UUID, gross_pnl: Decimal, closed_at: datetime
    ) -> SimpleNamespace:
        self.closed.append((trade_id, gross_pnl, closed_at))
        return SimpleNamespace(id=trade_id, status="CLOSED")


def _gateway() -> PaperExecutionGateway:
    return PaperExecutionGateway(
        PaperExecutionPolicy(
            version_id="paper-profile-a",
            latency=timedelta(0),
            spread_rate=Decimal("0.001"),
            slippage_rate=Decimal("0.002"),
            fee_rate=Decimal("0.001"),
            max_fill_quantity_per_event=Decimal("0.4"),
        )
    )


def _bar(
    bar_key: str,
    minute: int,
    *,
    high: str = "101",
    low: str = "99",
    close: str = "100",
) -> PaperBar:
    opened = NOW + timedelta(minutes=minute)
    return PaperBar(
        bar_key=bar_key,
        symbol="BTCUSDT",
        opened_at=opened,
        closed_at=opened + timedelta(minutes=1),
        available_at=opened + timedelta(minutes=1),
        open=Decimal("100"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
    )


@pytest.mark.asyncio
async def test_entry_event_persists_fill_position_and_trade_once_on_retry() -> None:
    gateway = _gateway()
    domain_order = gateway.submit(_plan(), "life-entry-1", submitted_at=NOW, eligible_after=NOW)
    ledger = MemoryLedger()
    coordinator = PaperLifecycleCoordinator(gateway, ledger)  # type: ignore[arg-type]
    uow = object()
    ledger_intent_id = uuid4()
    bar = _bar("entry-bar-1", 1)

    first = await coordinator.apply_entry_market_event(
        uow,
        paper_intent_id=domain_order.intent_id,
        ledger_intent_id=ledger_intent_id,
        bar=bar,
        available_quantity=Decimal("0.4"),
    )
    retry = await coordinator.apply_entry_market_event(
        uow,
        paper_intent_id=domain_order.intent_id,
        ledger_intent_id=ledger_intent_id,
        bar=bar,
        available_quantity=Decimal("0.4"),
    )

    assert first.position is not None
    assert first.position.quantity == Decimal("0.4")
    assert first.trade_id == retry.trade_id == ledger.trade_id
    assert first.persisted_fill_count == 1
    assert retry.persisted_fill_count == 0
    assert len(ledger.entries) == 1
    assert len(ledger.positions) == 1
    assert ledger.reservation_transfers == [
        (ledger_intent_id, ledger.trade_id),
    ]
    entry = next(iter(ledger.entries.values()))
    assert dict(entry)["intent_id"] == ledger_intent_id


@pytest.mark.asyncio
async def test_expired_no_fill_persists_terminal_status_and_releases_reservation() -> None:
    gateway = _gateway()
    domain_order = gateway.submit(_plan(), "life-expired-no-fill", submitted_at=NOW)
    ledger = MemoryLedger()
    coordinator = PaperLifecycleCoordinator(gateway, ledger)  # type: ignore[arg-type]
    ledger_intent_id = uuid4()

    result = await coordinator.apply_entry_market_event(
        object(),
        paper_intent_id=domain_order.intent_id,
        ledger_intent_id=ledger_intent_id,
        bar=_bar("expired-before-fill", 11),
    )

    assert result.order.status.value == "EXPIRED"
    assert ledger.order_transitions == [(ledger_intent_id, "EXPIRED")]
    assert ledger.reservation_releases == [ledger_intent_id]
    assert ledger.reservation_transfers == []


@pytest.mark.asyncio
async def test_monitor_persists_exit_and_closes_trade_idempotently() -> None:
    gateway = _gateway()
    domain_order = gateway.submit(_plan(), "life-entry-2", submitted_at=NOW, eligible_after=NOW)
    ledger = MemoryLedger()
    coordinator = PaperLifecycleCoordinator(gateway, ledger)  # type: ignore[arg-type]
    uow = object()
    ledger_intent_id = uuid4()
    entry = await coordinator.apply_entry_market_event(
        uow,
        paper_intent_id=domain_order.intent_id,
        ledger_intent_id=ledger_intent_id,
        bar=_bar("entry-bar-2", 1),
        available_quantity=Decimal("0.4"),
    )
    assert entry.position is not None
    open_position = entry.position
    stop_bar = _bar("stop-bar-2", 2, low="95")

    first = await coordinator.monitor_market_event(
        uow,
        paper_position_id=domain_order.intent_id,
        ledger_intent_id=ledger_intent_id,
        trade_id=ledger.trade_id,
        position=open_position,
        bar=stop_bar,
    )
    retry = await coordinator.monitor_market_event(
        uow,
        paper_position_id=domain_order.intent_id,
        ledger_intent_id=ledger_intent_id,
        trade_id=ledger.trade_id,
        position=open_position,
        bar=stop_bar,
    )

    assert first.position.is_closed
    assert first.exit_fill is not None
    assert first.exit_fill_appended
    assert first.trade_closed
    assert retry.position.is_closed
    assert retry.exit_fill is not None
    assert not retry.exit_fill_appended
    assert not retry.trade_closed
    assert len(ledger.exits) == 1
    assert len(ledger.closed) == 1
    closed_trade_id, gross_pnl, closed_at = ledger.closed[0]
    assert closed_trade_id == ledger.trade_id
    assert isinstance(gross_pnl, Decimal)
    assert closed_at == stop_bar.closed_at


@pytest.mark.asyncio
async def test_monitor_persists_trailing_stop_transition_without_exit() -> None:
    gateway = _gateway()
    plan = _plan()
    plan = replace(
        plan,
        exit_recipe=replace(plan.exit_recipe, trailing_atr_multiple=2, max_hold_bars=5),
    )
    domain_order = gateway.submit(plan, "life-entry-3", submitted_at=NOW, eligible_after=NOW)
    ledger = MemoryLedger()
    coordinator = PaperLifecycleCoordinator(gateway, ledger)  # type: ignore[arg-type]
    uow = object()
    entry = await coordinator.apply_entry_market_event(
        uow,
        paper_intent_id=domain_order.intent_id,
        ledger_intent_id=uuid4(),
        bar=_bar("entry-bar-3", 1),
        available_quantity=Decimal("0.4"),
    )
    assert entry.position is not None

    result = await coordinator.monitor_market_event(
        uow,
        paper_position_id=domain_order.intent_id,
        ledger_intent_id=uuid4(),
        trade_id=ledger.trade_id,
        position=entry.position,
        bar=_bar("trail-bar-3", 2, high="110", low="99"),
        atr=Decimal("1"),
    )

    assert not result.position.is_closed
    assert result.exit_fill is None
    assert result.position.stop_price > entry.position.stop_price
    assert any("stop-update" in key for key in ledger.positions)
