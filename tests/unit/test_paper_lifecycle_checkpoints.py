from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from tests.unit.test_paper_execution import _plan

from trading_bot.application.paper_lifecycle import (
    PaperLifecycleCoordinator,
    _market_event_key,
)
from trading_bot.domain.execution.paper import PaperBar, PaperExecutionGateway, PaperExecutionPolicy
from trading_bot.domain.execution.paper_state import PaperExecutionSnapshot

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


@dataclass
class Checkpoint:
    revision: int
    event_key: str
    event_hash: str
    event_scope: str
    event_at: datetime
    snapshot: PaperExecutionSnapshot


class MemoryCheckpoints:
    def __init__(self, snapshot: PaperExecutionSnapshot) -> None:
        self.rows = [Checkpoint(1, "seed", "0" * 64, "seed", NOW, snapshot)]

    async def lock_account(self, _uow: object, *, account_id: object) -> None:
        assert account_id is not None

    async def load_latest(self, _uow: object, *, account_id: object):
        del account_id
        latest = self.rows[-1]
        return latest.revision, latest.snapshot

    async def verify_ledger_consistency(
        self, _uow: object, *, account_id: object, snapshot: PaperExecutionSnapshot
    ) -> None:
        assert account_id is not None
        assert snapshot is not None

    async def append(
        self,
        _uow: object,
        *,
        account_id: object,
        event_key: str,
        event_hash: str,
        snapshot: PaperExecutionSnapshot,
        occurred_at: datetime,
        event_scope: str | None = None,
    ):
        del account_id
        for row in self.rows:
            if row.event_key == event_key:
                assert (row.event_hash, row.event_scope, row.event_at) == (
                    event_hash,
                    event_scope,
                    occurred_at,
                )
                return SimpleNamespace(
                    revision=self.rows[-1].revision,
                    snapshot=self.rows[-1].snapshot,
                    duplicate=True,
                )
        row = Checkpoint(
            self.rows[-1].revision + 1,
            event_key,
            event_hash,
            event_scope or "",
            occurred_at,
            snapshot,
        )
        self.rows.append(row)
        return SimpleNamespace(revision=row.revision, snapshot=snapshot, duplicate=False)


class CallbackUow:
    def __init__(self) -> None:
        self.callbacks = []

    def after_commit(self, callback):
        self.callbacks.append(callback)

    def commit(self) -> None:
        for callback in self.callbacks:
            callback()
        self.callbacks.clear()


class MemoryLedger:
    def __init__(self) -> None:
        self.fills: set[str] = set()
        self.events: set[str] = set()
        self.transfers: list[tuple[str, object]] = []
        self.releases: list[str] = []
        self.unknown: list[str] = []

    async def append_fill(self, _uow: object, **values: object) -> bool:
        key = str(values["fill_key"])
        created = key not in self.fills
        self.fills.add(key)
        return created

    async def append_position_event(self, _uow: object, **values: object) -> bool:
        key = str(values["event_key"])
        created = key not in self.events
        self.events.add(key)
        return created

    async def open_trade(self, _uow: object, **_values: object):
        return SimpleNamespace(trade_id=uuid4())

    async def record_order_transition(self, _uow: object, **_values: object) -> bool:
        return True

    async def transfer_reservation_to_open_trade(
        self, _uow: object, *, intent_id: str, trade_id: object
    ) -> None:
        self.transfers.append((intent_id, trade_id))

    async def release_reservation_for_terminal_no_fill(
        self, _uow: object, *, intent_id: str
    ) -> None:
        self.releases.append(intent_id)

    async def mark_reservation_unknown(self, _uow: object, *, intent_id: str) -> None:
        self.unknown.append(intent_id)


def _gateway() -> PaperExecutionGateway:
    return PaperExecutionGateway(
        PaperExecutionPolicy(
            version_id="paper-profile-checkpoint",
            latency=timedelta(0),
            spread_rate=Decimal("0.001"),
            slippage_rate=Decimal("0.002"),
            fee_rate=Decimal("0.001"),
        )
    )


def _bar() -> PaperBar:
    return PaperBar(
        bar_key="shared-bar",
        symbol="BTCUSDT",
        opened_at=NOW + timedelta(minutes=1),
        closed_at=NOW + timedelta(minutes=2),
        available_at=NOW + timedelta(minutes=2),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
    )


def test_market_event_checkpoint_key_stays_within_db_limit_for_maximum_identity() -> None:
    long_bar = PaperBar(
        bar_key="b" * 240,
        symbol="S" * 64,
        opened_at=NOW + timedelta(minutes=1),
        closed_at=NOW + timedelta(minutes=2),
        available_at=NOW + timedelta(minutes=2),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
    )

    key = _market_event_key("market", "i" * 240, long_bar)

    assert len(key) < 240
    assert key == _market_event_key("market", "i" * 240, long_bar)


@pytest.mark.asyncio
async def test_account_checkpoint_resumes_second_intent_and_historical_retry_loads_latest() -> None:
    account_id = uuid4()
    first_ledger_intent = uuid4()
    second_ledger_intent = uuid4()
    initial_gateway = _gateway()
    first = initial_gateway.submit(_plan(), "first-same-bar", submitted_at=NOW, eligible_after=NOW)
    second = initial_gateway.submit(
        _plan(), "second-same-bar", submitted_at=NOW, eligible_after=NOW
    )
    checkpoints = MemoryCheckpoints(initial_gateway.export_state())
    ledger = MemoryLedger()
    live_gateway = _gateway()
    coordinator = PaperLifecycleCoordinator(
        live_gateway,
        ledger,
        checkpoints,  # type: ignore[arg-type]
    )

    first_uow = CallbackUow()
    first_result = await coordinator.apply_entry_market_event(
        first_uow,  # type: ignore[arg-type]
        account_id=account_id,
        paper_intent_id=first.intent_id,
        ledger_intent_id=first_ledger_intent,
        bar=_bar(),
        available_quantity=Decimal("0.4"),
    )
    assert first_result.order.filled_quantity == Decimal("0.4")
    assert live_gateway.export_state() != checkpoints.rows[-1].snapshot
    # Simulate process death after DB commit and before the in-memory callback.

    restarted_gateway = _gateway()
    restarted = PaperLifecycleCoordinator(
        restarted_gateway,
        ledger,
        checkpoints,  # type: ignore[arg-type]
    )
    hydrate_uow = CallbackUow()
    await restarted.hydrate_account(hydrate_uow, account_id=account_id)  # type: ignore[arg-type]
    hydrate_uow.commit()
    assert restarted_gateway.order(first.intent_id).filled_quantity == Decimal("0.4")

    second_uow = CallbackUow()
    second_result = await restarted.apply_entry_market_event(
        second_uow,  # type: ignore[arg-type]
        account_id=account_id,
        paper_intent_id=second.intent_id,
        ledger_intent_id=second_ledger_intent,
        bar=_bar(),
        available_quantity=Decimal("0.4"),
    )
    second_uow.commit()
    latest = checkpoints.rows[-1].snapshot

    retry_uow = CallbackUow()
    retry = await restarted.apply_entry_market_event(
        retry_uow,  # type: ignore[arg-type]
        account_id=account_id,
        paper_intent_id=first.intent_id,
        ledger_intent_id=first_ledger_intent,
        bar=_bar(),
        available_quantity=Decimal("0.4"),
    )
    retry_uow.commit()

    assert second_result.order.filled_quantity == Decimal("0.4")
    assert retry.persisted_fill_count == 0
    assert checkpoints.rows[-1].revision == 3
    assert restarted_gateway.export_state() == latest
    assert len(ledger.fills) == 2


@pytest.mark.asyncio
async def test_lifecycle_fails_closed_when_checkpoint_policy_changed() -> None:
    account_id = uuid4()
    baseline = _gateway()
    order = baseline.submit(_plan(), "policy-drift-order", submitted_at=NOW, eligible_after=NOW)
    checkpoints = MemoryCheckpoints(baseline.export_state())
    changed_policy = PaperExecutionPolicy(
        version_id="paper-profile-changed",
        latency=timedelta(0),
        spread_rate=Decimal("0.001"),
        slippage_rate=Decimal("0.002"),
        fee_rate=Decimal("0.003"),
    )
    gateway = PaperExecutionGateway(changed_policy)
    ledger = MemoryLedger()
    coordinator = PaperLifecycleCoordinator(
        gateway,
        ledger,
        checkpoints,  # type: ignore[arg-type]
    )
    uow = CallbackUow()

    with pytest.raises(RuntimeError, match="Checkpoint PAPER sai policy"):
        await coordinator.apply_entry_market_event(
            uow,  # type: ignore[arg-type]
            account_id=account_id,
            paper_intent_id=order.intent_id,
            ledger_intent_id=uuid4(),
            bar=_bar(),
            available_quantity=Decimal("0.4"),
        )

    assert not ledger.fills
    assert not uow.callbacks
