from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from trading_bot.domain.market.ingestion import (
    ClockObservation,
    IngestionCursor,
    IngestionDisposition,
    IngestionGap,
    InstrumentObservation,
    MarketRecordVersion,
    MarketStream,
    assess_ingestion_range,
    classify_market_ingestion,
)
from trading_bot.domain.market.models import Instrument, OHLCVBar


def make_instrument() -> Instrument:
    return Instrument(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        min_notional=Decimal("5"),
    )


def make_bar(open_time: datetime, *, available_at: datetime | None = None) -> OHLCVBar:
    received = available_at or open_time + timedelta(minutes=1)
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe="1m",
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        event_at=open_time + timedelta(minutes=1),
        received_at=received,
        available_at=received,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("1"),
        is_closed=True,
    )


def make_stream() -> MarketStream:
    return MarketStream(source="binance-usdm-public", symbol="BTCUSDT", timeframe="1m")


def make_version(
    *,
    revision: int = 1,
    checksum: str = "a" * 64,
    idempotency_key: str = "source:cursor:1",
    record_id=None,
    supersedes_record_id=None,
    received_at: datetime | None = None,
    available_at: datetime | None = None,
) -> MarketRecordVersion:
    event_at = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
    received = received_at or event_at + timedelta(seconds=1)
    available = available_at or received + timedelta(seconds=1)
    return MarketRecordVersion(
        record_id=record_id or uuid4(),
        stream=make_stream(),
        logical_key="BTCUSDT:1m:2026-01-01T00:00:00Z",
        idempotency_key=idempotency_key,
        revision=revision,
        content_checksum=checksum,
        event_at=event_at,
        received_at=received,
        available_at=available,
        supersedes_record_id=supersedes_record_id,
    )


def test_instrument_observation_rejects_stale_or_future_metadata() -> None:
    as_of = datetime(2026, 1, 1, 1, tzinfo=UTC)
    observation = InstrumentObservation(
        instrument=make_instrument(), source="binance", observed_at=as_of - timedelta(minutes=2)
    )

    assert observation.is_fresh(as_of=as_of, max_age=timedelta(minutes=2)) is True
    assert observation.is_fresh(as_of=as_of, max_age=timedelta(minutes=1)) is False
    future = replace(observation, observed_at=as_of + timedelta(seconds=1))
    assert future.is_fresh(as_of=as_of, max_age=timedelta(minutes=2)) is False


def test_clock_observation_detects_source_clock_outside_tolerance() -> None:
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    within = ClockObservation(
        source="binance",
        source_time=observed_at + timedelta(milliseconds=200),
        observed_at=observed_at,
        tolerance=timedelta(milliseconds=500),
    )
    skewed = replace(within, source_time=observed_at + timedelta(seconds=2))

    assert within.is_within_tolerance is True
    assert skewed.is_within_tolerance is False


def test_cursor_and_range_require_complete_backfill_and_coverage_watermark() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(minutes=3)
    as_of = end + timedelta(minutes=2)
    bars = tuple(make_bar(start + timedelta(minutes=i)) for i in range(3))
    cursor = IngestionCursor(
        stream=make_stream(),
        token="opaque-cursor-3",
        covered_until=end,
        updated_at=as_of,
    )

    incomplete = assess_ingestion_range(
        stream=make_stream(),
        bars=bars,
        as_of=as_of,
        range_start=start,
        range_end=end,
        expected_interval=timedelta(minutes=1),
        max_event_age=timedelta(minutes=5),
        cursor=cursor,
        backfill_complete=False,
    )
    complete = assess_ingestion_range(
        stream=make_stream(),
        bars=bars,
        as_of=as_of,
        range_start=start,
        range_end=end,
        expected_interval=timedelta(minutes=1),
        max_event_age=timedelta(minutes=5),
        cursor=cursor,
        backfill_complete=True,
    )
    short_cursor = replace(cursor, covered_until=end - timedelta(minutes=1))
    cursor_incomplete = assess_ingestion_range(
        stream=make_stream(),
        bars=bars,
        as_of=as_of,
        range_start=start,
        range_end=end,
        expected_interval=timedelta(minutes=1),
        max_event_age=timedelta(minutes=5),
        cursor=short_cursor,
        backfill_complete=True,
    )

    assert incomplete.is_complete is False
    assert complete.is_complete is True
    assert cursor_incomplete.is_complete is False


def test_empty_requested_range_reports_expected_boundaries_with_stream_context() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(minutes=2)
    as_of = end + timedelta(minutes=1)

    status = assess_ingestion_range(
        stream=make_stream(),
        bars=(),
        as_of=as_of,
        range_start=start,
        range_end=end,
        expected_interval=timedelta(minutes=1),
        max_event_age=timedelta(minutes=5),
        cursor=None,
        backfill_complete=True,
    )

    assert status.quality.missing_expected_open_times == (
        ("BTCUSDT", "1m", start),
        ("BTCUSDT", "1m", start + timedelta(minutes=1)),
    )
    assert len(status.quality.missing_boundary_open_times) == 2
    assert status.is_complete is False


def test_open_gap_overlapping_requested_range_blocks_completion() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(minutes=3)
    as_of = end + timedelta(minutes=2)
    cursor = IngestionCursor(
        stream=make_stream(), token="cursor", covered_until=end, updated_at=as_of
    )
    gap = IngestionGap(
        stream=make_stream(),
        gap_start=start + timedelta(minutes=1),
        gap_end=start + timedelta(minutes=2),
        detected_at=as_of,
    )

    status = assess_ingestion_range(
        stream=make_stream(),
        bars=tuple(make_bar(start + timedelta(minutes=i)) for i in range(3)),
        as_of=as_of,
        range_start=start,
        range_end=end,
        expected_interval=timedelta(minutes=1),
        max_event_age=timedelta(minutes=5),
        cursor=cursor,
        gaps=(gap,),
        backfill_complete=True,
    )

    assert status.is_complete is False


def test_repeated_idempotency_key_with_same_checksum_is_duplicate() -> None:
    existing = make_version()
    retry = replace(existing, record_id=uuid4())

    assert classify_market_ingestion((existing,), retry) is IngestionDisposition.DUPLICATE


def test_repeated_idempotency_key_with_changed_content_is_conflict() -> None:
    existing = make_version()
    changed = replace(existing, record_id=uuid4(), content_checksum="b" * 64)

    assert classify_market_ingestion((existing,), changed) is IngestionDisposition.CONFLICT


def test_idempotency_replay_cannot_relabel_existing_payload_as_a_new_revision() -> None:
    existing = make_version()
    relabeled = make_version(
        revision=2,
        checksum=existing.content_checksum,
        idempotency_key=existing.idempotency_key,
        supersedes_record_id=existing.record_id,
        received_at=existing.available_at + timedelta(seconds=1),
        available_at=existing.available_at + timedelta(seconds=2),
    )

    assert classify_market_ingestion((existing,), relabeled) is IngestionDisposition.CONFLICT


def test_revised_source_record_appends_lineage_without_overwriting() -> None:
    previous = make_version()
    correction = make_version(
        revision=2,
        checksum="b" * 64,
        idempotency_key="source:cursor:2",
        supersedes_record_id=previous.record_id,
        received_at=previous.available_at + timedelta(seconds=1),
        available_at=previous.available_at + timedelta(seconds=2),
    )

    assert classify_market_ingestion((previous,), correction) is IngestionDisposition.REVISION
    assert previous.content_checksum == "a" * 64


def test_revision_with_missing_parent_or_nonsequential_number_is_conflict() -> None:
    previous = make_version()
    invalid = make_version(
        revision=3,
        checksum="b" * 64,
        idempotency_key="source:cursor:3",
        supersedes_record_id=uuid4(),
        received_at=previous.available_at + timedelta(seconds=1),
        available_at=previous.available_at + timedelta(seconds=2),
    )

    assert classify_market_ingestion((previous,), invalid) is IngestionDisposition.CONFLICT
