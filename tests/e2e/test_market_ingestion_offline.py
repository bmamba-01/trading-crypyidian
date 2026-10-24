from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest

from trading_bot.adapters.binance_usdm.market_data import BinanceUsdMMarketDataAdapter

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "binance_usdm"
NOW = datetime(2026, 10, 22, 12, 30, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_public_binance_market_data_ingestion_uses_only_recorded_offline_fixtures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)

    fixture_by_path = {
        "/fapi/v1/exchangeInfo": "exchange_info.json",
        "/fapi/v1/klines": "klines.json",
        "/fapi/v1/fundingRate": "funding_rates.json",
        "/fapi/v1/openInterest": "open_interest.json",
        "/futures/data/openInterestHist": "open_interest_history.json",
    }
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        fixture_name = fixture_by_path.get(request.url.path)
        if request.url.host != "fapi.binance.com" or fixture_name is None:
            raise AssertionError("Adapter thử gọi URL không nằm trong fixture offline.")
        return httpx2.Response(200, content=(FIXTURE_DIR / fixture_name).read_bytes())

    adapter = BinanceUsdMMarketDataAdapter(
        transport=httpx2.MockTransport(handler),
        clock=lambda: NOW,
    )
    async with adapter:
        instruments = await adapter.get_exchange_info()
        klines = await adapter.get_klines(symbol="BTCUSDT", interval="1m", limit=10)
        funding = await adapter.get_funding_rates(symbol="BTCUSDT", limit=10)
        open_interest = await adapter.get_open_interest("BTCUSDT")
        history = await adapter.get_open_interest_history(
            symbol="BTCUSDT",
            period="5m",
            start_time=datetime(2026, 10, 22, 12, 0, tzinfo=UTC),
            end_time=datetime(2026, 10, 22, 12, 10, tzinfo=UTC),
        )

    assert instruments.records[0].observation.instrument.symbol == "BTCUSDT"
    assert [bar.is_closed for bar in klines.records] == [True, False]
    assert funding.records[0].funding_rate.as_tuple().exponent == -8
    assert open_interest.records[0].quantity > 0
    assert history.is_complete is False
    assert history.missing_times == (datetime(2026, 10, 22, 12, 5, tzinfo=UTC),)
    assert len(requests) == 5
    for request in requests:
        assert request.method == "GET"
        assert request.url.host == "fapi.binance.com"
        assert request.url.path in fixture_by_path
        assert "x-mbx-apikey" not in request.headers
        assert "signature" not in request.url.params
        assert not {"apiKey", "api_key", "secret", "signature"} & set(request.url.params.keys())

    fixture_body = (FIXTURE_DIR / "klines.json").read_bytes()
    assert klines.raw_payload == fixture_body
    assert klines.checksum == hashlib.sha256(fixture_body).hexdigest()
