from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest

from trading_bot.adapters.binance_usdm.market_data import (
    BinanceUsdMMarketDataAdapter,
    MarketDataContractError,
)

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "binance_usdm"
# Sau mọi mốc thời gian trong fixture ghi ngày 14/11/2026.
NOW = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)


def fixture(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


class Recorder:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.requests: list[httpx2.Request] = []
        self.weights: list[tuple[int, str | None]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(200, json=self.payload)

    async def acquire(self, *, weight: int, category: str | None = None) -> None:
        self.weights.append((weight, category))

    def observe(self, headers: Any) -> None:
        return None

    def adapter(self) -> BinanceUsdMMarketDataAdapter:
        return BinanceUsdMMarketDataAdapter(
            transport=httpx2.MockTransport(self), clock=lambda: NOW, rate_limiter=self
        )

    def only_request(self) -> httpx2.Request:
        assert len(self.requests) == 1
        request = self.requests[0]
        assert request.method == "GET"
        assert request.url.host == "fapi.binance.com"
        assert "x-mbx-apikey" not in request.headers
        assert "signature" not in request.url.params
        return request


@pytest.mark.asyncio
async def test_premium_index_for_one_symbol_returns_mark_index_and_funding() -> None:
    recorder = Recorder(fixture("premium_index.json"))
    async with recorder.adapter() as adapter:
        page = await adapter.get_premium_index("BTCUSDT")

    request = recorder.only_request()
    assert request.url.path == "/fapi/v1/premiumIndex"
    assert dict(request.url.params) == {"symbol": "BTCUSDT"}
    assert recorder.weights == [(1, None)]
    (record,) = page.records
    assert record.symbol == "BTCUSDT"
    assert record.mark_price == Decimal("66537.30000000")
    assert record.index_price == Decimal("66556.09760870")
    assert record.last_funding_rate == Decimal("0.00003307")
    assert record.next_funding_time == datetime(2026, 11, 14, 16, 0, tzinfo=UTC)
    assert record.observed_at <= record.received_at == NOW


@pytest.mark.asyncio
async def test_premium_index_for_all_symbols_keeps_unicode_and_unscheduled_funding() -> None:
    base = fixture("premium_index.json")
    payload = [
        base,
        base | {"symbol": "哈基米USDT"},
        base | {"symbol": "ZRCUSDT", "nextFundingTime": 0},
    ]
    recorder = Recorder(payload)
    async with recorder.adapter() as adapter:
        page = await adapter.get_premium_index()

    assert dict(recorder.only_request().url.params) == {}
    assert recorder.weights == [(10, None)]
    assert [record.symbol for record in page.records] == ["BTCUSDT", "哈基米USDT", "ZRCUSDT"]
    assert page.records[2].next_funding_time is None


@pytest.mark.asyncio
async def test_price_ticker_uses_v2_endpoint_for_one_or_all_symbols() -> None:
    single = Recorder(fixture("ticker_price.json"))
    async with single.adapter() as adapter:
        page = await adapter.get_price_tickers("BTCUSDT")
    assert single.only_request().url.path == "/fapi/v2/ticker/price"
    assert single.weights == [(1, None)]
    assert page.records[0].price == Decimal("66550.70")

    everything = Recorder([fixture("ticker_price.json")])
    async with everything.adapter() as adapter:
        page = await adapter.get_price_tickers()
    assert dict(everything.only_request().url.params) == {}
    assert everything.weights == [(2, None)]
    assert len(page.records) == 1


@pytest.mark.asyncio
async def test_book_ticker_returns_best_bid_ask_and_rejects_crossed_book() -> None:
    recorder = Recorder(fixture("book_ticker.json"))
    async with recorder.adapter() as adapter:
        page = await adapter.get_book_tickers("BTCUSDT")

    assert recorder.only_request().url.path == "/fapi/v1/ticker/bookTicker"
    assert recorder.weights == [(2, None)]
    (record,) = page.records
    assert (record.bid_price, record.ask_price) == (Decimal("66534.70"), Decimal("66534.80"))
    assert record.spread == Decimal("0.10")

    crossed = Recorder(fixture("book_ticker.json") | {"bidPrice": "66540.00"})
    async with crossed.adapter() as adapter:
        with pytest.raises(MarketDataContractError):
            await adapter.get_book_tickers("BTCUSDT")


@pytest.mark.asyncio
async def test_ticker_24h_returns_rolling_statistics() -> None:
    recorder = Recorder(fixture("ticker_24hr.json"))
    async with recorder.adapter() as adapter:
        page = await adapter.get_24h_tickers("BTCUSDT")

    assert recorder.only_request().url.path == "/fapi/v1/ticker/24hr"
    assert recorder.weights == [(1, None)]
    (record,) = page.records
    assert record.last_price == Decimal("66530.40")
    assert record.price_change_percent == Decimal("-4.171")
    assert record.quote_volume == Decimal("12760092848.71")
    assert record.trade_count == 4743746
    assert record.open_time < record.close_time

    everything = Recorder([fixture("ticker_24hr.json")])
    async with everything.adapter() as adapter:
        await adapter.get_24h_tickers()
    assert everything.weights == [(40, None)]


@pytest.mark.asyncio
async def test_funding_info_lists_interval_and_caps_under_funding_quota() -> None:
    recorder = Recorder(fixture("funding_info.json"))
    async with recorder.adapter() as adapter:
        page = await adapter.get_funding_info()

    assert recorder.only_request().url.path == "/fapi/v1/fundingInfo"
    assert recorder.weights == [(0, "funding")]
    first = page.records[0]
    assert first.symbol == "GTCUSDT"
    assert first.funding_interval_hours == 4
    assert first.rate_cap == Decimal("0.02000000")
    assert first.rate_floor == Decimal("-0.02000000")


@pytest.mark.parametrize(
    ("kind", "path"),
    [
        ("global_account", "/futures/data/globalLongShortAccountRatio"),
        ("top_trader_account", "/futures/data/topLongShortAccountRatio"),
        ("top_trader_position", "/futures/data/topLongShortPositionRatio"),
    ],
)
@pytest.mark.asyncio
async def test_long_short_ratio_maps_kind_to_endpoint(kind: str, path: str) -> None:
    recorder = Recorder(fixture("long_short_ratio.json"))
    async with recorder.adapter() as adapter:
        page = await adapter.get_long_short_ratio(symbol="BTCUSDT", kind=kind, period="5m", limit=3)

    request = recorder.only_request()
    assert request.url.path == path
    assert dict(request.url.params) == {"symbol": "BTCUSDT", "period": "5m", "limit": "3"}
    assert recorder.weights == [(0, "futures_data")]
    assert len(page.records) == 3
    last = page.records[-1]
    assert last.kind == kind
    assert last.long_share == Decimal("0.7549")
    assert last.short_share == Decimal("0.2451")
    assert last.ratio == Decimal("3.0800")
    assert [record.observed_at for record in page.records] == sorted(
        record.observed_at for record in page.records
    )


@pytest.mark.asyncio
async def test_long_short_ratio_rejects_unknown_kind_period_and_foreign_symbol() -> None:
    recorder = Recorder(fixture("long_short_ratio.json"))
    async with recorder.adapter() as adapter:
        with pytest.raises(ValueError, match="kind"):
            await adapter.get_long_short_ratio(symbol="BTCUSDT", kind="whales", period="5m")
        with pytest.raises(ValueError, match="period"):
            await adapter.get_long_short_ratio(symbol="BTCUSDT", kind="global_account", period="1m")
        with pytest.raises(MarketDataContractError):
            await adapter.get_long_short_ratio(symbol="ETHUSDT", kind="global_account", period="5m")


@pytest.mark.asyncio
async def test_taker_buy_sell_volume_returns_flow_per_period() -> None:
    recorder = Recorder(fixture("taker_buy_sell_volume.json"))
    async with recorder.adapter() as adapter:
        page = await adapter.get_taker_buy_sell_volume(symbol="BTCUSDT", period="5m", limit=3)

    request = recorder.only_request()
    assert request.url.path == "/futures/data/takerlongshortRatio"
    assert recorder.weights == [(0, "futures_data")]
    first = page.records[0]
    assert first.symbol == "BTCUSDT"
    assert first.buy_volume == Decimal("642.7440")
    assert first.sell_volume == Decimal("450.2850")
    assert first.ratio == Decimal("1.4274")


@pytest.mark.asyncio
async def test_kline_flow_exposes_trade_count_and_taker_buy_volume() -> None:
    recorder = Recorder(fixture("klines.json"))
    adapter = BinanceUsdMMarketDataAdapter(
        transport=httpx2.MockTransport(recorder),
        clock=lambda: datetime(2026, 10, 22, 12, 30, 30, tzinfo=UTC),
        rate_limiter=recorder,
    )
    async with adapter:
        page = await adapter.get_kline_flows(symbol="BTCUSDT", interval="1m", limit=10)

    assert recorder.only_request().url.path == "/fapi/v1/klines"
    raw = fixture("klines.json")
    first = page.records[0]
    assert first.symbol == "BTCUSDT"
    assert first.timeframe == "1m"
    assert first.trade_count == raw[0][8]
    assert first.taker_buy_volume == Decimal(raw[0][9])
    assert first.taker_buy_quote_volume == Decimal(raw[0][10])
    assert first.taker_sell_volume == Decimal(raw[0][5]) - Decimal(raw[0][9])
    assert [flow.is_closed for flow in page.records] == [True, False]


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        ("get_premium_index", {"symbol": "BTCUSDT"}),
        ("get_price_tickers", {"symbol": "BTCUSDT", "price": 1.5, "time": 1}),
        ("get_book_tickers", []),
        ("get_24h_tickers", {"symbol": "ETHUSDT"}),
    ],
)
@pytest.mark.asyncio
async def test_malformed_snapshot_payload_fails_closed(method: str, payload: Any) -> None:
    recorder = Recorder(payload)
    async with recorder.adapter() as adapter:
        with pytest.raises(MarketDataContractError):
            await getattr(adapter, method)("BTCUSDT")
