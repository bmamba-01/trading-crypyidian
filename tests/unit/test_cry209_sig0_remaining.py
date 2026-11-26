"""Unit test TDD cho CRY-209: phần còn lại SIG-0.

Bao phủ 5 hạng mục:
1. Parser Binance Vision CSV giữ trường taker (trade_count, taker_buy_base_volume,
   taker_buy_quote_volume), kiểm định số học và xử lý dòng header tùy chọn.
2. BacktestDatasetManifest schema v5 ghim trường taker, fail-closed khi chiến lược yêu cầu
   taker nhưng thiếu dữ liệu, đồng thời giữ tương thích 100% reader/hash v2/v3/v4.
3. Ghi bền vững `ingestion_gap` khi thiếu nến trong backfill REST và collector SIG-0.
4. Xử lý xung đột khóa idempotency khi backfill `feed_delay > 0` trùng khoảng (không hỏng
   nến cũ, không crash, vẫn fail-closed khi nội dung OHLCV/taker khác).
5. WebSocket `/market` parse trường taker + cấu hình/wiring chuyển collector sang WS `/market`
   và chặn chạy trùng lặp với `ingestion_live_symbols`.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from trading_bot.adapters.binance_usdm.market_stream import (
    MarketDataContractError,
    parse_kline_stream_message,
)
from trading_bot.adapters.binance_usdm.vision_archive import (
    ArchiveRightsGrant,
    ArchiveRightsPurpose,
    HistoricalBackfillError,
    VisionArchive,
    append_archive_to_market_repository,
    inspect_kline_coverage,
    parse_kline_archive,
)
from trading_bot.adapters.database.repositories.market_data import (
    MarketDataRepository,
    MarketRepositoryConflict,
)
from trading_bot.application.backtest_dataset_manifest import (
    BacktestDatasetManifest,
    backtest_dataset_bars_hash,
    build_backtest_dataset_manifest,
)
from trading_bot.application.backtest_dataset_replay import (
    BacktestDatasetDataChanged,
    BacktestDatasetReplayInput,
    BacktestDatasetReplayLoader,
)
from trading_bot.application.backtest_dataset_replay_bundle import (
    BacktestDatasetBundleMismatch,
    build_backtest_dataset_replay_bundle,
)
from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    BinanceRestBackfillRunner,
    RestBackfillRequest,
)
from trading_bot.application.binance_rest_backfill_ingestion import (
    BinanceRestBackfillIngestionService,
    make_backfill_bar_version,
)
from trading_bot.application.market_bar_collector import (
    MarketBarCollector,
    parse_collector_streams,
)
from trading_bot.config.settings import AppSettings
from trading_bot.domain.market.ingestion import (
    IngestionCursor,
    IngestionDisposition,
    IngestionGap,
    InstrumentObservation,
    MarketRecordVersion,
    MarketStream,
)
from trading_bot.domain.market.models import Instrument, MarketBarSourceIdentity, OHLCVBar
from trading_bot.ports.market_data import MarketDataPage

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
START = NOW - timedelta(minutes=4)
WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")
INSTRUMENT_ID = UUID("22222222-2222-4222-8222-222222222222")
POLICY_FINGERPRINT = hashlib.sha256(b"cry209-policy-v1").hexdigest()
ARCHIVE_URL = (
    "https://data.binance.vision/data/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2026-11-01.zip"
)
VISION_TERMS_URL = (
    "https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md"
)


def _make_zip(csv_name: str, content: bytes) -> VisionArchive:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(csv_name, content)
    payload = output.getvalue()
    checksum = hashlib.sha256(payload).hexdigest()
    return VisionArchive(
        url=ARCHIVE_URL,
        checksum=checksum,
        publisher_checksum=checksum,
        payload=payload,
        retrieved_at=NOW,
    )


def _vision_grant() -> ArchiveRightsGrant:
    return ArchiveRightsGrant(
        provider="BINANCE_VISION",
        purpose=ArchiveRightsPurpose.PERSONAL_NONCOMMERCIAL_RESEARCH,
        terms_url=VISION_TERMS_URL,
        terms_checked_at=NOW - timedelta(days=1),
        verified_at=NOW - timedelta(hours=1),
        enabled=True,
        personal_noncommercial_acknowledged=True,
        approval_reference="cry209-research-approval",
    )


# ---------------------------------------------------------------------------
# 1. Parser Binance Vision CSV giữ trường taker + header tùy chọn
# ---------------------------------------------------------------------------


def test_vision_csv_parser_preserves_taker_fields_and_skips_optional_header() -> None:
    csv_with_header = (
        b"open_time,open,high,low,close,volume,close_time,quote_volume,count,"
        b"taker_buy_volume,taker_buy_quote_volume,ignore\n"
        b"1793491200000,100.10,101.25,99.90,100.80,12.345,1793491259999,"
        b"1234.50,42,7.125,712.25,0\n"
        b"1793491260000,100.80,102.00,100.50,101.75,8.0,1793491319999,"
        b"820.0,31,5.0,510.0,0\n"
    )
    archive = _make_zip("BTCUSDT-1m-2026-11-01.csv", csv_with_header)
    rows = parse_kline_archive(archive, symbol="BTCUSDT", interval="1m")

    assert len(rows) == 2
    assert rows[0].trade_count == 42
    assert rows[0].taker_buy_base_volume == Decimal("7.125")
    assert rows[0].taker_buy_quote_volume == Decimal("712.25")
    assert rows[1].trade_count == 31
    assert rows[1].taker_buy_base_volume == Decimal("5.0")
    assert rows[1].taker_buy_quote_volume == Decimal("510.0")

    bar = rows[0].to_simulated_backtest_bar()
    assert bar.trade_count == 42
    assert bar.taker_buy_base_volume == Decimal("7.125")
    assert bar.taker_buy_quote_volume == Decimal("712.25")


@pytest.mark.parametrize(
    ("count", "taker_base", "taker_quote"),
    [
        ("-1", "5.0", "500.0"),
        ("1.5", "5.0", "500.0"),
        ("42", "-0.1", "500.0"),
        ("42", "13.0", "500.0"),  # > volume (12.345)
        ("42", "5.0", "-1.0"),
        ("42", "5.0", "1234.51"),  # > quote_volume (1234.50)
    ],
)
def test_vision_csv_parser_rejects_invalid_taker_fields(
    count: str, taker_base: str, taker_quote: str
) -> None:
    csv_body = (
        f"1793491200000,100.10,101.25,99.90,100.80,12.345,1793491259999,"
        f"1234.50,{count},{taker_base},{taker_quote},0\n"
    ).encode()
    archive = _make_zip("BTCUSDT-1m-2026-11-01.csv", csv_body)
    with pytest.raises(HistoricalBackfillError):
        parse_kline_archive(archive, symbol="BTCUSDT", interval="1m")


@pytest.mark.asyncio
async def test_vision_archive_append_persists_taker_fields_to_repository() -> None:
    csv_body = (
        b"1793491200000,100.10,101.25,99.90,100.80,12.345,1793491259999,1234.50,42,7.125,712.25,0\n"
    )
    archive = _make_zip("BTCUSDT-1m-2026-11-01.csv", csv_body)
    rows = parse_kline_archive(archive, symbol="BTCUSDT", interval="1m")
    coverage = inspect_kline_coverage(
        rows,
        start=rows[0].open_time,
        end=rows[0].open_time + timedelta(minutes=1),
        interval="1m",
    )

    stored_bars: list[OHLCVBar] = []

    class _Repo:
        async def append_raw_reference(self, **kwargs: object) -> Any:
            return SimpleNamespace(disposition=IngestionDisposition.ACCEPT, record_id=uuid4())

        async def append_bar(self, **kwargs: object) -> Any:
            stored_bars.append(kwargs["bar"])  # type: ignore[arg-type]
            return SimpleNamespace(disposition=IngestionDisposition.ACCEPT, record_id=uuid4())

    await append_archive_to_market_repository(
        _Repo(),
        workspace_id=WORKSPACE_ID,
        instrument_id=INSTRUMENT_ID,
        archive=archive,
        bars=rows,
        raw_object_key="vision/BTCUSDT-1m-2026-11-01.zip",
        grant=_vision_grant(),
        request_now=NOW,
        coverage=coverage,
    )
    assert len(stored_bars) == 1
    assert stored_bars[0].trade_count == 42
    assert stored_bars[0].taker_buy_base_volume == Decimal("7.125")
    assert stored_bars[0].taker_buy_quote_volume == Decimal("712.25")


# ---------------------------------------------------------------------------
# 2. Manifest v5 ghim trường taker & tương thích ngược v2/v3/v4
# ---------------------------------------------------------------------------


def _manifest_request() -> RestBackfillRequest:
    return RestBackfillRequest(
        symbol="BTCUSDT",
        interval="1m",
        start=START,
        end=NOW,
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        page_limit=10,
    )


def _manifest_evidence() -> BackfillRightsEvidence:
    return BackfillRightsEvidence(
        workspace_id=WORKSPACE_ID,
        provider="BINANCE_USDM_REST",
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        terms_url="https://www.binance.com/en/terms",
        policy_fingerprint=POLICY_FINGERPRINT,
        verified_at=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
    )


def _manifest_bar(
    open_time: datetime,
    *,
    with_taker: bool = True,
    with_identity: bool = False,
    taker_buy_base: Decimal = Decimal("0.8"),
) -> OHLCVBar:
    checksum = hashlib.sha256(open_time.isoformat().encode()).hexdigest()
    identity = (
        MarketBarSourceIdentity(
            workspace_id=WORKSPACE_ID,
            record_id=uuid4(),
            logical_key=f"BINANCE_USDM_REST_BACKFILL:BTCUSDT:1m:{open_time.isoformat()}",
            revision=1,
            supersedes_record_id=None,
            checksum=checksum,
        )
        if with_identity
        else None
    )
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
        quote_volume=Decimal("151.5"),
        trade_count=12 if with_taker else None,
        taker_buy_base_volume=taker_buy_base if with_taker else None,
        taker_buy_quote_volume=Decimal("80.8") if with_taker else None,
        is_closed=True,
        source="BINANCE_USDM_REST_BACKFILL",
        source_cursor=str(int(open_time.timestamp() * 1000)),
        checksum=checksum,
        source_identity=identity,
    )


def test_manifest_v5_pins_taker_fields_and_preserves_v3_v4_hashes() -> None:
    bars_a = tuple(
        _manifest_bar(
            START + timedelta(minutes=i),
            with_taker=True,
            with_identity=True,
            taker_buy_base=Decimal("0.8"),
        )
        for i in range(4)
    )
    bars_b = tuple(replace(b, taker_buy_base_volume=Decimal("0.9")) for b in bars_a)

    # v3 và v4 không thay đổi hash khi chỉ đổi trường taker (tương thích ngược bit-for-bit).
    assert backtest_dataset_bars_hash(bars_a, schema_version=3) == backtest_dataset_bars_hash(
        bars_b, schema_version=3
    )
    assert backtest_dataset_bars_hash(bars_a, schema_version=4) == backtest_dataset_bars_hash(
        bars_b, schema_version=4
    )
    # v5 PHẢI thay đổi hash khi trường taker thay đổi.
    assert backtest_dataset_bars_hash(bars_a, schema_version=5) != backtest_dataset_bars_hash(
        bars_b, schema_version=5
    )

    m5 = build_backtest_dataset_manifest(
        _manifest_request(),
        evaluation_start=START + timedelta(minutes=2),
        warmup_bars=2,
        bars=bars_a,
        rights_evidence=_manifest_evidence(),
        prepared_at=NOW,
        schema_version=5,
        require_taker_flow=True,
    )
    assert m5.schema_version == 5
    assert m5.has_pinned_taker_flow is True
    assert m5.has_pinned_source_identities is True

    restored = BacktestDatasetManifest.from_canonical_json(
        m5.canonical_json, expected_dataset_hash=m5.dataset_hash
    )
    assert restored == m5


def test_manifest_v5_require_taker_flow_fails_closed_when_missing() -> None:
    bars_no_taker = tuple(
        _manifest_bar(START + timedelta(minutes=i), with_taker=False) for i in range(4)
    )
    bars_with_taker = tuple(
        _manifest_bar(START + timedelta(minutes=i), with_taker=True) for i in range(4)
    )
    # Chiến lược yêu cầu taker nhưng dùng schema v3 -> fail-closed.
    with pytest.raises(ValueError, match="taker"):
        build_backtest_dataset_manifest(
            _manifest_request(),
            evaluation_start=START + timedelta(minutes=2),
            warmup_bars=2,
            bars=bars_with_taker,
            rights_evidence=_manifest_evidence(),
            prepared_at=NOW,
            schema_version=3,
            require_taker_flow=True,
        )
    # Chiến lược yêu cầu taker trên v5 nhưng bar thiếu taker -> fail-closed.
    with pytest.raises(ValueError, match="taker"):
        build_backtest_dataset_manifest(
            _manifest_request(),
            evaluation_start=START + timedelta(minutes=2),
            warmup_bars=2,
            bars=bars_no_taker,
            rights_evidence=_manifest_evidence(),
            prepared_at=NOW,
            schema_version=5,
            require_taker_flow=True,
        )


@pytest.mark.asyncio
async def test_replay_loader_and_bundle_enforce_require_taker_flow() -> None:
    bars_v3 = tuple(_manifest_bar(START + timedelta(minutes=i), with_taker=True) for i in range(4))
    m3 = build_backtest_dataset_manifest(
        _manifest_request(),
        evaluation_start=START + timedelta(minutes=2),
        warmup_bars=2,
        bars=bars_v3,
        rights_evidence=_manifest_evidence(),
        prepared_at=NOW,
        schema_version=3,
    )

    class _ManifestLookup:
        async def get_by_hash(
            self, *, workspace_id: UUID, dataset_hash: str
        ) -> BacktestDatasetManifest:
            return m3

    class _Gate:
        async def authorize(self, **_: object) -> BackfillRightsEvidence:
            return _manifest_evidence()

    class _Market:
        async def list_bars(self, **_: object) -> list[OHLCVBar]:
            return list(bars_v3)

    loader = BacktestDatasetReplayLoader(
        repository=_ManifestLookup(),
        rights_gate=_Gate(),
        market_data=_Market(),
    )
    # Không yêu cầu taker -> v3 vẫn nạp thành công bình thường.
    loaded = await loader.load_for_replay(
        workspace_id=WORKSPACE_ID, dataset_hash=m3.dataset_hash, now=NOW
    )
    assert loaded.manifest == m3
    # Yêu cầu taker -> fail-closed vì v3 không ghim trường taker.
    with pytest.raises(BacktestDatasetDataChanged, match="taker"):
        await loader.load_for_replay(
            workspace_id=WORKSPACE_ID,
            dataset_hash=m3.dataset_hash,
            now=NOW,
            require_taker_flow=True,
        )
    # Bundle cũng fail-closed khi require_taker_flow=True với manifest v3.
    with pytest.raises(BacktestDatasetBundleMismatch, match="taker"):
        build_backtest_dataset_replay_bundle(
            (BacktestDatasetReplayInput(m3, bars_v3, _manifest_evidence()),),
            require_taker_flow=True,
        )


# ---------------------------------------------------------------------------
# 3. Ghi bền vững `ingestion_gap` khi thiếu nến (backfill REST + collector)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_backfill_ingestion_persists_ingestion_gaps_for_missing_bars() -> None:
    # Request 4 phút: [START, NOW). Chỉ trả nến phút 0 và phút 3 -> thiếu [START+1m, START+3m).
    bar0 = _manifest_bar(START, with_taker=True)
    bar3 = _manifest_bar(START + timedelta(minutes=3), with_taker=True)
    payload = b"gap-page"
    page = MarketDataPage(
        records=(bar0, bar3),
        raw_payload=payload,
        checksum=hashlib.sha256(payload).hexdigest(),
        received_at=NOW,
        available_at=NOW,
        is_complete=True,
    )

    class _Market:
        async def get_klines(self, **_: object) -> MarketDataPage[OHLCVBar]:
            return page

    class _Rights:
        async def authorize(self, **_: object) -> BackfillRightsEvidence:
            return _manifest_evidence()

    @dataclass
    class _Repo:
        cursors: dict[MarketStream, IngestionCursor] = field(default_factory=dict)
        bars: list[OHLCVBar] = field(default_factory=list)
        gaps: list[IngestionGap] = field(default_factory=list)

        async def get_cursor(
            self, *, workspace_id: UUID, stream: MarketStream, as_of: datetime
        ) -> IngestionCursor | None:
            return self.cursors.get(stream)

        async def append_bar(
            self,
            *,
            workspace_id: UUID,
            instrument_id: UUID,
            bar: OHLCVBar,
            version: MarketRecordVersion,
        ) -> object:
            self.bars.append(bar)
            return SimpleNamespace(disposition=IngestionDisposition.ACCEPT, record_id=uuid4())

        async def save_cursor(self, *, workspace_id: UUID, cursor: IngestionCursor) -> None:
            self.cursors[cursor.stream] = cursor

        async def append_gap(self, *, workspace_id: UUID, gap: IngestionGap) -> UUID:
            assert workspace_id == WORKSPACE_ID
            self.gaps.append(gap)
            return uuid4()

    repo = _Repo()
    service = BinanceRestBackfillIngestionService(
        runner=BinanceRestBackfillRunner(market_data=_Market(), rights_gate=_Rights()),
        repository=repo,
        workspace_id=WORKSPACE_ID,
        instrument_id=INSTRUMENT_ID,
    )
    batch = await service.run_batch(_manifest_request(), now=NOW, max_pages=1)
    assert batch.missing_bars == 2
    assert len(repo.gaps) == 1
    assert repo.gaps[0].gap_start == START + timedelta(minutes=1)
    assert repo.gaps[0].gap_end == START + timedelta(minutes=3)
    assert repo.gaps[0].stream == MarketStream("BINANCE_USDM_REST_BACKFILL", "BTCUSDT", "1m")


@pytest.mark.asyncio
async def test_market_bar_collector_persists_ingestion_gaps_when_middle_bars_missing() -> None:
    tick_now = NOW + timedelta(seconds=10)
    # initial_bars=4 -> window [START, NOW). Trả phút 0 và phút 3 -> thiếu phút 1..2.
    bar0 = _manifest_bar(START, with_taker=True)
    bar3 = _manifest_bar(START + timedelta(minutes=3), with_taker=True)
    payload = b"collector-gap"

    class _Binance:
        async def get_klines(self, **_: object) -> MarketDataPage[OHLCVBar]:
            return MarketDataPage(
                records=(bar0, bar3),
                raw_payload=payload,
                checksum=hashlib.sha256(payload).hexdigest(),
                received_at=tick_now,
                available_at=tick_now,
                is_complete=True,
            )

        async def get_exchange_info(self) -> Any:
            instrument = Instrument(
                symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
                tick_size=Decimal("0.1"),
                quantity_step=Decimal("0.001"),
                min_notional=Decimal("5"),
                status="TRADING",
            )
            return SimpleNamespace(
                records=(
                    SimpleNamespace(
                        observation=InstrumentObservation(
                            instrument=instrument, source="BINANCE_USDM", observed_at=tick_now
                        )
                    ),
                ),
                received_at=tick_now,
                available_at=tick_now,
            )

    class _Rights:
        async def authorize(
            self, *, request: RestBackfillRequest, now: datetime
        ) -> BackfillRightsEvidence:
            return BackfillRightsEvidence(
                workspace_id=WORKSPACE_ID,
                provider="BINANCE_USDM_REST",
                purpose=request.purpose,
                terms_url="https://www.binance.com/en/terms",
                policy_fingerprint=POLICY_FINGERPRINT,
                verified_at=now - timedelta(days=1),
                valid_until=now + timedelta(days=30),
                checked_at=now,
            )

    @dataclass
    class _Market:
        cursors: dict[tuple[str, str, str], IngestionCursor] = field(default_factory=dict)
        bars: list[OHLCVBar] = field(default_factory=list)
        gaps: list[IngestionGap] = field(default_factory=list)
        instrument_id: UUID = INSTRUMENT_ID

        async def get_cursor(self, **kwargs: Any) -> Any:
            stream = kwargs["stream"]
            return self.cursors.get((stream.source, stream.symbol, stream.timeframe))

        async def save_cursor(self, *, workspace_id: UUID, cursor: IngestionCursor) -> None:
            s = cursor.stream
            self.cursors[(s.source, s.symbol, s.timeframe)] = cursor

        async def get_instrument(self, **kwargs: Any) -> Any:
            return SimpleNamespace(id=self.instrument_id)

        async def append_instrument(self, **kwargs: Any) -> Any:
            return SimpleNamespace(record_id=self.instrument_id)

        async def append_bar(self, **kwargs: Any) -> Any:
            self.bars.append(kwargs["bar"])
            return SimpleNamespace(disposition=IngestionDisposition.ACCEPT, record_id=uuid4())

        async def append_gap(self, *, workspace_id: UUID, gap: IngestionGap) -> UUID:
            assert workspace_id == WORKSPACE_ID
            self.gaps.append(gap)
            return uuid4()

    market = _Market()

    @asynccontextmanager
    async def scope() -> Any:
        yield SimpleNamespace(market=market, rights_gate=_Rights())

    collector = MarketBarCollector(
        workspace_id=WORKSPACE_ID,
        streams=parse_collector_streams("BTCUSDT:1m"),
        market_data=_Binance(),  # type: ignore[arg-type]
        session_scope=scope,
        initial_bars=4,
    )
    tick = await collector.collect_once(tick_now)
    assert tick.succeeded
    assert tick.results[0].missing == 2
    assert len(market.gaps) == 1
    assert market.gaps[0].gap_start == START + timedelta(minutes=1)
    assert market.gaps[0].gap_end == START + timedelta(minutes=3)


# ---------------------------------------------------------------------------
# 4. Xử lý xung đột khóa idempotency khi backfill feed_delay > 0 trùng khoảng
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_market_repository_handles_feed_delay_overlap_idempotently_and_rejects_tamper() -> (
    None
):
    open_time = START
    close_time = open_time + timedelta(minutes=1) - timedelta(milliseconds=1)
    bar_delay0 = OHLCVBar(
        symbol="BTCUSDT",
        timeframe="1m",
        open_time=open_time,
        close_time=close_time,
        event_at=close_time,
        received_at=close_time,
        available_at=close_time,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("1.5"),
        quote_volume=Decimal("151.5"),
        trade_count=12,
        taker_buy_base_volume=Decimal("0.8"),
        taker_buy_quote_volume=Decimal("80.8"),
        is_closed=True,
        source="BINANCE_USDM_REST_BACKFILL",
        source_cursor=str(int(open_time.timestamp() * 1000)),
    )
    v_bar0, v0 = make_backfill_bar_version(workspace_id=WORKSPACE_ID, bar=bar_delay0)

    # Backfill chạy lại trên cùng khoảng với feed_delay = 5s: cùng OHLCV+taker, khác available_at.
    delayed_at = close_time + timedelta(seconds=5)
    bar_delay5 = replace(bar_delay0, received_at=delayed_at, available_at=delayed_at)
    v_bar5, v5 = make_backfill_bar_version(workspace_id=WORKSPACE_ID, bar=bar_delay5)
    assert v5.idempotency_key == v0.idempotency_key
    assert v5.content_checksum != v0.content_checksum

    existing_row = SimpleNamespace(
        id=v0.record_id,
        workspace_id=WORKSPACE_ID,
        instrument_id=INSTRUMENT_ID,
        symbol=v_bar0.symbol,
        timeframe=v_bar0.timeframe,
        source=v_bar0.source,
        logical_key=v0.logical_key,
        idempotency_key=v0.idempotency_key,
        revision=1,
        checksum=v0.content_checksum,
        supersedes_id=None,
        open_time=v_bar0.open_time,
        close_time=v_bar0.close_time,
        event_at=v_bar0.event_at,
        received_at=v_bar0.received_at,
        available_at=v_bar0.available_at,
        open=v_bar0.open,
        high=v_bar0.high,
        low=v_bar0.low,
        close=v_bar0.close,
        volume=v_bar0.volume,
        quote_volume=v_bar0.quote_volume,
        trade_count=v_bar0.trade_count,
        taker_buy_base_volume=v_bar0.taker_buy_base_volume,
        taker_buy_quote_volume=v_bar0.taker_buy_quote_volume,
        is_closed=v_bar0.is_closed,
    )

    class _FakeSession:
        def __init__(self, row: object) -> None:
            self._row = row

        async def scalar(self, _stmt: object) -> object:
            return self._row

    repo = MarketDataRepository(_FakeSession(existing_row))  # type: ignore[arg-type]
    result = await repo.append_bar(
        workspace_id=WORKSPACE_ID,
        instrument_id=INSTRUMENT_ID,
        bar=v_bar5,
        version=v5,
    )
    assert result.disposition is IngestionDisposition.DUPLICATE
    assert result.record_id == v0.record_id

    # Nếu nội dung OHLCV bị sửa (tamper) -> VẪN phải từ chối bằng MarketRepositoryConflict!
    tampered = replace(bar_delay5, close=Decimal("101.5"))
    v_tampered, vt = make_backfill_bar_version(workspace_id=WORKSPACE_ID, bar=tampered)
    with pytest.raises(MarketRepositoryConflict, match="idempotency"):
        await repo.append_bar(
            workspace_id=WORKSPACE_ID,
            instrument_id=INSTRUMENT_ID,
            bar=v_tampered,
            version=vt,
        )


# ---------------------------------------------------------------------------
# 5. WebSocket `/market` parse trường taker + chuyển collector sang WS `/market`
# ---------------------------------------------------------------------------


def test_ws_kline_parser_keeps_taker_fields_and_rejects_invalid_taker() -> None:
    open_ms = int(START.timestamp() * 1000)
    close_ms = int((START + timedelta(minutes=1) - timedelta(milliseconds=1)).timestamp() * 1000)
    payload = {
        "stream": "btcusdt@kline_1m",
        "data": {
            "e": "kline",
            "E": close_ms + 50,
            "s": "BTCUSDT",
            "k": {
                "t": open_ms,
                "T": close_ms,
                "s": "BTCUSDT",
                "i": "1m",
                "o": "100.0",
                "c": "101.0",
                "h": "102.0",
                "l": "99.0",
                "v": "10.0",
                "n": 42,
                "x": True,
                "q": "1010.0",
                "V": "6.25",
                "Q": "631.25",
            },
        },
    }
    bar = parse_kline_stream_message(
        json.dumps(payload), received_at=START + timedelta(minutes=1, seconds=1)
    )
    assert bar.trade_count == 42
    assert bar.taker_buy_base_volume == Decimal("6.25")
    assert bar.taker_buy_quote_volume == Decimal("631.25")

    # Taker buy vượt volume -> từ chối hợp đồng.
    payload["data"]["k"]["V"] = "10.01"  # type: ignore[index]
    with pytest.raises(MarketDataContractError):
        parse_kline_stream_message(
            json.dumps(payload), received_at=START + timedelta(minutes=1, seconds=1)
        )


def test_settings_market_collector_websocket_transport_and_overlap_guard() -> None:
    ws_enabled = AppSettings(
        _env_file=None,
        app_env="test",
        process_role="ingestion-worker",
        ingestion_workspace_id=str(WORKSPACE_ID),
        market_collector_enabled=True,
        market_collector_transport="websocket",
        market_collector_streams="BTCUSDT:1m,5m;ETHUSDT:15m",
    )
    assert ws_enabled.market_collector_transport == "websocket"
    assert ws_enabled.effective_live_kline_subscriptions == (
        ("BTCUSDT", "1m"),
        ("BTCUSDT", "5m"),
        ("ETHUSDT", "15m"),
    )

    # Không cho phép bật cả REST polling lẫn WS live ingestion trên cùng luồng (symbol, timeframe).
    with pytest.raises(ValidationError, match="trùng"):
        AppSettings(
            _env_file=None,
            app_env="test",
            process_role="ingestion-worker",
            ingestion_workspace_id=str(WORKSPACE_ID),
            market_collector_enabled=True,
            market_collector_transport="rest",
            market_collector_streams="BTCUSDT:15m,1h",
            ingestion_live_symbols="BTCUSDT",
            ingestion_live_timeframes="15m",
        )


@pytest.mark.asyncio
async def test_create_app_wires_websocket_collector_without_rest_polling() -> None:
    import asyncio

    from trading_bot.app import create_app

    captured: dict[str, Any] = {}

    class _DummyRightsWorker:
        last_tick_succeeded = True

        def __init__(self, **_: Any) -> None:
            pass

        async def run(self, stopping: asyncio.Event) -> None:
            await stopping.wait()

    class _DummyLiveWorker:
        is_healthy = True

        async def run(self, stopping: asyncio.Event) -> None:
            await stopping.wait()

    class _DummyRuntime:
        def __init__(self) -> None:
            self.worker = _DummyLiveWorker()
            self.event_worker = None
            self.quote_worker = None
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    def _live_factory(**kwargs: Any) -> _DummyRuntime:
        captured.update(kwargs)
        return _DummyRuntime()

    settings = AppSettings(
        _env_file=None,
        app_env="test",
        process_role="ingestion-worker",
        ingestion_workspace_id=str(WORKSPACE_ID),
        market_collector_enabled=True,
        market_collector_transport="websocket",
        market_collector_streams="BTCUSDT:1m,5m",
    )
    application = create_app(
        settings=settings,
        session_factory=object(),  # type: ignore[arg-type]
        rights_worker_factory=_DummyRightsWorker,
        live_market_ingestion_factory=_live_factory,
    )
    async with application.router.lifespan_context(application):
        assert captured["subscriptions"] == (("BTCUSDT", "1m"), ("BTCUSDT", "5m"))
        assert getattr(application.state, "live_market_worker", None) is not None
        assert getattr(application.state, "market_collector", None) is None
