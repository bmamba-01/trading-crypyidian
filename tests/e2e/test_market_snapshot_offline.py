"""E2E offline: mọi endpoint snapshot công khai đi qua adapter chỉ bằng fixture đã ghi."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest

from trading_bot.adapters.binance_usdm.market_data import BinanceUsdMMarketDataAdapter

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "binance_usdm"
NOW = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)
FIXTURE_BY_PATH = {
    "/fapi/v1/premiumIndex": "premium_index.json",
    "/fapi/v2/ticker/price": "ticker_price.json",
    "/fapi/v1/ticker/bookTicker": "book_ticker.json",
    "/fapi/v1/ticker/24hr": "ticker_24hr.json",
    "/fapi/v1/fundingInfo": "funding_info.json",
    "/futures/data/globalLongShortAccountRatio": "long_short_ratio.json",
    "/futures/data/topLongShortAccountRatio": "long_short_ratio.json",
    "/futures/data/topLongShortPositionRatio": "long_short_ratio.json",
    "/futures/data/takerlongshortRatio": "taker_buy_sell_volume.json",
}


@pytest.mark.asyncio
async def test_public_market_snapshot_uses_only_recorded_fixtures_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        fixture_name = FIXTURE_BY_PATH.get(request.url.path)
        if request.url.host != "fapi.binance.com" or fixture_name is None:
            raise AssertionError("Adapter thử gọi URL không nằm trong fixture offline.")
        return httpx2.Response(200, content=(FIXTURE_DIR / fixture_name).read_bytes())

    adapter = BinanceUsdMMarketDataAdapter(
        transport=httpx2.MockTransport(handler), clock=lambda: NOW
    )
    async with adapter:
        premium = await adapter.get_premium_index("BTCUSDT")
        price = await adapter.get_price_tickers("BTCUSDT")
        book = await adapter.get_book_tickers("BTCUSDT")
        stats = await adapter.get_24h_tickers("BTCUSDT")
        funding_info = await adapter.get_funding_info()
        ratios = [
            await adapter.get_long_short_ratio(symbol="BTCUSDT", kind=kind, period="5m", limit=3)
            for kind in ("global_account", "top_trader_account", "top_trader_position")
        ]
        taker = await adapter.get_taker_buy_sell_volume(symbol="BTCUSDT", period="5m", limit=3)

    # Giá khớp, mark price và best bid/ask của cùng một symbol phải nằm cùng vùng giá.
    mark = premium.records[0].mark_price
    assert abs(price.records[0].price - mark) / mark < Decimal("0.01")
    assert book.records[0].bid_price < book.records[0].ask_price
    assert stats.records[0].low_price <= stats.records[0].last_price <= stats.records[0].high_price
    assert {info.funding_interval_hours for info in funding_info.records} == {4}
    assert [len(page.records) for page in ratios] == [3, 3, 3]
    assert all(
        abs(item.long_share + item.short_share - 1) < Decimal("0.001")
        for page in ratios
        for item in page.records
    )
    assert all(item.buy_volume > 0 and item.sell_volume > 0 for item in taker.records)
    assert sorted({request.url.path for request in requests}) == sorted(FIXTURE_BY_PATH)
    for request in requests:
        assert request.method == "GET"
        assert "x-mbx-apikey" not in request.headers
        assert not {"apiKey", "api_key", "secret", "signature"} & set(request.url.params.keys())
