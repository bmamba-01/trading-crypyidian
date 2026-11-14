from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from trading_bot.adapters.binance_usdm.market_stream import KlineStreamDisconnected
from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
)
from trading_bot.application.live_kline_ingestion import LiveIngestionError
from trading_bot.application.live_market_events import LiveMarketEventService
from trading_bot.domain.market.stream_events import (
    AggTradeEvent,
    LiquidationEvent,
    MarkPriceEvent,
)
from trading_bot.domain.market.trade_flow import TradeFlowBucket
from trading_bot.workers.live_market_ingestion_worker import LiveMarketEventWorker

WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")
T0 = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)


def mark(offset: float, *, symbol: str = "BTCUSDT") -> MarkPriceEvent:
    at = T0 + timedelta(seconds=offset)
    return MarkPriceEvent(
        symbol=symbol,
        event_at=at,
        mark_price=Decimal("100"),
        index_price=Decimal("100.1"),
        funding_rate=Decimal("0.0001"),
        next_funding_time=T0 + timedelta(hours=1),
        received_at=at + timedelta(milliseconds=20),
        checksum=hashlib.sha256(f"mark{offset}{symbol}".encode()).hexdigest(),
    )


def liquidation(offset: float) -> LiquidationEvent:
    at = T0 + timedelta(seconds=offset)
    return LiquidationEvent(
        symbol="BTCUSDT",
        side="SELL",
        order_type="LIMIT",
        time_in_force="IOC",
        status="FILLED",
        price=Decimal("99"),
        average_price=Decimal("99.5"),
        quantity=Decimal("2"),
        filled_quantity=Decimal("2"),
        trade_time=at,
        event_at=at,
        received_at=at + timedelta(milliseconds=20),
        checksum=hashlib.sha256(f"liq{offset}".encode()).hexdigest(),
    )


def agg(aggregate_id: int, offset: float) -> AggTradeEvent:
    at = T0 + timedelta(seconds=offset)
    return AggTradeEvent(
        symbol="BTCUSDT",
        aggregate_id=aggregate_id,
        price=Decimal("100"),
        quantity=Decimal("1"),
        first_trade_id=aggregate_id,
        last_trade_id=aggregate_id,
        trade_time=at,
        buyer_is_maker=False,
        received_at=at + timedelta(milliseconds=20),
    )


def flow(offset_minutes: int, *, complete: bool = True) -> TradeFlowBucket:
    start = T0 + timedelta(minutes=offset_minutes)
    return TradeFlowBucket(
        symbol="BTCUSDT",
        bucket_start=start,
        bucket_seconds=60,
        buy_quantity=Decimal("3"),
        sell_quantity=Decimal("1"),
        buy_quote=Decimal("300"),
        sell_quote=Decimal("100"),
        trade_count=4,
        first_aggregate_id=10,
        last_aggregate_id=13,
        is_complete=complete,
        closed_at=start + timedelta(seconds=61),
    )


@dataclass
class FakeInstrument:
    id: UUID


@dataclass
class Store:
    instruments: dict[str, UUID] = field(default_factory=lambda: {"BTCUSDT": uuid4()})
    rows: dict[str, tuple[str, Any]] = field(default_factory=dict)
    commits: int = 0


class FakeRepository:
    def __init__(self, store: Store) -> None:
        self._store = store
        self.pending: dict[str, tuple[str, Any]] = {}

    async def get_instrument(self, *, workspace_id: UUID, source: str, symbol: str, as_of: Any):
        assert source == "BINANCE_USDM"
        instrument_id = self._store.instruments.get(symbol)
        return None if instrument_id is None else FakeInstrument(instrument_id)

    async def _append(self, kind: str, key: str, value: Any) -> bool:
        assert len(key) <= 240
        if key in self._store.rows or key in self.pending:
            return False
        self.pending[key] = (kind, value)
        return True

    async def append_mark_price(
        self, *, workspace_id, instrument_id, source, idempotency_key, event
    ):
        return await self._append("mark_price", idempotency_key, event)

    async def append_liquidation(
        self, *, workspace_id, instrument_id, source, idempotency_key, event
    ):
        return await self._append("liquidation", idempotency_key, event)

    async def append_trade_flow(
        self, *, workspace_id, instrument_id, source, idempotency_key, checksum, bucket
    ):
        assert len(checksum) == 64
        return await self._append("trade_flow", idempotency_key, bucket)


class FakeRights:
    def __init__(self) -> None:
        self.deny = False
        self.requests: list[Any] = []

    async def authorize(self, *, request: Any, now: datetime) -> BackfillRightsEvidence:
        self.requests.append(request)
        if self.deny:
            raise PermissionError("Policy hiện hành không cấp đúng mục đích backfill.")
        return BackfillRightsEvidence(
            workspace_id=WORKSPACE_ID,
            provider="BINANCE_USDM_REST",
            purpose=request.purpose,
            terms_url="https://www.binance.com/en/terms",
            policy_fingerprint=hashlib.sha256(b"policy").hexdigest(),
            verified_at=now - timedelta(hours=1),
            valid_until=now + timedelta(minutes=5),
        )


@dataclass
class FakeTransaction:
    market: FakeRepository
    rights: FakeRights


class Harness:
    def __init__(self) -> None:
        self.store = Store()
        self.rights = FakeRights()

    @asynccontextmanager
    async def transactions(self) -> Any:
        repository = FakeRepository(self.store)
        yield FakeTransaction(repository, self.rights)
        self.store.rows.update(repository.pending)
        self.store.commits += 1

    def service(self) -> LiveMarketEventService:
        return LiveMarketEventService(workspace_id=WORKSPACE_ID, transactions=self.transactions)

    def kinds(self) -> list[str]:
        return sorted(kind for kind, _ in self.store.rows.values())


@pytest.mark.asyncio
async def test_write_persists_each_kind_once_in_one_transaction() -> None:
    harness = Harness()
    service = harness.service()
    batch = [mark(1), liquidation(2), flow(0)]

    written = await service.write(batch, now=T0 + timedelta(seconds=70))
    again = await service.write(batch, now=T0 + timedelta(seconds=71))

    assert written == 3
    assert again == 0
    assert harness.kinds() == ["liquidation", "mark_price", "trade_flow"]
    assert harness.store.commits == 2
    assert {request.purpose for request in harness.rights.requests} == {
        BackfillRightsPurpose.PAPER_DEMO
    }
    assert {request.symbol for request in harness.rights.requests} == {"BTCUSDT"}


@pytest.mark.asyncio
async def test_two_partial_buckets_of_the_same_minute_are_both_kept() -> None:
    harness = Harness()
    first = flow(0, complete=False)
    second = TradeFlowBucket(
        **{
            **{name: getattr(first, name) for name in first.__slots__},
            "first_aggregate_id": 20,
            "last_aggregate_id": 22,
        }
    )

    assert await harness.service().write([first, second], now=T0 + timedelta(minutes=2)) == 2


@pytest.mark.asyncio
async def test_rights_denial_writes_nothing() -> None:
    harness = Harness()
    harness.rights.deny = True

    with pytest.raises(PermissionError):
        await harness.service().write([mark(1)], now=T0 + timedelta(seconds=5))
    with pytest.raises(PermissionError):
        await harness.service().ensure_ready(("BTCUSDT",), now=T0)

    assert harness.store.rows == {}


@pytest.mark.asyncio
async def test_unknown_instrument_fails_closed() -> None:
    harness = Harness()

    with pytest.raises(LiveIngestionError):
        await harness.service().ensure_ready(("ETHUSDT",), now=T0)
    with pytest.raises(LiveIngestionError):
        await harness.service().write([mark(1, symbol="ETHUSDT")], now=T0 + timedelta(seconds=5))

    assert harness.store.rows == {}


class FakeEventService:
    def __init__(self) -> None:
        self.batches: list[list[Any]] = []
        self.ready_calls = 0
        self.ready_error: BaseException | None = None

    async def ensure_ready(self, symbols: Any, *, now: datetime) -> None:
        self.ready_calls += 1
        if self.ready_error is not None:
            raise self.ready_error

    async def write(self, events: Any, *, now: datetime) -> int:
        self.batches.append(list(events))
        return len(events)

    @property
    def written(self) -> list[Any]:
        return [event for batch in self.batches for event in batch]


class FakeEventStream:
    def __init__(self, events: list[Any], *, hold_open: bool = False) -> None:
        self._events = events
        self._hold_open = hold_open

    async def events(self) -> AsyncGenerator[Any]:
        for event in self._events:
            yield event
        if self._hold_open:
            await asyncio.Event().wait()
        raise KlineStreamDisconnected("KLINE_STREAM_WEBSOCKETDISCONNECT")


def build_worker(service: FakeEventService, streams: list[FakeEventStream], clock: dict) -> Any:
    remaining = list(streams)
    return LiveMarketEventWorker(
        service=service,
        stream_factory=lambda: remaining.pop(0),
        symbols=("BTCUSDT",),
        now=lambda: clock["now"],
        mark_price_interval_seconds=60,
        flush_seconds=0,
        reconnect_seconds=0.01,
    )


@pytest.mark.asyncio
async def test_worker_samples_mark_price_once_per_interval_and_keeps_every_liquidation() -> None:
    service = FakeEventService()
    clock = {"now": T0 + timedelta(seconds=130)}
    stream = FakeEventStream(
        [mark(1), mark(2), liquidation(3), mark(59), liquidation(59.5), mark(60), mark(61)]
    )
    worker = build_worker(service, [stream], clock)

    with pytest.raises(KlineStreamDisconnected):
        await worker.run_session()

    marks = [event for event in service.written if isinstance(event, MarkPriceEvent)]
    liquidations = [event for event in service.written if isinstance(event, LiquidationEvent)]
    assert [event.event_at for event in marks] == [
        T0 + timedelta(seconds=1),
        T0 + timedelta(seconds=60),
    ]
    assert len(liquidations) == 2
    assert worker.events_written == 4


@pytest.mark.asyncio
async def test_worker_aggregates_trades_and_marks_cut_bucket_partial_on_disconnect() -> None:
    service = FakeEventService()
    clock = {"now": T0 + timedelta(seconds=200)}
    stream = FakeEventStream([agg(1, 59), agg(2, 61), agg(3, 62), agg(4, 121), agg(5, 122)])
    worker = build_worker(service, [stream], clock)

    with pytest.raises(KlineStreamDisconnected):
        await worker.run_session()

    buckets = [event for event in service.written if isinstance(event, TradeFlowBucket)]
    assert [
        (bucket.bucket_start, bucket.is_complete, bucket.trade_count) for bucket in buckets
    ] == [
        (T0, False, 1),
        (T0 + timedelta(minutes=1), True, 2),
        (T0 + timedelta(minutes=2), False, 2),
    ]


@pytest.mark.asyncio
async def test_worker_run_reconnects_and_reports_rights_denial() -> None:
    service = FakeEventService()
    service.ready_error = PermissionError("denied")
    clock = {"now": T0 + timedelta(seconds=10)}
    worker = build_worker(service, [FakeEventStream([liquidation(1)], hold_open=True)], clock)
    stopping = asyncio.Event()

    task = asyncio.create_task(worker.run(stopping))
    for _ in range(200):
        if worker.last_error_code is not None:
            break
        await asyncio.sleep(0.01)
    assert worker.last_error_code == "LIVE_INGEST_RIGHTS_DENIED"
    assert worker.is_healthy is False
    service.ready_error = None
    for _ in range(200):
        if worker.events_written == 1:
            break
        await asyncio.sleep(0.01)
    assert worker.events_written == 1
    assert worker.is_healthy is True
    stopping.set()
    await asyncio.wait_for(task, timeout=1)
