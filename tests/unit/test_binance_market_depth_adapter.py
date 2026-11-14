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
NOW = datetime(2026, 11, 14, 15, 30, tzinfo=UTC)


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
        assert "x-mbx-apikey" not in request.headers
        return request


@pytest.mark.asyncio
async def test_order_book_returns_sorted_levels_and_top_of_book_imbalance() -> None:
    recorder = Recorder(fixture("depth.json"))
    async with recorder.adapter() as adapter:
        page = await adapter.get_order_book("BTCUSDT", limit=5)

    request = recorder.only_request()
    assert request.url.path == "/fapi/v1/depth"
    assert dict(request.url.params) == {"symbol": "BTCUSDT", "limit": "5"}
    assert recorder.weights == [(2, None)]
    (book,) = page.records
    assert book.symbol == "BTCUSDT"
    assert book.last_update_id == 12112579716621
    assert book.bids[0] == (Decimal("65879.90"), Decimal("6.899"))
    assert book.asks[0] == (Decimal("65880.00"), Decimal("0.178"))
    assert [price for price, _ in book.bids] == sorted(
        (price for price, _ in book.bids), reverse=True
    )
    assert book.mid_price == Decimal("65879.95")
    bid, ask = Decimal("9.883"), Decimal("0.183")
    assert book.imbalance(levels=5) == (bid - ask) / (bid + ask)


@pytest.mark.parametrize(("limit", "weight"), [(50, 2), (100, 5), (500, 10), (1000, 20)])
@pytest.mark.asyncio
async def test_order_book_weight_follows_limit(limit: int, weight: int) -> None:
    recorder = Recorder(fixture("depth.json"))
    async with recorder.adapter() as adapter:
        await adapter.get_order_book("BTCUSDT", limit=limit)
    assert recorder.weights == [(weight, None)]


@pytest.mark.asyncio
async def test_order_book_rejects_bad_limit_and_crossed_or_unsorted_levels() -> None:
    recorder = Recorder(fixture("depth.json"))
    async with recorder.adapter() as adapter:
        with pytest.raises(ValueError, match="limit"):
            await adapter.get_order_book("BTCUSDT", limit=7)
    crossed = fixture("depth.json")
    crossed["asks"][0][0] = "65879.00"
    unsorted = fixture("depth.json")
    unsorted["bids"].reverse()
    for payload in (crossed, unsorted):
        bad = Recorder(payload)
        async with bad.adapter() as adapter:
            with pytest.raises(MarketDataContractError):
                await adapter.get_order_book("BTCUSDT", limit=5)


@pytest.mark.asyncio
async def test_aggregate_trades_expose_taker_side_and_paging_cursor() -> None:
    recorder = Recorder(fixture("agg_trades.json"))
    async with recorder.adapter() as adapter:
        page = await adapter.get_aggregate_trades(symbol="BTCUSDT", from_id=3522210116, limit=3)

    request = recorder.only_request()
    assert request.url.path == "/fapi/v1/aggTrades"
    assert dict(request.url.params) == {
        "symbol": "BTCUSDT",
        "limit": "3",
        "fromId": "3522210116",
    }
    assert recorder.weights == [(20, None)]
    assert [trade.aggregate_id for trade in page.records] == [
        3522210116,
        3522210117,
        3522210118,
    ]
    assert [trade.taker_side for trade in page.records] == ["SELL", "SELL", "BUY"]
    assert page.records[0].price == Decimal("65870.40")
    assert page.records[0].quote_quantity == Decimal("65870.40") * Decimal("0.075")
    assert page.is_complete is False


@pytest.mark.asyncio
async def test_aggregate_trades_reject_from_id_with_time_range() -> None:
    recorder = Recorder(fixture("agg_trades.json"))
    async with recorder.adapter() as adapter:
        with pytest.raises(ValueError, match="from_id"):
            await adapter.get_aggregate_trades(symbol="BTCUSDT", from_id=1, start_time=NOW)


@pytest.mark.asyncio
async def test_basis_returns_futures_minus_index() -> None:
    recorder = Recorder(fixture("basis.json"))
    async with recorder.adapter() as adapter:
        page = await adapter.get_basis(pair="BTCUSDT", period="5m", limit=2)

    request = recorder.only_request()
    assert request.url.path == "/futures/data/basis"
    assert dict(request.url.params) == {
        "pair": "BTCUSDT",
        "contractType": "PERPETUAL",
        "period": "5m",
        "limit": "2",
    }
    assert recorder.weights == [(0, "futures_data")]
    last = page.records[-1]
    assert last.futures_price == Decimal("65889.70")
    assert last.index_price == Decimal("65923.20652174")
    assert last.basis == Decimal("-33.50652174")
    assert last.basis_rate == Decimal("-0.0005")


@pytest.mark.parametrize(
    ("kind", "path", "name", "key"),
    [
        ("mark", "/fapi/v1/markPriceKlines", "mark_price_klines.json", "symbol"),
        ("index", "/fapi/v1/indexPriceKlines", "index_price_klines.json", "pair"),
    ],
)
@pytest.mark.asyncio
async def test_reference_price_klines_for_mark_and_index(
    kind: str, path: str, name: str, key: str
) -> None:
    recorder = Recorder(fixture(name))
    async with recorder.adapter() as adapter:
        page = await adapter.get_reference_price_klines(
            symbol="BTCUSDT", kind=kind, interval="1h", limit=2
        )

    request = recorder.only_request()
    assert request.url.path == path
    assert dict(request.url.params) == {key: "BTCUSDT", "interval": "1h", "limit": "2"}
    assert recorder.weights == [(1, None)]
    first, second = page.records
    assert first.kind == kind
    assert first.open_time == datetime(2026, 11, 14, 14, 0, tzinfo=UTC)
    assert first.low <= min(first.open, first.close) <= max(first.open, first.close) <= first.high
    assert (first.is_closed, second.is_closed) == (True, False)


@pytest.mark.asyncio
async def test_reference_price_klines_reject_unknown_kind() -> None:
    recorder = Recorder(fixture("mark_price_klines.json"))
    async with recorder.adapter() as adapter:
        with pytest.raises(ValueError, match="kind"):
            await adapter.get_reference_price_klines(
                symbol="BTCUSDT", kind="premium", interval="1h"
            )
