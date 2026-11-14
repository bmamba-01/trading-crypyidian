from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from trading_bot.adapters.binance_usdm.market_stream import KlineStreamDisconnected
from trading_bot.application.live_kline_ingestion import (
    CatchUpResult,
    LiveBarDisposition,
    LiveKlineSubscription,
)
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.workers.live_market_ingestion_worker import LiveMarketIngestionWorker

INTERVAL = timedelta(minutes=15)
T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
SUB = LiveKlineSubscription("BTCUSDT", "15m")


def make_bar(open_time: datetime, *, closed: bool) -> OHLCVBar:
    close_time = open_time + INTERVAL - timedelta(milliseconds=1)
    received = close_time + timedelta(seconds=1) if closed else open_time + timedelta(minutes=1)
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe="15m",
        open_time=open_time,
        close_time=close_time,
        event_at=min(close_time, received),
        received_at=received,
        available_at=received,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("1"),
        quote_volume=Decimal("100"),
        is_closed=closed,
        source="BINANCE_USDM",
    )


class FakeService:
    def __init__(self, *, covered_until: datetime = T0) -> None:
        self.covered_until = covered_until
        self.rest_available_until: datetime | None = None
        self.calls: list[tuple[str, datetime | None]] = []
        self.catch_up_error: BaseException | None = None

    async def catch_up(
        self, subscription: LiveKlineSubscription, *, now: datetime
    ) -> CatchUpResult:
        self.calls.append(("catch_up", None))
        if self.catch_up_error is not None:
            raise self.catch_up_error
        appended = 0
        if self.rest_available_until is not None:
            while self.covered_until < self.rest_available_until:
                self.covered_until += INTERVAL
                appended += 1
        return CatchUpResult(subscription, appended, self.covered_until, True)

    async def ingest_closed_bar(self, bar: OHLCVBar, *, now: datetime) -> LiveBarDisposition:
        self.calls.append(("ingest", bar.open_time))
        if bar.open_time < self.covered_until:
            return LiveBarDisposition.DUPLICATE
        if bar.open_time > self.covered_until:
            return LiveBarDisposition.NEEDS_CATCH_UP
        self.covered_until += INTERVAL
        return LiveBarDisposition.APPENDED


class FakeStream:
    def __init__(self, bars: list[OHLCVBar], *, hold_open: bool = False) -> None:
        self._bars = bars
        self._hold_open = hold_open
        self.closed = False

    async def bars(self) -> AsyncGenerator[OHLCVBar]:
        try:
            for bar in self._bars:
                yield bar
            if self._hold_open:
                await asyncio.Event().wait()
            raise KlineStreamDisconnected("KLINE_STREAM_WEBSOCKETDISCONNECT")
        finally:
            self.closed = True


def build_worker(service: FakeService, streams: list[FakeStream], **kwargs: Any) -> Any:
    remaining = list(streams)
    clock = {"monotonic": 0.0}

    def monotonic() -> float:
        clock["monotonic"] += 10.0
        return clock["monotonic"]

    return LiveMarketIngestionWorker(
        service=service,
        stream_factory=lambda: remaining.pop(0),
        subscriptions=(SUB,),
        now=lambda: T0 + timedelta(hours=1),
        reconnect_seconds=0.01,
        monotonic=kwargs.pop("monotonic", monotonic),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_session_catches_up_then_appends_only_closed_bars_in_order() -> None:
    service = FakeService()
    stream = FakeStream(
        [
            make_bar(T0, closed=False),
            make_bar(T0, closed=True),
            make_bar(T0 + INTERVAL, closed=False),
            make_bar(T0 + INTERVAL, closed=True),
        ]
    )
    worker = build_worker(service, [stream])

    with pytest.raises(KlineStreamDisconnected):
        await worker.run_session()

    assert service.calls == [("catch_up", None), ("ingest", T0), ("ingest", T0 + INTERVAL)]
    assert worker.bars_appended == 2
    assert stream.closed is True


@pytest.mark.asyncio
async def test_update_for_a_later_bar_triggers_rest_catch_up_for_the_missed_close() -> None:
    service = FakeService()
    stream = FakeStream(
        [
            make_bar(T0 + INTERVAL, closed=False),
            make_bar(T0 + INTERVAL, closed=True),
        ]
    )

    def open_stream() -> FakeStream:
        # Nến T0 đóng sau lần lấp đầu tiên, trong lúc stream chưa kịp nối.
        service.rest_available_until = T0 + INTERVAL
        return stream

    worker = LiveMarketIngestionWorker(
        service=service,
        stream_factory=open_stream,
        subscriptions=(SUB,),
        now=lambda: T0 + timedelta(hours=1),
    )

    with pytest.raises(KlineStreamDisconnected):
        await worker.run_session()

    assert service.calls == [
        ("catch_up", None),
        ("catch_up", None),
        ("ingest", T0 + INTERVAL),
    ]
    assert service.covered_until == T0 + 2 * INTERVAL


@pytest.mark.asyncio
async def test_catch_up_is_throttled_while_rest_lags_behind_the_stream() -> None:
    service = FakeService()
    stream = FakeStream([make_bar(T0 + INTERVAL, closed=False) for _ in range(5)])
    worker = build_worker(service, [stream], monotonic=lambda: 100.0)

    with pytest.raises(KlineStreamDisconnected):
        await worker.run_session()

    assert service.calls == [("catch_up", None), ("catch_up", None)]


@pytest.mark.asyncio
async def test_stale_stream_bars_are_ignored() -> None:
    service = FakeService()
    stream = FakeStream([make_bar(T0 - INTERVAL, closed=True)])
    worker = build_worker(service, [stream])

    with pytest.raises(KlineStreamDisconnected):
        await worker.run_session()

    assert service.calls == [("catch_up", None)]


@pytest.mark.asyncio
async def test_run_reconnects_after_disconnect_and_stops_cleanly() -> None:
    service = FakeService()
    first = FakeStream([make_bar(T0, closed=True)])
    second = FakeStream([make_bar(T0 + INTERVAL, closed=True)], hold_open=True)
    worker = build_worker(service, [first, second])
    stopping = asyncio.Event()

    task = asyncio.create_task(worker.run(stopping))
    for _ in range(200):
        if worker.bars_appended == 2:
            break
        await asyncio.sleep(0.01)
    assert worker.bars_appended == 2
    assert worker.is_healthy is True
    stopping.set()
    await asyncio.wait_for(task, timeout=1)

    assert first.closed is True
    assert second.closed is True
    assert [name for name, _ in service.calls].count("catch_up") == 2


@pytest.mark.asyncio
async def test_rights_denial_marks_worker_unhealthy_without_opening_stream() -> None:
    service = FakeService()
    service.catch_up_error = PermissionError("Policy hiện hành chưa được verified.")
    opened: list[int] = []

    def stream_factory() -> FakeStream:
        opened.append(1)
        return FakeStream([])

    worker = LiveMarketIngestionWorker(
        service=service,
        stream_factory=stream_factory,
        subscriptions=(SUB,),
        now=lambda: T0,
        reconnect_seconds=0.01,
    )
    stopping = asyncio.Event()

    task = asyncio.create_task(worker.run(stopping))
    for _ in range(200):
        if worker.last_error_code is not None:
            break
        await asyncio.sleep(0.01)
    stopping.set()
    await asyncio.wait_for(task, timeout=1)

    assert worker.last_error_code == "LIVE_INGEST_RIGHTS_DENIED"
    assert worker.is_healthy is False
    assert opened == []


def test_worker_rejects_empty_or_duplicate_subscriptions() -> None:
    service = FakeService()
    for subscriptions in ((), (SUB, SUB)):
        with pytest.raises(ValueError):  # noqa: PT011
            LiveMarketIngestionWorker(
                service=service,
                stream_factory=lambda: FakeStream([]),
                subscriptions=subscriptions,
            )
