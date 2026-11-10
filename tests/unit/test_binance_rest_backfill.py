from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    BinanceRestBackfillRunner,
    RestBackfillCheckpoint,
    RestBackfillError,
    RestBackfillRequest,
    build_trailing_365_day_rest_request,
)
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.ports.market_data import MarketDataPage

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
START = NOW - timedelta(minutes=4)
POLICY_FINGERPRINT = hashlib.sha256(b"policy-v1").hexdigest()
WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")


def request(**changes: object) -> RestBackfillRequest:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "start": START,
        "end": NOW,
        "purpose": BackfillRightsPurpose.BACKTEST_RESEARCH,
        "page_limit": 2,
        "feed_delay": timedelta(0),
    }
    values.update(changes)
    return RestBackfillRequest(**values)  # type: ignore[arg-type]


def evidence(**changes: object) -> BackfillRightsEvidence:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "provider": "BINANCE_USDM_REST",
        "purpose": BackfillRightsPurpose.BACKTEST_RESEARCH,
        "terms_url": "https://www.binance.com/en/terms",
        "policy_fingerprint": POLICY_FINGERPRINT,
        "verified_at": NOW - timedelta(days=1),
        "valid_until": NOW + timedelta(days=1),
    }
    values.update(changes)
    return BackfillRightsEvidence(**values)  # type: ignore[arg-type]


def test_rights_evidence_requires_a_workspace_uuid() -> None:
    with pytest.raises(ValueError, match="workspace_id"):
        evidence(workspace_id="workspace-a")


def bar(
    open_time: datetime,
    *,
    source: str = "BINANCE_USDM",
    received_at: datetime = NOW,
    interval: str = "1m",
) -> OHLCVBar:
    interval_length = timedelta(weeks=1) if interval == "1w" else timedelta(minutes=1)
    close_time = open_time + interval_length - timedelta(milliseconds=1)
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe=interval,
        open_time=open_time,
        close_time=close_time,
        event_at=close_time,
        received_at=received_at,
        available_at=received_at,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("1.5"),
        quote_volume=Decimal("151.5"),
        is_closed=True,
        source=source,
        source_cursor=str(int(open_time.timestamp() * 1000)),
    )


def page(bars: tuple[OHLCVBar, ...], *, complete: bool) -> MarketDataPage[OHLCVBar]:
    raw_payload = f"page-{bars[0].open_time.isoformat() if bars else 'empty'}".encode()
    return MarketDataPage(
        records=bars,
        raw_payload=raw_payload,
        checksum=hashlib.sha256(raw_payload).hexdigest(),
        received_at=NOW,
        available_at=NOW,
        is_complete=complete,
    )


class FakeMarketData:
    def __init__(self, pages: tuple[MarketDataPage[OHLCVBar], ...]) -> None:
        self.pages = list(pages)
        self.calls: list[dict[str, object]] = []

    async def get_klines(self, **kwargs: object) -> MarketDataPage[OHLCVBar]:
        self.calls.append(kwargs)
        return self.pages.pop(0)


class FakeRightsGate:
    def __init__(self, value: BackfillRightsEvidence) -> None:
        self.value = value
        self.calls = 0

    async def authorize(self, **_: object) -> BackfillRightsEvidence:
        self.calls += 1
        return self.value


def test_request_builds_trailing_year_and_rejects_unaligned_or_oversized_ranges() -> None:
    value = build_trailing_365_day_rest_request("BTCUSDT", "1h", now=NOW + timedelta(seconds=20))

    assert value.end == NOW
    assert value.start == NOW - timedelta(days=365)
    assert value.purpose is BackfillRightsPurpose.BACKTEST_RESEARCH

    weekly = build_trailing_365_day_rest_request("BTCUSDT", "1w", now=NOW)
    assert weekly.expected_bars == 52
    assert weekly.end - weekly.start == timedelta(days=364)
    assert weekly.start.weekday() == 0
    assert weekly.end.weekday() == 0

    with pytest.raises(ValueError, match="căn theo biên"):
        request(start=START + timedelta(seconds=1))
    with pytest.raises(ValueError, match="tối đa"):
        request(start=START - timedelta(days=367))
    with pytest.raises(ValueError, match="1..1500"):
        request(page_limit=1501)
    with pytest.raises(ValueError, match="cố định"):
        request(interval="1M")


@pytest.mark.asyncio
async def test_rest_backfill_paginates_half_open_range_and_resumes_from_checkpoint() -> None:
    market = FakeMarketData(
        (
            page((bar(START), bar(START + timedelta(minutes=1))), complete=False),
            page(
                (bar(START + timedelta(minutes=2)), bar(START + timedelta(minutes=3))),
                complete=False,
            ),
        )
    )
    gate = FakeRightsGate(evidence())
    runner = BinanceRestBackfillRunner(market_data=market, rights_gate=gate)

    first = await runner.run_batch(
        request(feed_delay=timedelta(seconds=2)),
        now=NOW + timedelta(seconds=3),
        max_pages=1,
    )
    assert not first.checkpoint.finished
    restored = RestBackfillCheckpoint.from_json(first.checkpoint.to_json())
    second = await runner.run_batch(
        request(feed_delay=timedelta(seconds=2)),
        now=NOW + timedelta(seconds=3),
        checkpoint=restored,
        max_pages=2,
    )

    assert second.coverage_complete
    assert second.checkpoint.bars_received == 4
    assert len(second.bars) == 2
    assert tuple(item.quote_volume for item in (*first.bars, *second.bars)) == (
        Decimal("151.5"),
        Decimal("151.5"),
        Decimal("151.5"),
        Decimal("151.5"),
    )
    assert second.bars[0].received_at == START + timedelta(minutes=3, seconds=1, milliseconds=999)
    assert second.bars[0].available_at == second.bars[0].received_at
    assert second.bars[0].source == "BINANCE_USDM_REST_BACKFILL"
    assert market.calls[0]["start_time"] == START
    assert market.calls[0]["end_time"] == NOW - timedelta(milliseconds=1)
    assert market.calls[1]["start_time"] == START + timedelta(minutes=2)
    assert all(call["limit"] == 2 for call in market.calls)
    assert gate.calls == 2

    with pytest.raises(RestBackfillError, match="request fingerprint"):
        await runner.run_batch(
            request(feed_delay=timedelta(seconds=1)),
            now=NOW + timedelta(seconds=3),
            checkpoint=restored,
        )


@pytest.mark.asyncio
async def test_rights_denial_or_changed_policy_fingerprint_stops_before_fetch() -> None:
    market = FakeMarketData(
        (page((bar(START), bar(START + timedelta(minutes=1))), complete=False),)
    )
    denied_gate = FakeRightsGate(evidence(purpose=BackfillRightsPurpose.PAPER_DEMO))
    runner = BinanceRestBackfillRunner(market_data=market, rights_gate=denied_gate)

    with pytest.raises(PermissionError, match="mục đích"):
        await runner.run_batch(request(), now=NOW, max_pages=1)
    assert market.calls == []

    authorized = FakeRightsGate(evidence())
    runner = BinanceRestBackfillRunner(market_data=market, rights_gate=authorized)
    partial = await runner.run_batch(request(), now=NOW, max_pages=1)
    changed = FakeRightsGate(evidence(policy_fingerprint=hashlib.sha256(b"policy-v2").hexdigest()))
    resumed = BinanceRestBackfillRunner(market_data=market, rights_gate=changed)
    with pytest.raises(RestBackfillError, match="policy fingerprint"):
        await resumed.run_batch(request(), now=NOW, checkpoint=partial.checkpoint, max_pages=1)
    assert market.calls
    assert len(market.calls) == 1


@pytest.mark.asyncio
async def test_short_page_preserves_gap_and_never_interpolates_missing_bars() -> None:
    market = FakeMarketData((page((bar(START), bar(START + timedelta(minutes=2))), complete=True),))
    runner = BinanceRestBackfillRunner(market_data=market, rights_gate=FakeRightsGate(evidence()))

    result = await runner.run_batch(request(page_limit=3), now=NOW, max_pages=2)

    assert result.checkpoint.finished
    assert not result.coverage_complete
    assert result.missing_bars == 2
    assert tuple(item.open_time for item in result.bars) == (START, START + timedelta(minutes=2))


@pytest.mark.asyncio
async def test_provider_page_cannot_exceed_requested_limit() -> None:
    market = FakeMarketData(
        (
            page(
                (bar(START), bar(START + timedelta(minutes=1)), bar(START + timedelta(minutes=2))),
                complete=False,
            ),
        )
    )
    runner = BinanceRestBackfillRunner(market_data=market, rights_gate=FakeRightsGate(evidence()))

    with pytest.raises(RestBackfillError, match="vượt page_limit"):
        await runner.run_batch(request(page_limit=2), now=NOW, max_pages=1)


@pytest.mark.asyncio
async def test_weekly_kline_grid_uses_monday_utc_anchor() -> None:
    weekly_request = build_trailing_365_day_rest_request("BTCUSDT", "1w", now=NOW)
    valid_market = FakeMarketData(
        (page((bar(weekly_request.start, interval="1w"),), complete=True),)
    )
    runner = BinanceRestBackfillRunner(
        market_data=valid_market,
        rights_gate=FakeRightsGate(evidence()),
    )

    result = await runner.run_batch(weekly_request, now=NOW, max_pages=1)

    assert result.bars[0].open_time == weekly_request.start
    assert not result.coverage_complete
    invalid_market = FakeMarketData(
        (
            page(
                (bar(weekly_request.start + timedelta(days=4), interval="1w"),),
                complete=True,
            ),
        )
    )
    runner = BinanceRestBackfillRunner(
        market_data=invalid_market,
        rights_gate=FakeRightsGate(evidence()),
    )
    with pytest.raises(RestBackfillError, match="lệch lưới"):
        await runner.run_batch(weekly_request, now=NOW, max_pages=1)


@pytest.mark.asyncio
async def test_duplicate_out_of_order_future_or_open_bars_are_rejected() -> None:
    cases = (
        (bar(START), bar(START)),
        (bar(START + timedelta(minutes=1)), bar(START)),
        (bar(NOW, received_at=NOW + timedelta(minutes=2)),),
        (replace(bar(START), is_closed=False),),
    )
    for records in cases:
        market = FakeMarketData((page(records, complete=True),))
        runner = BinanceRestBackfillRunner(
            market_data=market, rights_gate=FakeRightsGate(evidence())
        )
        with pytest.raises(RestBackfillError):
            await runner.run_batch(request(), now=NOW + timedelta(minutes=2), max_pages=1)


def test_checkpoint_rejects_tampering_and_request_mismatch() -> None:
    checkpoint = RestBackfillCheckpoint.start(
        request(), policy_fingerprint=POLICY_FINGERPRINT, now=NOW
    )
    payload = checkpoint.to_json().replace(POLICY_FINGERPRINT.encode(), b"not-a-valid-hash")

    with pytest.raises(RestBackfillError, match="không hợp lệ"):
        RestBackfillCheckpoint.from_json(payload)


@pytest.mark.asyncio
async def test_rights_gate_is_checked_before_each_page_and_checkpoint_binds_request() -> None:
    market = FakeMarketData(
        (
            page((bar(START), bar(START + timedelta(minutes=1))), complete=False),
            page(
                (bar(START + timedelta(minutes=2)), bar(START + timedelta(minutes=3))),
                complete=False,
            ),
        )
    )
    gate = FakeRightsGate(evidence())
    runner = BinanceRestBackfillRunner(market_data=market, rights_gate=gate)

    result = await runner.run_batch(request(), now=NOW, max_pages=2)

    assert result.coverage_complete
    assert gate.calls == 2
    with pytest.raises(RestBackfillError, match="request fingerprint"):
        await runner.run_batch(request(symbol="ETHUSDT"), now=NOW, checkpoint=result.checkpoint)


@pytest.mark.asyncio
async def test_rights_evidence_uses_database_checked_at_when_database_clock_is_ahead() -> None:
    market = FakeMarketData((page((bar(START),), complete=True),))
    database_now = NOW + timedelta(seconds=1)
    value = evidence(
        verified_at=NOW - timedelta(days=1),
        checked_at=database_now,
        valid_until=database_now + timedelta(minutes=5),
    )
    runner = BinanceRestBackfillRunner(market_data=market, rights_gate=FakeRightsGate(value))

    result = await runner.run_batch(request(), now=NOW, max_pages=1)

    assert len(result.bars) == 1
    assert result.bars[0].source == "BINANCE_USDM_REST_BACKFILL"
    assert len(market.calls) == 1


@pytest.mark.asyncio
async def test_expired_rights_or_bad_page_checksum_never_returns_bars() -> None:
    market = FakeMarketData((page((bar(START),), complete=True),))
    expired_gate = FakeRightsGate(
        evidence(valid_until=NOW - timedelta(seconds=1), verified_at=NOW - timedelta(days=2))
    )
    runner = BinanceRestBackfillRunner(market_data=market, rights_gate=expired_gate)

    with pytest.raises(PermissionError, match="hết hạn"):
        await runner.run_batch(request(page_limit=2), now=NOW, max_pages=1)
    assert not market.calls

    good_gate = FakeRightsGate(evidence())
    broken_page = replace(page((bar(START),), complete=True), checksum="0" * 64)
    market = FakeMarketData((broken_page,))
    runner = BinanceRestBackfillRunner(market_data=market, rights_gate=good_gate)
    with pytest.raises(RestBackfillError, match="Checksum"):
        await runner.run_batch(request(page_limit=2), now=NOW, max_pages=1)
