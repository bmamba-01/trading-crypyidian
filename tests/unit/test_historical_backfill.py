from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import httpx2
import pytest

from trading_bot.adapters.binance_usdm.vision_archive import (
    VISION_DATASET_LICENSE,
    ArchiveCheckpoint,
    ArchiveRightsGrant,
    ArchiveRightsPurpose,
    BackfillCheckpoint,
    BackfillManifest,
    BackfillRequest,
    BinanceVisionArchiveDownloader,
    DatasetRightsError,
    HistoricalBackfillError,
    VisionArchive,
    append_archive_to_market_repository,
    backfill_request_fingerprint,
    build_backfill_manifest,
    build_trailing_365_day_request,
    build_vision_archive_urls,
    inspect_kline_coverage,
    parse_kline_archive,
    parse_vision_checksum,
    require_vision_archive_rights,
)
from trading_bot.adapters.database.repositories.market_data import MarketAppendResult
from trading_bot.domain.market.ingestion import IngestionDisposition

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
TERMS_URL = "https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md"
ARCHIVE_URL = (
    "https://data.binance.vision/data/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2026-11-01.zip"
)


def personal_grant(**changes: object) -> ArchiveRightsGrant:
    values: dict[str, object] = {
        "provider": "BINANCE_VISION",
        "purpose": ArchiveRightsPurpose.PERSONAL_NONCOMMERCIAL_RESEARCH,
        "terms_url": TERMS_URL,
        "terms_checked_at": NOW - timedelta(days=1),
        "verified_at": NOW - timedelta(hours=1),
        "enabled": True,
        "personal_noncommercial_acknowledged": True,
        "approval_reference": "research-approval-1",
    }
    values.update(changes)
    return ArchiveRightsGrant(**values)  # type: ignore[arg-type]


def request(**changes: object) -> BackfillRequest:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "start": datetime(2026, 11, 1, tzinfo=UTC),
        "end": datetime(2026, 11, 3, tzinfo=UTC),
        "now": NOW,
        "archive_period": "daily",
    }
    values.update(changes)
    return BackfillRequest(**values)  # type: ignore[arg-type]


def make_zip(name: str, content: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, content)
    return output.getvalue()


def streamed_response(content: bytes) -> httpx2.Response:
    return httpx2.Response(200, stream=httpx2.ByteStream(content))


def sample_csv() -> bytes:
    return (
        b"1793491200000,100.10,101.25,99.90,100.80,12.345,1793491259999,"
        b"1234.50,42,7.125,712.25,0\n"
        b"1793491260000,100.80,102.00,100.50,101.75,8.0,1793491319999,"
        b"820.0,31,5.0,510.0,0\n"
    )


def test_vision_rights_are_fail_closed_and_scope_personal_research_only() -> None:
    with pytest.raises(DatasetRightsError, match="chưa được bật"):
        require_vision_archive_rights(
            None, purpose=ArchiveRightsPurpose.PERSONAL_NONCOMMERCIAL_RESEARCH, now=NOW
        )

    with pytest.raises(DatasetRightsError, match="phi thương mại cá nhân"):
        require_vision_archive_rights(
            personal_grant(personal_noncommercial_acknowledged=False),
            purpose=ArchiveRightsPurpose.PERSONAL_NONCOMMERCIAL_RESEARCH,
            now=NOW,
        )

    with pytest.raises(DatasetRightsError, match="không cho phép"):
        require_vision_archive_rights(
            personal_grant(),
            purpose=ArchiveRightsPurpose.PRODUCT_OR_DEMO,
            now=NOW,
        )

    with pytest.raises(DatasetRightsError, match="cũ"):
        require_vision_archive_rights(
            personal_grant(terms_checked_at=NOW - timedelta(days=31)),
            purpose=ArchiveRightsPurpose.PERSONAL_NONCOMMERCIAL_RESEARCH,
            now=NOW,
        )
    with pytest.raises(DatasetRightsError, match="license"):
        require_vision_archive_rights(
            personal_grant(license_id="MIT"),
            purpose=ArchiveRightsPurpose.PERSONAL_NONCOMMERCIAL_RESEARCH,
            now=NOW,
        )
    with pytest.raises(DatasetRightsError, match="Xác nhận quyền"):
        require_vision_archive_rights(
            personal_grant(verified_at=NOW - timedelta(days=31)),
            purpose=ArchiveRightsPurpose.PERSONAL_NONCOMMERCIAL_RESEARCH,
            now=NOW,
        )


def test_backfill_request_requires_supported_symbol_interval_and_closed_utc_range() -> None:
    with pytest.raises(ValueError, match="UTC"):
        request(start=datetime(2026, 11, 1, tzinfo=timezone(timedelta(hours=7))))
    with pytest.raises(ValueError, match="interval"):
        request(interval="17m")
    with pytest.raises(ValueError, match="symbol"):
        request(symbol="BTC/USDT")
    with pytest.raises(ValueError, match="tương lai"):
        request(end=NOW + timedelta(days=1))
    with pytest.raises(ValueError, match="366 ngày"):
        request(start=datetime(2025, 10, 1, tzinfo=UTC), end=datetime(2026, 11, 3, tzinfo=UTC))


def test_trailing_365_day_request_floors_to_latest_closed_utc_interval() -> None:
    now = datetime(2026, 11, 3, 12, 3, 30, 123456, tzinfo=UTC)
    minute = build_trailing_365_day_request("BTCUSDT", "1m", now=now)
    assert minute.end == datetime(2026, 11, 3, 12, 3, tzinfo=UTC)
    assert minute.start == datetime(2025, 11, 3, 12, 3, tzinfo=UTC)
    assert minute.end - minute.start == timedelta(days=365)
    assert minute.now == now

    exact_boundary = build_trailing_365_day_request(
        "ETHUSDT", "1h", now=datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    )
    assert exact_boundary.end == datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    assert exact_boundary.end - exact_boundary.start == timedelta(days=365)

    daily = build_trailing_365_day_request(
        "BTCUSDT", "1d", now=datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    )
    assert daily.end == datetime(2026, 11, 3, tzinfo=UTC)
    assert daily.end - daily.start == timedelta(days=365)

    with pytest.raises(ValueError, match="UTC"):
        build_trailing_365_day_request(
            "BTCUSDT",
            "1m",
            now=datetime(2026, 11, 3, tzinfo=timezone(timedelta(hours=7))),
        )
    with pytest.raises(ValueError, match="interval"):
        build_trailing_365_day_request("BTCUSDT", "1M", now=now)


def test_archive_urls_are_bounded_to_official_host_and_requested_period() -> None:
    archive_request = request(start=datetime(2026, 10, 30, tzinfo=UTC))
    urls = build_vision_archive_urls(archive_request)
    assert urls == (
        "https://data.binance.vision/data/futures/um/daily/klines/BTCUSDT/1m/"
        "BTCUSDT-1m-2026-10-30.zip",
        "https://data.binance.vision/data/futures/um/daily/klines/BTCUSDT/1m/"
        "BTCUSDT-1m-2026-10-31.zip",
        "https://data.binance.vision/data/futures/um/daily/klines/BTCUSDT/1m/"
        "BTCUSDT-1m-2026-11-01.zip",
        "https://data.binance.vision/data/futures/um/daily/klines/BTCUSDT/1m/"
        "BTCUSDT-1m-2026-11-02.zip",
    )
    monthly_urls = build_vision_archive_urls(
        request(
            start=datetime(2026, 10, 31, tzinfo=UTC),
            end=datetime(2026, 11, 2, tzinfo=UTC),
            archive_period="monthly",
        )
    )
    assert monthly_urls == (
        "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1m/"
        "BTCUSDT-1m-2026-10.zip",
        "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1m/"
        "BTCUSDT-1m-2026-11.zip",
    )


def test_checksum_parser_accepts_official_digest_and_rejects_malformed_text() -> None:
    digest = "a" * 64
    assert parse_vision_checksum(f"{digest}  BTCUSDT-1m-2026-11-01.zip\n".encode()) == digest
    assert parse_vision_checksum(f"SHA256 (archive.zip) = {digest}\n".encode()) == digest
    with pytest.raises(HistoricalBackfillError, match="SHA-256"):
        parse_vision_checksum(b"not a checksum\n")


def test_kline_archive_parser_uses_decimal_and_utc_milliseconds() -> None:
    archive_bytes = make_zip("BTCUSDT-1m-2026-11-01.csv", sample_csv())
    archive = VisionArchive(
        url="https://data.binance.vision/data/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2026-11-01.zip",
        checksum=hashlib.sha256(archive_bytes).hexdigest(),
        payload=archive_bytes,
        retrieved_at=NOW,
    )
    bars = parse_kline_archive(
        archive, symbol="BTCUSDT", interval="1m", max_uncompressed_bytes=1_000_000
    )
    assert len(bars) == 2
    assert bars[0].open_time == datetime.fromtimestamp(1793491200, UTC)
    assert str(bars[0].open) == "100.10"
    assert str(bars[0].volume) == "12.345"
    assert str(bars[0].quote_volume) == "1234.50"
    assert bars[0].close_time == datetime.fromtimestamp(1793491259999 / 1000, UTC)


def test_kline_archive_parser_rejects_bad_schema_bad_ohlc_and_zip_path_traversal() -> None:
    member_name = "BTCUSDT-1m-2026-11-01.csv"
    bad_schema = make_zip(member_name, b"1,2,3\n")
    archive = VisionArchive(ARCHIVE_URL, hashlib.sha256(bad_schema).hexdigest(), bad_schema, NOW)
    with pytest.raises(HistoricalBackfillError, match="12 cột"):
        parse_kline_archive(archive, symbol="BTCUSDT", interval="1m", max_uncompressed_bytes=1000)

    bad_ohlc = make_zip(
        member_name,
        b"1793491200000,100,90,95,101,1,1793491259999,1,1,1,1,0\n",
    )
    bad_archive = VisionArchive(ARCHIVE_URL, hashlib.sha256(bad_ohlc).hexdigest(), bad_ohlc, NOW)
    with pytest.raises(HistoricalBackfillError, match="OHLC"):
        parse_kline_archive(
            bad_archive, symbol="BTCUSDT", interval="1m", max_uncompressed_bytes=1000
        )

    traversal = make_zip("../../escape.csv", sample_csv())
    traversal_archive = VisionArchive(
        ARCHIVE_URL, hashlib.sha256(traversal).hexdigest(), traversal, NOW
    )
    with pytest.raises(HistoricalBackfillError, match="đường dẫn"):
        parse_kline_archive(
            traversal_archive, symbol="BTCUSDT", interval="1m", max_uncompressed_bytes=1000
        )

    reversed_csv = b"\n".join(reversed(sample_csv().splitlines())) + b"\n"
    out_of_order = make_zip(member_name, reversed_csv)
    ordered_archive = VisionArchive(
        ARCHIVE_URL, hashlib.sha256(out_of_order).hexdigest(), out_of_order, NOW
    )
    with pytest.raises(HistoricalBackfillError, match="không tăng"):
        parse_kline_archive(
            ordered_archive, symbol="BTCUSDT", interval="1m", max_uncompressed_bytes=1000
        )

    with pytest.raises(HistoricalBackfillError, match="không khớp symbol/interval"):
        parse_kline_archive(archive, symbol="ETHUSDT", interval="1m", max_uncompressed_bytes=1000)


def test_coverage_report_detects_missing_duplicate_and_unexpected_bars() -> None:
    archive_bytes = make_zip("BTCUSDT-1m-2026-11-01.csv", sample_csv())
    archive = VisionArchive(
        ARCHIVE_URL,
        hashlib.sha256(archive_bytes).hexdigest(),
        archive_bytes,
        NOW,
    )
    bars = parse_kline_archive(
        archive, symbol="BTCUSDT", interval="1m", max_uncompressed_bytes=1000
    )
    quality = inspect_kline_coverage(
        bars + (bars[1],),
        start=bars[0].open_time,
        end=bars[1].open_time + timedelta(minutes=2),
        interval="1m",
    )
    assert quality.expected_count == 3
    assert quality.actual_count == 2
    assert quality.duplicate_open_times == (bars[1].open_time,)
    assert quality.missing_open_times == (bars[1].open_time + timedelta(minutes=1),)
    assert not quality.is_complete
    with pytest.raises(ValueError, match="interval khác"):
        inspect_kline_coverage(
            bars[:1],
            start=bars[0].open_time,
            end=bars[0].open_time + timedelta(minutes=1),
            interval="5m",
        )


def test_checkpoint_rejects_duplicate_or_inconsistent_resume_cursor() -> None:
    receipt = ArchiveCheckpoint(
        url="https://data.binance.vision/a.zip",
        publisher_sha256="a" * 64,
        downloaded_sha256="a" * 64,
        byte_length=100,
        retrieved_at=NOW,
    )
    with pytest.raises(ValueError, match="next_index"):
        BackfillCheckpoint(completed=(receipt,), next_index=0)
    with pytest.raises(ValueError, match="trùng"):
        BackfillCheckpoint(
            request_fingerprint="a" * 64,
            completed=(receipt, receipt),
            next_index=2,
        )
    checkpoint = BackfillCheckpoint(
        request_fingerprint="b" * 64,
        completed=(receipt,),
        next_index=1,
    )
    assert BackfillCheckpoint.from_json(checkpoint.to_json()) == checkpoint
    with pytest.raises(HistoricalBackfillError, match="Checkpoint JSON"):
        BackfillCheckpoint.from_json(b'{"next_index": 0, "completed": []}')


def test_complete_manifest_binds_rights_archive_coverage_and_timestamp_assumptions() -> None:
    archive_payload = make_zip("BTCUSDT-1m-2026-11-01.csv", sample_csv())
    archive = VisionArchive(
        ARCHIVE_URL, hashlib.sha256(archive_payload).hexdigest(), archive_payload, NOW
    )
    bars = parse_kline_archive(
        archive, symbol="BTCUSDT", interval="1m", max_uncompressed_bytes=1000
    )
    backfill_request = request(
        start=bars[0].open_time,
        end=bars[1].open_time + timedelta(minutes=1),
    )
    url = build_vision_archive_urls(backfill_request)[0]
    digest = hashlib.sha256(archive_payload).hexdigest()
    checkpoint = BackfillCheckpoint(
        request_fingerprint=backfill_request_fingerprint(backfill_request),
        completed=(
            ArchiveCheckpoint(
                url=url,
                publisher_sha256=digest,
                downloaded_sha256=digest,
                byte_length=len(archive_payload),
                retrieved_at=NOW,
            ),
        ),
        next_index=1,
    )
    manifest = build_backfill_manifest(
        backfill_request,
        grant=personal_grant(),
        checkpoint=checkpoint,
        bars=bars,
        generated_at=NOW,
        feed_delay=timedelta(milliseconds=250),
    )
    assert isinstance(manifest, BackfillManifest)
    assert manifest.status == "COMPLETE"
    assert manifest.can_ingest
    assert manifest.coverage.expected_count == 2
    assert manifest.assumed_feed_delay_ms == 250
    assert manifest.terms_url == TERMS_URL
    assert manifest.license_id == VISION_DATASET_LICENSE
    assert len(manifest.dataset_sha256) == 64
    assert len(manifest.manifest_sha256) == 64
    assert b'"status":"COMPLETE"' in manifest.to_json()
    assert manifest.manifest_sha256.encode() in manifest.to_json()

    incomplete = build_backfill_manifest(
        backfill_request,
        grant=personal_grant(),
        checkpoint=BackfillCheckpoint(
            request_fingerprint=backfill_request_fingerprint(backfill_request)
        ),
        bars=bars[:1],
        generated_at=NOW,
    )
    assert incomplete.status == "IN_PROGRESS"
    assert not incomplete.can_ingest


@pytest.mark.asyncio
async def test_downloader_requires_explicit_rights_checksum_and_resumable_checkpoint() -> None:
    archive_payload = make_zip("BTCUSDT-1m-2026-11-01.csv", sample_csv())
    archive_url = "https://data.binance.vision/data/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2026-11-01.zip"
    digest = hashlib.sha256(archive_payload).hexdigest()
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        if request.url.path.endswith(".CHECKSUM"):
            return streamed_response(f"{digest}  {request.url.path.split('/')[-1][:-9]}\n".encode())
        return streamed_response(archive_payload)

    transport = httpx2.MockTransport(handler)
    downloader = BinanceVisionArchiveDownloader(
        transport=transport,
        clock=lambda: NOW,
        max_archive_bytes=1_000_000,
    )
    with pytest.raises(DatasetRightsError):
        await downloader.download(
            request(start=datetime(2026, 11, 1, tzinfo=UTC), end=datetime(2026, 11, 2, tzinfo=UTC)),
            grant=None,
            checkpoint=BackfillCheckpoint(),
        )
    with pytest.raises(HistoricalBackfillError, match="thời điểm hiện tại"):
        await downloader.download(
            request(now=NOW + timedelta(days=1)),
            grant=personal_grant(),
            checkpoint=BackfillCheckpoint(),
        )

    downloaded = await downloader.download(
        request(start=datetime(2026, 11, 1, tzinfo=UTC), end=datetime(2026, 11, 2, tzinfo=UTC)),
        grant=personal_grant(),
        checkpoint=BackfillCheckpoint(),
    )
    assert downloaded.checkpoint.completed_urls == (archive_url,)
    assert downloaded.archives[0].checksum == digest
    assert downloaded.archives[0].publisher_checksum == digest
    assert calls == [
        f"{archive_url}.CHECKSUM",
        archive_url,
    ]


@pytest.mark.asyncio
async def test_downloader_resumes_in_bounded_batches_without_redownloading_completed_archives() -> (
    None
):
    archive_payload = make_zip("BTCUSDT-1m-2026-11-01.csv", sample_csv())
    digest = hashlib.sha256(archive_payload).hexdigest()
    calls: list[str] = []

    def handler(http_request: httpx2.Request) -> httpx2.Response:
        calls.append(str(http_request.url))
        if http_request.url.path.endswith(".CHECKSUM"):
            return streamed_response(f"{digest}  archive.zip\n".encode())
        return streamed_response(archive_payload)

    async with BinanceVisionArchiveDownloader(
        transport=httpx2.MockTransport(handler),
        clock=lambda: NOW,
        max_archive_bytes=1_000_000,
        max_archives_per_call=1,
    ) as downloader:
        request_two_days = request(
            start=datetime(2026, 11, 1, tzinfo=UTC),
            end=datetime(2026, 11, 3, tzinfo=UTC),
        )
        first = await downloader.download(
            request_two_days,
            grant=personal_grant(),
            checkpoint=BackfillCheckpoint(),
        )
        assert not first.is_complete
        second = await downloader.download(
            request_two_days,
            grant=personal_grant(),
            checkpoint=BackfillCheckpoint.from_json(first.checkpoint.to_json()),
        )
    assert second.is_complete
    assert first.checkpoint.next_index == 1
    assert second.checkpoint.next_index == 2
    assert len(second.checkpoint.completed) == 2
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_downloader_stops_archive_exceeding_configured_download_size() -> None:
    payload = b"x" * 512
    digest = hashlib.sha256(payload).hexdigest()

    def handler(http_request: httpx2.Request) -> httpx2.Response:
        if http_request.url.path.endswith(".CHECKSUM"):
            return streamed_response(f"{digest} archive.zip\n".encode())
        return streamed_response(payload)

    async with BinanceVisionArchiveDownloader(
        transport=httpx2.MockTransport(handler),
        clock=lambda: NOW,
        max_archive_bytes=100,
    ) as downloader:
        with pytest.raises(HistoricalBackfillError, match="giới hạn"):
            await downloader.download(
                request(
                    start=datetime(2026, 11, 1, tzinfo=UTC), end=datetime(2026, 11, 2, tzinfo=UTC)
                ),
                grant=personal_grant(),
                checkpoint=BackfillCheckpoint(),
            )


@pytest.mark.asyncio
async def test_downloader_rejects_untrusted_redirect_and_checksum_mismatch() -> None:
    request_one_day = request(
        start=datetime(2026, 11, 1, tzinfo=UTC), end=datetime(2026, 11, 2, tzinfo=UTC)
    )

    def redirect(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"Location": "https://evil.example/archive.zip"})

    downloader = BinanceVisionArchiveDownloader(
        transport=httpx2.MockTransport(redirect),
        clock=lambda: NOW,
        max_archive_bytes=1_000_000,
    )
    with pytest.raises(HistoricalBackfillError, match="redirect"):
        await downloader.download(
            request_one_day, grant=personal_grant(), checkpoint=BackfillCheckpoint()
        )

    def mismatch(req: httpx2.Request) -> httpx2.Response:
        body = (
            b"0" * 64 + b"  BTCUSDT-1m-2026-11-01.zip\n"
            if req.url.path.endswith(".CHECKSUM")
            else b"wrong archive"
        )
        return streamed_response(body)

    downloader = BinanceVisionArchiveDownloader(
        transport=httpx2.MockTransport(mismatch),
        clock=lambda: NOW,
        max_archive_bytes=1_000_000,
    )
    with pytest.raises(HistoricalBackfillError, match="không khớp"):
        await downloader.download(
            request_one_day, grant=personal_grant(), checkpoint=BackfillCheckpoint()
        )


@pytest.mark.asyncio
async def test_importer_requires_verified_coverage_and_records_simulated_close_clock() -> None:
    archive_payload = make_zip("BTCUSDT-1m-2026-11-01.csv", sample_csv())
    archive = VisionArchive(
        "https://data.binance.vision/data/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2026-11-01.zip",
        hashlib.sha256(archive_payload).hexdigest(),
        archive_payload,
        NOW,
        publisher_checksum=hashlib.sha256(archive_payload).hexdigest(),
    )
    bars = parse_kline_archive(
        archive, symbol="BTCUSDT", interval="1m", max_uncompressed_bytes=1000
    )
    coverage = inspect_kline_coverage(
        bars[:1],
        start=bars[0].open_time,
        end=bars[0].open_time + timedelta(minutes=1),
        interval="1m",
    )

    class CapturingRepository:
        def __init__(self) -> None:
            self.raw: list[dict[str, object]] = []
            self.bars: list[dict[str, object]] = []

        async def append_raw_reference(self, **kwargs: object) -> MarketAppendResult:
            self.raw.append(kwargs)
            return MarketAppendResult(IngestionDisposition.ACCEPT, UUID(int=1))

        async def append_bar(self, **kwargs: object) -> MarketAppendResult:
            self.bars.append(kwargs)
            return MarketAppendResult(IngestionDisposition.ACCEPT, UUID(int=len(self.bars) + 1))

    repository = CapturingRepository()
    result = await append_archive_to_market_repository(
        repository,
        workspace_id=UUID(int=2),
        instrument_id=UUID(int=3),
        archive=archive,
        bars=bars[:1],
        raw_object_key="market/BTCUSDT/2026-11-01.zip",
        grant=personal_grant(),
        request_now=NOW,
        coverage=coverage,
        feed_delay=timedelta(seconds=250),
    )
    assert len(result) == 1
    assert repository.raw[0]["checksum"] == archive.checksum
    stored_bar = repository.bars[0]["bar"]
    assert stored_bar.received_at == bars[0].close_time
    assert stored_bar.available_at == bars[0].close_time + timedelta(seconds=250)
    assert stored_bar.source == "BINANCE_VISION"

    empty_repository = CapturingRepository()
    incomplete = inspect_kline_coverage(
        bars[:1],
        start=bars[0].open_time,
        end=bars[0].open_time + timedelta(minutes=2),
        interval="1m",
    )
    with pytest.raises(HistoricalBackfillError, match="gap"):
        await append_archive_to_market_repository(
            empty_repository,
            workspace_id=UUID(int=2),
            instrument_id=UUID(int=3),
            archive=archive,
            bars=bars[:1],
            raw_object_key="market/BTCUSDT/2026-11-01.zip",
            grant=personal_grant(),
            request_now=NOW,
            coverage=incomplete,
        )
    assert not empty_repository.raw
    assert not empty_repository.bars
