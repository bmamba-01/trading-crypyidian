from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx2
import pytest

from trading_bot.adapters.binance_usdm.market_data import BinanceUsdMMarketDataAdapter
from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    BinanceRestBackfillRunner,
    RestBackfillRequest,
)

NOW = datetime(2026, 11, 3, 12, 0, 5, tzinfo=UTC)
START = NOW.replace(second=0, microsecond=0) - timedelta(minutes=2)


class FixtureRightsGate:
    async def authorize(self, *, request: object, now: datetime) -> BackfillRightsEvidence:
        return BackfillRightsEvidence(
            workspace_id=UUID("11111111-1111-4111-8111-111111111111"),
            provider="BINANCE_USDM_REST",
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
            terms_url="https://www.binance.com/en/terms",
            policy_fingerprint=hashlib.sha256(b"approved fixture policy").hexdigest(),
            verified_at=now - timedelta(hours=1),
            valid_until=now + timedelta(days=1),
        )


@pytest.mark.asyncio
async def test_wp07_rest_backfill_uses_public_adapter_and_resumes_half_open_year_slice() -> None:
    end = START + timedelta(minutes=2)
    opens = (START, START + timedelta(minutes=1))
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.url.host == "fapi.binance.com"
        assert request.url.path == "/fapi/v1/klines"
        assert request.url.params["symbol"] == "BTCUSDT"
        assert request.url.params["interval"] == "1m"
        assert request.url.params["limit"] == "1"
        assert request.url.params["endTime"] == str(int(end.timestamp() * 1000) - 1)
        open_time = datetime.fromtimestamp(int(request.url.params["startTime"]) / 1000, UTC)
        if open_time not in opens:
            raise AssertionError("Adapter yêu cầu cursor ngoài fixture được ghi nhận.")
        close_time = open_time + timedelta(minutes=1) - timedelta(milliseconds=1)
        row = [
            int(open_time.timestamp() * 1000),
            "100.0",
            "102.0",
            "99.0",
            "101.0",
            "1.25",
            int(close_time.timestamp() * 1000),
            "126.25",
            2,
            "0.75",
            "75.75",
            "0",
        ]
        return httpx2.Response(200, json=[row])

    adapter = BinanceUsdMMarketDataAdapter(
        transport=httpx2.MockTransport(handler),
        clock=lambda: NOW,
        sleep=lambda _: _no_wait(),
    )
    request = RestBackfillRequest(
        symbol="BTCUSDT",
        interval="1m",
        start=START,
        end=end,
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        page_limit=1,
    )
    async with adapter:
        runner = BinanceRestBackfillRunner(
            market_data=adapter,
            rights_gate=FixtureRightsGate(),
        )
        result = await runner.run_batch(request, now=NOW, max_pages=2)

    assert result.coverage_complete
    assert result.checkpoint.bars_received == 2
    assert len(result.checkpoint.page_receipts) == 2
    assert len(requests) == 2
    assert all("x-mbx-apikey" not in item.headers for item in requests)
    assert all(item.url.path == "/fapi/v1/klines" for item in requests)


async def _no_wait() -> None:
    return None
