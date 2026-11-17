from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.application.live_kline_ingestion import (
    LIVE_BAR_SOURCE,
    LiveBarDisposition,
    LiveIngestionError,
    LiveKlineIngestionService,
    LiveKlineSubscription,
)
from trading_bot.domain.market.ingestion import (
    IngestionCursor,
    IngestionGap,
    InstrumentObservation,
    MarketRecordVersion,
    MarketStream,
)
from trading_bot.domain.market.models import Instrument, OHLCVBar
from trading_bot.ports.market_data import ExchangeInstrument, MarketDataPage

WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")
INTERVAL = timedelta(minutes=15)
T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
SUB = LiveKlineSubscription("BTCUSDT", "15m")
STREAM = MarketStream(LIVE_BAR_SOURCE, "BTCUSDT", "15m")


def make_bar(open_time: datetime, *, received_at: datetime, closed: bool = True) -> OHLCVBar:
    close_time = open_time + INTERVAL - timedelta(milliseconds=1)
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe="15m",
        open_time=open_time,
        close_time=close_time,
        event_at=min(close_time, received_at),
        received_at=received_at,
        available_at=received_at,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("1.5"),
        quote_volume=Decimal("150"),
        is_closed=closed,
        source=LIVE_BAR_SOURCE,
        source_cursor=str(int(open_time.timestamp() * 1000)),
        checksum=hashlib.sha256(open_time.isoformat().encode()).hexdigest(),
    )


@dataclass
class FakeInstrumentRow:
    id: UUID


@dataclass
class FakeAppendResult:
    record_id: UUID


@dataclass
class FakeStore:
    cursor: IngestionCursor | None = None
    instrument_id: UUID | None = None
    bars: list[OHLCVBar] = field(default_factory=list)
    versions: list[MarketRecordVersion] = field(default_factory=list)
    gaps: list[IngestionGap] = field(default_factory=list)
    instruments: list[InstrumentObservation] = field(default_factory=list)
    commits: int = 0
    rollbacks: int = 0


class FakeRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.pending_bars: list[tuple[OHLCVBar, MarketRecordVersion]] = []
        self.pending_gaps: list[IngestionGap] = []
        self.pending_instruments: list[InstrumentObservation] = []
        self.pending_cursor: IngestionCursor | None = None
        self.pending_instrument_id: UUID | None = None

    async def get_cursor(
        self, *, workspace_id: UUID, stream: MarketStream, as_of: datetime
    ) -> IngestionCursor | None:
        assert workspace_id == WORKSPACE_ID
        assert stream == STREAM
        return self._store.cursor

    async def save_cursor(self, *, workspace_id: UUID, cursor: IngestionCursor) -> None:
        current = self._store.cursor
        assert current is None or cursor.covered_until >= current.covered_until
        self.pending_cursor = cursor

    async def get_instrument(
        self, *, workspace_id: UUID, source: str, symbol: str, as_of: datetime
    ) -> FakeInstrumentRow | None:
        assert source == LIVE_BAR_SOURCE
        if self._store.instrument_id is None:
            return None
        return FakeInstrumentRow(self._store.instrument_id)

    async def append_instrument(self, **kwargs: Any) -> FakeAppendResult:
        self.pending_instruments.append(kwargs["observation"])
        self.pending_instrument_id = uuid4()
        return FakeAppendResult(self.pending_instrument_id)

    async def append_bar(
        self,
        *,
        workspace_id: UUID,
        instrument_id: UUID,
        bar: OHLCVBar,
        version: MarketRecordVersion,
    ) -> object:
        assert instrument_id in {self._store.instrument_id, self.pending_instrument_id}
        assert version.stream == STREAM
        assert bar.is_closed
        self.pending_bars.append((bar, version))
        return object()

    async def append_gap(self, *, workspace_id: UUID, gap: IngestionGap) -> UUID:
        self.pending_gaps.append(gap)
        return uuid4()


class FakeRightsGate:
    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny
        self.requests: list[RestBackfillRequest] = []

    async def authorize(
        self, *, request: RestBackfillRequest, now: datetime
    ) -> BackfillRightsEvidence:
        self.requests.append(request)
        if self.deny:
            raise PermissionError("Policy hiện hành không cấp đúng mục đích backfill.")
        return BackfillRightsEvidence(
            workspace_id=WORKSPACE_ID,
            provider="BINANCE_USDM_REST",
            purpose=request.purpose,
            terms_url="https://www.binance.com/en/terms",
            policy_fingerprint=hashlib.sha256(b"policy").hexdigest(),
            verified_at=now - timedelta(hours=1),
            valid_until=now + timedelta(minutes=5),
        )


@dataclass
class FakeTransaction:
    market: FakeRepository
    rights: FakeRightsGate


class Harness:
    def __init__(self, *, deny_rights: bool = False) -> None:
        self.store = FakeStore()
        self.rights = FakeRightsGate(deny=deny_rights)
        self.kline_requests: list[dict[str, Any]] = []
        self.exchange_info_calls = 0
        self.available_until: datetime | None = None
        self.missing_opens: set[datetime] = set()
        self.clock = T0

    @asynccontextmanager
    async def transactions(self) -> Any:
        repository = FakeRepository(self.store)
        try:
            yield FakeTransaction(repository, self.rights)
        except BaseException:
            self.store.rollbacks += 1
            raise
        for bar, version in repository.pending_bars:
            self.store.bars.append(bar)
            self.store.versions.append(version)
        self.store.gaps.extend(repository.pending_gaps)
        self.store.instruments.extend(repository.pending_instruments)
        if repository.pending_instrument_id is not None:
            self.store.instrument_id = repository.pending_instrument_id
        if repository.pending_cursor is not None:
            self.store.cursor = repository.pending_cursor
        self.store.commits += 1

    async def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 500,
    ) -> MarketDataPage[OHLCVBar]:
        assert start_time is not None
        assert end_time is not None
        self.kline_requests.append({"start": start_time, "end": end_time, "limit": limit})
        records: list[OHLCVBar] = []
        open_time = start_time
        while open_time <= end_time and len(records) < limit:
            visible = self.available_until is None or open_time < self.available_until
            if visible and open_time not in self.missing_opens:
                records.append(make_bar(open_time, received_at=self.clock))
            open_time += INTERVAL
        return MarketDataPage(
            records=tuple(records),
            raw_payload=b"[]",
            checksum=hashlib.sha256(b"[]").hexdigest(),
            received_at=self.clock,
            available_at=self.clock,
        )

    async def get_exchange_info(self) -> MarketDataPage[ExchangeInstrument]:
        self.exchange_info_calls += 1
        instrument = ExchangeInstrument(
            observation=InstrumentObservation(
                instrument=Instrument(
                    symbol="BTCUSDT",
                    base_asset="BTC",
                    quote_asset="USDT",
                    tick_size=Decimal("0.1"),
                    quantity_step=Decimal("0.001"),
                    min_notional=Decimal("100"),
                ),
                source=LIVE_BAR_SOURCE,
                observed_at=self.clock,
            ),
            contract_type="PERPETUAL",
            margin_asset="USDT",
        )
        return MarketDataPage(
            records=(instrument,),
            raw_payload=b"{}",
            checksum=hashlib.sha256(b"{}").hexdigest(),
            received_at=self.clock,
            available_at=self.clock,
        )

    def service(self, **kwargs: Any) -> LiveKlineIngestionService:
        return LiveKlineIngestionService(
            workspace_id=WORKSPACE_ID,
            market_data=self,
            transactions=self.transactions,
            **kwargs,
        )


@pytest.mark.asyncio
async def test_first_catch_up_loads_warmup_window_registers_instrument_and_sets_cursor() -> None:
    harness = Harness()
    harness.clock = T0 + timedelta(minutes=7)

    result = await harness.service(warmup_bars=200).catch_up(SUB, now=harness.clock)

    assert result.bars_appended == 200
    assert result.complete is True
    assert result.covered_until == T0
    assert harness.store.bars[0].open_time == T0 - 200 * INTERVAL
    assert harness.store.bars[-1].open_time == T0 - INTERVAL
    assert all(bar.source == LIVE_BAR_SOURCE and bar.is_closed for bar in harness.store.bars)
    assert harness.store.cursor is not None
    assert harness.store.cursor.covered_until == T0
    assert harness.exchange_info_calls == 1
    assert len(harness.store.instruments) == 1
    assert {request.purpose for request in harness.rights.requests} == {
        BackfillRightsPurpose.PAPER_DEMO
    }
    # Khóa idempotency cố định theo nguồn/symbol/timeframe/open_time để retry không nhân bản.
    assert harness.store.versions[0].idempotency_key == (
        f"BINANCE_USDM:BTCUSDT:15m:{(T0 - 200 * INTERVAL).isoformat()}"
    )


@pytest.mark.asyncio
async def test_catch_up_resumes_from_cursor_and_pages_until_current_bar() -> None:
    harness = Harness()
    harness.clock = T0
    await harness.service(warmup_bars=200).catch_up(SUB, now=harness.clock)
    harness.kline_requests.clear()
    service = harness.service(warmup_bars=200, page_limit=3)
    harness.clock = T0 + 7 * INTERVAL + timedelta(seconds=2)

    result = await service.catch_up(SUB, now=harness.clock)

    assert result.bars_appended == 7
    assert result.covered_until == T0 + 7 * INTERVAL
    assert [request["start"] for request in harness.kline_requests] == [
        T0,
        T0 + 3 * INTERVAL,
        T0 + 6 * INTERVAL,
    ]
    assert harness.exchange_info_calls == 1
    opens = [bar.open_time for bar in harness.store.bars]
    assert opens == sorted(set(opens))


@pytest.mark.asyncio
async def test_catch_up_is_incomplete_when_rest_has_not_published_the_closed_bar() -> None:
    harness = Harness()
    service = harness.service(warmup_bars=200)
    harness.clock = T0
    await service.catch_up(SUB, now=harness.clock)
    harness.clock = T0 + INTERVAL + timedelta(milliseconds=200)
    harness.available_until = T0

    result = await service.catch_up(SUB, now=harness.clock)

    assert result.bars_appended == 0
    assert result.complete is False
    assert result.covered_until == T0


@pytest.mark.asyncio
async def test_catch_up_records_gap_when_exchange_omits_a_bar() -> None:
    harness = Harness()
    service = harness.service(warmup_bars=200)
    harness.clock = T0
    await service.catch_up(SUB, now=harness.clock)
    harness.missing_opens = {T0 + INTERVAL}
    harness.clock = T0 + 3 * INTERVAL + timedelta(seconds=1)

    result = await service.catch_up(SUB, now=harness.clock)

    assert result.bars_appended == 2
    assert len(harness.store.gaps) == 1
    gap = harness.store.gaps[0]
    assert (gap.gap_start, gap.gap_end) == (T0 + INTERVAL, T0 + 2 * INTERVAL)
    assert gap.stream == STREAM


@pytest.mark.asyncio
async def test_rights_denial_writes_nothing_and_makes_no_market_request() -> None:
    harness = Harness(deny_rights=True)

    with pytest.raises(PermissionError):
        await harness.service().catch_up(SUB, now=T0)

    assert harness.kline_requests == []
    assert harness.exchange_info_calls == 0
    assert harness.store.bars == []
    assert harness.store.cursor is None
    assert harness.store.commits == 0


@pytest.mark.asyncio
async def test_closed_stream_bar_at_cursor_is_appended_and_advances_cursor() -> None:
    harness = Harness()
    service = harness.service(warmup_bars=200)
    harness.clock = T0
    await service.catch_up(SUB, now=harness.clock)
    now = T0 + INTERVAL + timedelta(milliseconds=300)

    disposition = await service.ingest_closed_bar(make_bar(T0, received_at=now), now=now)

    assert disposition is LiveBarDisposition.APPENDED
    assert harness.store.bars[-1].open_time == T0
    assert harness.store.cursor is not None
    assert harness.store.cursor.covered_until == T0 + INTERVAL
    assert harness.rights.requests[-1].start == T0
    assert harness.rights.requests[-1].end == T0 + INTERVAL


@pytest.mark.asyncio
async def test_stream_bar_behind_cursor_is_duplicate_and_ahead_needs_catch_up() -> None:
    harness = Harness()
    service = harness.service(warmup_bars=200)
    harness.clock = T0
    await service.catch_up(SUB, now=harness.clock)
    count = len(harness.store.bars)
    now = T0 + 3 * INTERVAL

    behind = await service.ingest_closed_bar(make_bar(T0 - INTERVAL, received_at=now), now=now)
    ahead = await service.ingest_closed_bar(make_bar(T0 + INTERVAL, received_at=now), now=now)

    assert behind is LiveBarDisposition.DUPLICATE
    assert ahead is LiveBarDisposition.NEEDS_CATCH_UP
    assert len(harness.store.bars) == count
    assert harness.store.cursor is not None
    assert harness.store.cursor.covered_until == T0


@pytest.mark.asyncio
async def test_stream_bar_without_cursor_needs_catch_up() -> None:
    harness = Harness()
    now = T0 + INTERVAL + timedelta(seconds=1)

    disposition = await harness.service().ingest_closed_bar(make_bar(T0, received_at=now), now=now)

    assert disposition is LiveBarDisposition.NEEDS_CATCH_UP
    assert harness.store.bars == []


@pytest.mark.asyncio
async def test_forming_or_foreign_source_bar_is_rejected() -> None:
    harness = Harness()
    now = T0 + timedelta(minutes=3)
    service = harness.service()

    with pytest.raises(LiveIngestionError):
        await service.ingest_closed_bar(make_bar(T0, received_at=now, closed=False), now=now)
    later = T0 + INTERVAL + timedelta(seconds=1)
    foreign = make_bar(T0, received_at=later)
    object.__setattr__(foreign, "source", "BINANCE_USDM_REST_BACKFILL")
    with pytest.raises(LiveIngestionError):
        await service.ingest_closed_bar(foreign, now=later)


@pytest.mark.asyncio
async def test_rights_denial_blocks_stream_bar() -> None:
    harness = Harness()
    service = harness.service(warmup_bars=200)
    harness.clock = T0
    await service.catch_up(SUB, now=harness.clock)
    count = len(harness.store.bars)
    harness.rights.deny = True
    now = T0 + INTERVAL + timedelta(seconds=1)

    with pytest.raises(PermissionError):
        await service.ingest_closed_bar(make_bar(T0, received_at=now), now=now)

    assert len(harness.store.bars) == count


def test_subscription_validates_symbol_and_interval() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        LiveKlineSubscription("btcusdt", "15m")
    with pytest.raises(ValueError):  # noqa: PT011
        LiveKlineSubscription("BTCUSDT", "1M")
