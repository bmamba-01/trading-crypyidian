from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    BinanceRestBackfillRunner,
    RestBackfillRequest,
)
from trading_bot.application.binance_rest_backfill_ingestion import (
    BinanceRestBackfillIngestionService,
)
from trading_bot.domain.market.ingestion import IngestionCursor, MarketRecordVersion, MarketStream
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.ports.market_data import MarketDataPage

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
START = NOW - timedelta(minutes=2)
WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")
INSTRUMENT_ID = UUID("22222222-2222-4222-8222-222222222222")
POLICY_FINGERPRINT = hashlib.sha256(b"policy-v1").hexdigest()


def _bar(open_time: datetime) -> OHLCVBar:
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe="1m",
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1) - timedelta(milliseconds=1),
        event_at=open_time + timedelta(minutes=1) - timedelta(milliseconds=1),
        received_at=NOW,
        available_at=NOW,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("1.5"),
        is_closed=True,
        source="BINANCE_USDM",
        source_cursor=str(int(open_time.timestamp() * 1000)),
    )


def _page(bars: tuple[OHLCVBar, ...], *, complete: bool) -> MarketDataPage[OHLCVBar]:
    payload = f"page:{bars[0].open_time.isoformat() if bars else 'empty'}".encode()
    return MarketDataPage(
        records=bars,
        raw_payload=payload,
        checksum=hashlib.sha256(payload).hexdigest(),
        received_at=NOW,
        available_at=NOW,
        is_complete=complete,
    )


def _request(**overrides: object) -> RestBackfillRequest:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "start": START,
        "end": NOW,
        "purpose": BackfillRightsPurpose.BACKTEST_RESEARCH,
        "page_limit": 1,
    }
    values.update(overrides)
    return RestBackfillRequest(**values)  # type: ignore[arg-type]


class _Pages:
    def __init__(self, pages: tuple[MarketDataPage[OHLCVBar], ...]) -> None:
        self.pages = list(pages)

    async def get_klines(self, **_: object) -> MarketDataPage[OHLCVBar]:
        return self.pages.pop(0)


class _Rights:
    async def authorize(self, **_: object) -> BackfillRightsEvidence:
        return BackfillRightsEvidence(
            workspace_id=WORKSPACE_ID,
            provider="BINANCE_USDM_REST",
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
            terms_url="https://www.binance.com/en/terms",
            policy_fingerprint=POLICY_FINGERPRINT,
            verified_at=NOW - timedelta(days=1),
            valid_until=NOW + timedelta(days=1),
        )


class _Repository:
    def __init__(self) -> None:
        self.cursors: dict[MarketStream, IngestionCursor] = {}
        self.bars: dict[str, tuple[UUID, OHLCVBar, MarketRecordVersion]] = {}

    async def get_cursor(
        self, *, workspace_id: UUID, stream: MarketStream, as_of: datetime
    ) -> IngestionCursor | None:
        assert workspace_id == WORKSPACE_ID
        return self.cursors.get(stream)

    async def append_bar(
        self,
        *,
        workspace_id: UUID,
        instrument_id: UUID,
        bar: OHLCVBar,
        version: MarketRecordVersion,
    ) -> None:
        assert workspace_id == WORKSPACE_ID
        assert instrument_id == INSTRUMENT_ID
        previous = self.bars.get(version.idempotency_key)
        if previous is not None:
            assert previous[2].content_checksum == version.content_checksum
            return
        self.bars[version.idempotency_key] = (instrument_id, bar, version)

    async def save_cursor(self, *, workspace_id: UUID, cursor: IngestionCursor) -> None:
        assert workspace_id == WORKSPACE_ID
        self.cursors[cursor.stream] = cursor


@pytest.mark.asyncio
async def test_backfill_ingestion_appends_bars_and_checkpoint_for_atomic_resume() -> None:
    market = _Pages(
        (
            _page((_bar(START),), complete=False),
            _page((_bar(START + timedelta(minutes=1)),), complete=True),
        )
    )
    runner = BinanceRestBackfillRunner(market_data=market, rights_gate=_Rights())
    repository = _Repository()
    service = BinanceRestBackfillIngestionService(
        runner=runner,
        repository=repository,
        workspace_id=WORKSPACE_ID,
        instrument_id=INSTRUMENT_ID,
    )

    first = await service.run_batch(_request(), now=NOW, max_pages=1)
    second = await service.run_batch(_request(), now=NOW, max_pages=1)

    assert first.checkpoint.next_open_time == START + timedelta(minutes=1)
    assert not first.checkpoint.finished
    assert second.coverage_complete
    assert len(repository.bars) == 2
    assert len(repository.cursors) == 1
    cursor = next(iter(repository.cursors.values()))
    assert cursor.stream.source.startswith("BINANCE_RB_")
    assert cursor.covered_until == NOW
    assert cursor.token.encode("utf-8") == second.checkpoint.to_json()
    assert all(
        record[1].checksum == record[2].content_checksum for record in repository.bars.values()
    )


@pytest.mark.asyncio
async def test_backfill_ingestion_isolates_cursor_for_each_request_fingerprint() -> None:
    market = _Pages(
        (
            _page((_bar(START),), complete=False),
            _page((_bar(START + timedelta(minutes=1)),), complete=True),
        )
    )
    runner = BinanceRestBackfillRunner(market_data=market, rights_gate=_Rights())
    repository = _Repository()
    service = BinanceRestBackfillIngestionService(
        runner=runner,
        repository=repository,
        workspace_id=WORKSPACE_ID,
        instrument_id=INSTRUMENT_ID,
    )

    await service.run_batch(_request(), now=NOW, max_pages=1)
    second = await service.run_batch(
        _request(feed_delay=timedelta(seconds=1)),
        now=NOW + timedelta(seconds=2),
        max_pages=1,
    )

    assert second.checkpoint.finished
    assert second.checkpoint.missing_bars == 1
    assert len(repository.cursors) == 2
    assert len({cursor.stream.source for cursor in repository.cursors.values()}) == 2


@pytest.mark.asyncio
async def test_backfill_ingestion_rejects_corrupt_persisted_checkpoint_before_fetch() -> None:
    market = _Pages((_page((_bar(START),), complete=False),))
    runner = BinanceRestBackfillRunner(market_data=market, rights_gate=_Rights())
    repository = _Repository()
    service = BinanceRestBackfillIngestionService(
        runner=runner,
        repository=repository,
        workspace_id=WORKSPACE_ID,
        instrument_id=INSTRUMENT_ID,
    )
    stream = service.cursor_stream(_request())
    repository.cursors[stream] = IngestionCursor(
        stream=stream,
        token="not-json",
        covered_until=START + timedelta(minutes=1),
        updated_at=NOW,
    )

    with pytest.raises(RuntimeError, match="Checkpoint persistence"):
        await service.run_batch(_request(), now=NOW, max_pages=1)
    assert market.pages
