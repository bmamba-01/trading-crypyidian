"""Unit test SIG-0: collector nến công khai Binance cho PAPER nội bộ."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from trading_bot.app import _market_collector_status
from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.application.market_bar_collector import (
    COLLECTOR_CURSOR_SOURCE,
    CollectorStream,
    MarketBarCollector,
    last_closed_boundary,
    parse_collector_streams,
)
from trading_bot.config.settings import AppSettings
from trading_bot.domain.market.ingestion import IngestionCursor, InstrumentObservation
from trading_bot.domain.market.models import Instrument, OHLCVBar
from trading_bot.ports.market_data import MarketDataPage
from trading_bot.workers.market_bar_collector_worker import MarketBarCollectorWorker

WORKSPACE = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
NOW = datetime(2026, 11, 15, 10, 0, 30, tzinfo=UTC)


def _bar(symbol: str, interval: str, open_time: datetime, received: datetime) -> OHLCVBar:
    duration = {"1m": timedelta(minutes=1), "5m": timedelta(minutes=5)}[interval]
    close_time = open_time + duration - timedelta(milliseconds=1)
    return OHLCVBar(
        symbol=symbol,
        timeframe=interval,
        open_time=open_time,
        close_time=close_time,
        event_at=close_time,
        received_at=received,
        available_at=received,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=Decimal("10"),
        quote_volume=Decimal("1000"),
        trade_count=5,
        taker_buy_base_volume=Decimal("6"),
        taker_buy_quote_volume=Decimal("600"),
        is_closed=True,
        source="BINANCE_USDM",
    )


class FakeBinance:
    """Trả các nến đã đóng trong [start, end] tới ``published_until`` (giả lập độ trễ phát hành)."""

    def __init__(self, published_until: datetime | None = None) -> None:
        self.published_until = published_until
        self.kline_calls: list[tuple[str, str, datetime, datetime]] = []
        self.exchange_calls = 0

    async def get_klines(
        self, *, symbol: str, interval: str, start_time: datetime, end_time: datetime, limit: int
    ) -> MarketDataPage[OHLCVBar]:
        self.kline_calls.append((symbol, interval, start_time, end_time))
        duration = {"1m": timedelta(minutes=1), "5m": timedelta(minutes=5)}[interval]
        bars = []
        cursor = start_time
        while cursor <= end_time and len(bars) < limit:
            if self.published_until is None or cursor < self.published_until:
                bars.append(_bar(symbol, interval, cursor, NOW))
            cursor += duration
        payload = f"{symbol}:{interval}:{start_time.isoformat()}:{len(bars)}".encode()
        return MarketDataPage(
            records=tuple(bars),
            raw_payload=payload,
            checksum=hashlib.sha256(payload).hexdigest(),
            received_at=NOW,
            available_at=NOW,
            is_complete=len(bars) < limit,
        )

    async def get_exchange_info(self) -> Any:
        self.exchange_calls += 1
        instrument = Instrument(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            tick_size=Decimal("0.1"),
            quantity_step=Decimal("0.001"),
            min_notional=Decimal("5"),
            status="TRADING",
        )
        observation = InstrumentObservation(
            instrument=instrument, source="BINANCE_USDM", observed_at=NOW
        )
        return SimpleNamespace(
            records=(SimpleNamespace(observation=observation),),
            received_at=NOW,
            available_at=NOW,
        )


class AllowGate:
    def __init__(self) -> None:
        self.requests: list[RestBackfillRequest] = []

    async def authorize(self, *, request: RestBackfillRequest, now: datetime) -> Any:
        self.requests.append(request)
        return BackfillRightsEvidence(
            workspace_id=WORKSPACE,
            provider="BINANCE_USDM_REST",
            purpose=request.purpose,
            terms_url="https://www.binance.com/en/terms",
            policy_fingerprint="a" * 64,
            verified_at=now - timedelta(days=1),
            valid_until=now + timedelta(days=30),
            checked_at=now,
        )


class DenyGate:
    async def authorize(self, *, request: RestBackfillRequest, now: datetime) -> Any:
        raise PermissionError("Chưa có policy PAPER_DEMO cho BINANCE_USDM_REST.")


@dataclass
class FakeMarket:
    cursors: dict[tuple[str, str, str], IngestionCursor] = field(default_factory=dict)
    bars: list[OHLCVBar] = field(default_factory=list)
    instrument_id: UUID | None = None

    async def get_cursor(self, *, workspace_id: UUID, stream: Any, as_of: datetime) -> Any:
        return self.cursors.get((stream.source, stream.symbol, stream.timeframe))

    async def save_cursor(self, *, workspace_id: UUID, cursor: IngestionCursor) -> None:
        stream = cursor.stream
        self.cursors[(stream.source, stream.symbol, stream.timeframe)] = cursor

    async def get_instrument(self, **kwargs: Any) -> Any:
        return None if self.instrument_id is None else SimpleNamespace(id=self.instrument_id)

    async def append_instrument(self, **kwargs: Any) -> Any:
        self.instrument_id = uuid4()
        return SimpleNamespace(record_id=self.instrument_id)

    async def append_bar(self, **kwargs: Any) -> Any:
        assert kwargs["workspace_id"] == WORKSPACE
        assert kwargs["version"].idempotency_key.startswith("BINANCE_USDM_REST_BACKFILL:")
        self.bars.append(kwargs["bar"])
        return None


def _collector(
    binance: FakeBinance, market: FakeMarket, gate: Any, *, streams: str = "BTCUSDT:1m"
) -> MarketBarCollector:
    @asynccontextmanager
    async def scope() -> Any:
        yield SimpleNamespace(market=market, rights_gate=gate)

    return MarketBarCollector(
        workspace_id=WORKSPACE,
        streams=parse_collector_streams(streams),
        market_data=binance,  # type: ignore[arg-type]
        session_scope=scope,
        initial_bars=10,
    )


def test_parse_streams_and_closed_boundary() -> None:
    streams = parse_collector_streams(" BTCUSDT:1m,5m ; ETHUSDT:1m;BTCUSDT:1m ")
    assert streams == (
        CollectorStream("BTCUSDT", "1m"),
        CollectorStream("BTCUSDT", "5m"),
        CollectorStream("ETHUSDT", "1m"),
    )
    for bad in ("", "BTCUSDT", "BTCUSDT:2h", "btcusdt:1m"):
        with pytest.raises(ValueError, match="collector"):
            parse_collector_streams(bad)
    # 10:00:30 trừ 3 giây chờ phát hành → nến 1m cuối đã đóng kết thúc lúc 10:00.
    assert last_closed_boundary(NOW, "1m") == datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
    assert last_closed_boundary(datetime(2026, 11, 15, 10, 0, 2, tzinfo=UTC), "1m") == datetime(
        2026, 11, 15, 9, 59, tzinfo=UTC
    )
    assert last_closed_boundary(NOW, "5m") == datetime(2026, 11, 15, 10, 0, tzinfo=UTC)


def test_first_tick_backfills_initial_window_with_paper_demo_rights() -> None:
    binance, market, gate = FakeBinance(), FakeMarket(), AllowGate()
    tick = asyncio.run(_collector(binance, market, gate).collect_once(NOW))

    assert tick.succeeded
    (result,) = tick.results
    assert (result.status, result.bars, result.missing) == ("collected", 10, 0)
    assert binance.exchange_calls == 1
    assert gate.requests[0].purpose is BackfillRightsPurpose.PAPER_DEMO
    assert gate.requests[0].start == datetime(2026, 11, 15, 9, 50, tzinfo=UTC)
    assert gate.requests[0].end == datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
    assert market.bars[0].taker_buy_base_volume == Decimal("6")
    assert market.bars[-1].source == "BINANCE_USDM_REST_BACKFILL"
    cursor = market.cursors[(COLLECTOR_CURSOR_SOURCE, "BTCUSDT", "1m")]
    assert cursor.covered_until == datetime(2026, 11, 15, 10, 0, tzinfo=UTC)

    # Cùng thời điểm: đã đủ → không gọi mạng lần nữa.
    again = asyncio.run(_collector(binance, market, gate).collect_once(NOW))
    assert again.results[0].status == "up_to_date"
    assert len(binance.kline_calls) == 1


def test_rights_denied_does_not_call_binance_or_write() -> None:
    binance, market = FakeBinance(), FakeMarket()
    tick = asyncio.run(_collector(binance, market, DenyGate()).collect_once(NOW))
    assert not tick.succeeded
    assert tick.results[0].status == "rights_denied"
    assert binance.kline_calls == []
    assert market.bars == []
    assert market.cursors == {}


def test_unpublished_tail_bar_is_fetched_next_tick_not_skipped() -> None:
    binance = FakeBinance(published_until=datetime(2026, 11, 15, 9, 59, tzinfo=UTC))
    market, gate = FakeMarket(), AllowGate()
    first = asyncio.run(_collector(binance, market, gate).collect_once(NOW))
    assert first.results[0].bars == 9
    assert market.cursors[(COLLECTOR_CURSOR_SOURCE, "BTCUSDT", "1m")].covered_until == datetime(
        2026, 11, 15, 9, 59, tzinfo=UTC
    )
    binance.published_until = None
    second = asyncio.run(_collector(binance, market, gate).collect_once(NOW))
    assert second.results[0].bars == 1
    assert market.bars[-1].open_time == datetime(2026, 11, 15, 9, 59, tzinfo=UTC)


def test_one_stream_error_does_not_block_others() -> None:
    class Partial(FakeBinance):
        async def get_klines(self, **kwargs: Any) -> MarketDataPage[OHLCVBar]:
            if kwargs["symbol"] == "ETHUSDT":
                raise RuntimeError("network")
            return await super().get_klines(**kwargs)

    market = FakeMarket()
    tick = asyncio.run(
        _collector(Partial(), market, AllowGate(), streams="ETHUSDT:1m;BTCUSDT:1m").collect_once(
            NOW
        )
    )
    assert [r.status for r in tick.results] == ["error", "collected"]
    assert tick.results[0].detail == "RuntimeError"


def test_worker_records_tick_and_status_counts() -> None:
    market = FakeMarket()
    worker = MarketBarCollectorWorker(
        collector=_collector(FakeBinance(), market, DenyGate()), tick_seconds=30, clock=lambda: NOW
    )
    asyncio.run(worker.run_once())
    assert worker.last_tick is not None

    async def status() -> dict[str, Any] | None:
        task = asyncio.create_task(asyncio.sleep(3600))
        app = SimpleNamespace(
            state=SimpleNamespace(market_collector=worker, market_collector_task=task)
        )
        result = _market_collector_status(app)  # type: ignore[arg-type]
        task.cancel()
        return result

    assert asyncio.run(status()) == {
        "state": "degraded",
        "streams": 1,
        "rights_denied": 1,
        "errors": 0,
    }


def test_settings_allow_collector_only_for_paper_ingestion_worker() -> None:
    workspace = str(uuid4())
    enabled = AppSettings(
        app_env="test",
        process_role="ingestion-worker",
        ingestion_workspace_id=workspace,
        market_collector_enabled=True,
    )
    assert enabled.market_collector_enabled
    with pytest.raises(ValidationError, match="ingestion-worker"):
        AppSettings(app_env="test", process_role="admin-web", market_collector_enabled=True)
    with pytest.raises(ValidationError, match="collector"):
        AppSettings(
            app_env="test",
            process_role="ingestion-worker",
            ingestion_workspace_id=workspace,
            market_collector_enabled=True,
            market_collector_streams="BTCUSDT:2h",
        )


def test_page_received_after_tick_start_is_accepted() -> None:
    """Adapter thật đóng dấu received_at sau đầu tick; collector không được coi là "tương lai"."""

    class LateReceipt(FakeBinance):
        async def get_klines(self, **kwargs: Any) -> MarketDataPage[OHLCVBar]:
            page = await super().get_klines(**kwargs)
            late = NOW + timedelta(seconds=5)
            return MarketDataPage(
                records=page.records,
                raw_payload=page.raw_payload,
                checksum=page.checksum,
                received_at=late,
                available_at=late,
                is_complete=page.is_complete,
            )

    tick = asyncio.run(_collector(LateReceipt(), FakeMarket(), AllowGate()).collect_once(NOW))
    assert tick.results[0].status == "collected"
