from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest

from trading_bot.adapters.binance_usdm.market_data import (
    BinanceHTTPError,
    BinanceRateLimitError,
    BinanceUsdMMarketDataAdapter,
    MarketDataContractError,
    PublicRequestLimiter,
)

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "binance_usdm"
NOW = datetime(2026, 10, 22, 12, 30, 30, tzinfo=UTC)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


class RecordingLimiter:
    def __init__(self) -> None:
        self.requests: list[tuple[int, str | None]] = []
        self.observed: list[str | None] = []
        self.configured_limits: list[int] = []

    async def acquire(self, *, weight: int, category: str | None = None) -> None:
        self.requests.append((weight, category))

    def observe(self, headers: Any) -> None:
        self.observed.append(headers.get("x-mbx-used-weight-1m"))

    def configure_requests_per_minute(self, limit: int) -> None:
        self.configured_limits.append(limit)


def build_adapter(
    handler: Any,
    *,
    sleep: Any = None,
    limiter: Any = None,
) -> BinanceUsdMMarketDataAdapter:
    transport = httpx2.MockTransport(handler)
    return BinanceUsdMMarketDataAdapter(
        transport=transport,
        clock=lambda: NOW,
        sleep=sleep or asyncio.sleep,
        rate_limiter=limiter,
    )


@pytest.mark.asyncio
async def test_exchange_info_uses_filter_steps_and_decimal_values() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, content=fixture_bytes("exchange_info.json"))

    limiter = RecordingLimiter()
    adapter = build_adapter(handler, limiter=limiter)
    async with adapter:
        page = await adapter.get_exchange_info()

    assert len(page.records) == 1
    metadata = page.records[0]
    assert metadata.observation.instrument.symbol == "BTCUSDT"
    assert metadata.observation.instrument.tick_size == Decimal("0.10")
    assert metadata.observation.instrument.quantity_step == Decimal("0.001")
    assert metadata.observation.instrument.min_notional == Decimal("5.0")
    assert metadata.contract_type == "PERPETUAL"
    assert page.checksum
    assert requests[0].url.path == "/fapi/v1/exchangeInfo"
    assert "x-mbx-apikey" not in requests[0].headers
    assert limiter.configured_limits == [2400]


@pytest.mark.asyncio
async def test_exchange_info_rejects_missing_required_filter_instead_of_guessing() -> None:
    payload = json.loads(fixture_bytes("exchange_info.json"))
    payload["symbols"][0]["filters"] = [
        item for item in payload["symbols"][0]["filters"] if item["filterType"] != "PRICE_FILTER"
    ]

    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=payload)

    adapter = build_adapter(handler)
    async with adapter:
        with pytest.raises(MarketDataContractError, match="PRICE_FILTER"):
            await adapter.get_exchange_info()


@pytest.mark.asyncio
async def test_klines_keep_decimal_precision_and_mark_current_bar_open() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, content=fixture_bytes("klines.json"))

    limiter = RecordingLimiter()
    adapter = build_adapter(handler, limiter=limiter)
    start = datetime(2026, 10, 22, 12, 29, tzinfo=UTC)
    end = datetime(2026, 10, 22, 12, 30, tzinfo=UTC)
    async with adapter:
        page = await adapter.get_klines(
            symbol="BTCUSDT",
            interval="1m",
            start_time=start,
            end_time=end,
            limit=1000,
        )

    assert page.records[0].open == Decimal("65000.10")
    assert page.records[0].volume == Decimal("12.34000000")
    assert page.records[0].is_closed is True
    assert page.records[1].is_closed is False
    assert page.records[1].event_at == NOW
    assert page.records[1].available_at == NOW
    assert requests[0].url.params["startTime"] == "1792672140000"
    assert requests[0].url.params["endTime"] == "1792672200000"
    assert limiter.requests == [(5, None)]
    assert page.checksum


@pytest.mark.asyncio
async def test_funding_history_uses_inclusive_bounds_and_retry_after() -> None:
    requests: list[httpx2.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx2.Response(429, headers={"Retry-After": "2"}, json={"code": -1003})
        return httpx2.Response(200, content=fixture_bytes("funding_rates.json"))

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    limiter = RecordingLimiter()
    adapter = build_adapter(handler, sleep=sleep, limiter=limiter)
    start = NOW - timedelta(hours=9)
    async with adapter:
        page = await adapter.get_funding_rates(
            symbol="BTCUSDT", start_time=start, end_time=NOW, limit=100
        )

    assert len(requests) == 2
    assert sleeps == [2.0]
    assert all(request.url.path == "/fapi/v1/fundingRate" for request in requests)
    assert requests[-1].url.params["startTime"] == str(int(start.timestamp() * 1000))
    assert requests[-1].url.params["endTime"] == str(int(NOW.timestamp() * 1000))
    assert page.records[0].funding_rate == Decimal("0.00010000")
    assert limiter.requests == [(0, "funding"), (0, "funding")]


@pytest.mark.asyncio
async def test_non_retryable_http_error_fails_without_second_request() -> None:
    calls = 0

    def handler(_: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(400, json={"code": -1121, "msg": "Invalid symbol."})

    adapter = build_adapter(handler)
    async with adapter:
        with pytest.raises(BinanceHTTPError, match="400"):
            await adapter.get_open_interest("BAD")

    assert calls == 1


@pytest.mark.asyncio
async def test_418_with_long_retry_after_is_surfaced_without_early_retry() -> None:
    calls = 0

    def handler(_: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(418, headers={"Retry-After": "120"}, json={"code": -1003})

    adapter = build_adapter(handler)
    async with adapter:
        with pytest.raises(BinanceRateLimitError, match="418") as error:
            await adapter.get_open_interest("BTCUSDT")

    assert calls == 1
    assert error.value.retry_after == 120


@pytest.mark.asyncio
async def test_server_error_uses_bounded_backoff_then_recovers() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx2.Response(503)
        return httpx2.Response(200, content=fixture_bytes("open_interest.json"))

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    adapter = build_adapter(handler, sleep=sleep)
    async with adapter:
        page = await adapter.get_open_interest("BTCUSDT")

    assert calls == 2
    assert sleeps == [0.25]
    assert page.records[0].quantity == Decimal("12345.678")


@pytest.mark.asyncio
async def test_timeout_retries_are_bounded_and_do_not_add_authentication() -> None:
    requests: list[httpx2.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if len(requests) < 3:
            raise httpx2.ReadTimeout("offline timeout", request=request)
        return httpx2.Response(200, content=fixture_bytes("open_interest.json"))

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    adapter = build_adapter(handler, sleep=sleep)
    async with adapter:
        page = await adapter.get_open_interest("BTCUSDT")

    assert len(requests) == 3
    assert sleeps == [0.25, 0.5]
    assert page.records[0].quantity == Decimal("12345.678")
    for request in requests:
        assert "x-mbx-apikey" not in request.headers
        assert "signature" not in request.url.params


@pytest.mark.asyncio
async def test_open_interest_history_reports_omitted_period_and_old_retention() -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=fixture_bytes("open_interest_history.json"))

    adapter = build_adapter(handler)
    start = datetime(2026, 10, 22, 12, 0, 30, tzinfo=UTC)
    end = datetime(2026, 10, 22, 12, 10, tzinfo=UTC)
    async with adapter:
        page = await adapter.get_open_interest_history(
            symbol="BTCUSDT",
            period="5m",
            start_time=start,
            end_time=end,
        )

    assert len(page.records) == 1
    assert page.missing_times == (datetime(2026, 10, 22, 12, 5, tzinfo=UTC),)
    assert page.is_complete is False
    assert "lược bỏ" in (page.incomplete_reason or "")

    adapter = build_adapter(handler)
    async with adapter:
        old = await adapter.get_open_interest_history(
            symbol="BTCUSDT",
            period="1d",
            start_time=NOW - timedelta(days=45),
            end_time=NOW - timedelta(days=40),
        )
    assert old.is_complete is False
    assert "một tháng" in (old.incomplete_reason or "")


@pytest.mark.asyncio
async def test_malformed_kline_tuple_is_rejected() -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=[[1792672140000, "65000.10"]])

    adapter = build_adapter(handler)
    async with adapter:
        with pytest.raises(MarketDataContractError, match="12 trường"):
            await adapter.get_klines(symbol="BTCUSDT", interval="1m", limit=1)


@pytest.mark.asyncio
async def test_public_request_limiter_waits_for_weight_window_with_headroom() -> None:
    now = [0.0]
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    limiter = PublicRequestLimiter(
        requests_per_minute=10,
        safety_ratio=0.5,
        monotonic=lambda: now[0],
        sleep=sleep,
    )
    await limiter.acquire(weight=5)
    await limiter.acquire(weight=1)

    assert sleeps == [60.0]
