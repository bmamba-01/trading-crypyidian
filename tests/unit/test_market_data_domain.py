from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain.market.models import Instrument, MarketSnapshot, OHLCVBar
from trading_bot.domain.market.validation import bars_available_as_of, inspect_bar_series


def make_bar(
    *,
    opened_at: datetime | None = None,
    available_at: datetime | None = None,
    close_price: str = "101",
) -> OHLCVBar:
    start = opened_at or datetime(2026, 1, 1, tzinfo=UTC)
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe="1m",
        open_time=start,
        close_time=start + timedelta(minutes=1),
        event_at=start + timedelta(minutes=1),
        received_at=(available_at or start + timedelta(minutes=1)),
        available_at=(available_at or start + timedelta(minutes=1)),
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal(close_price),
        volume=Decimal("2.5"),
        is_closed=True,
    )


def test_instrument_requires_positive_exchange_steps() -> None:
    instrument = Instrument(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.10"),
        quantity_step=Decimal("0.001"),
        min_notional=Decimal("5"),
    )

    assert instrument.tick_size == Decimal("0.10")
    with pytest.raises(ValueError, match="tick_size"):
        Instrument(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            tick_size=Decimal("0"),
            quantity_step=Decimal("0.001"),
            min_notional=Decimal("5"),
        )


def test_ohlcv_bar_requires_timezone_aware_timestamps_and_valid_prices() -> None:
    with pytest.raises(ValueError, match="múi giờ"):
        make_bar(opened_at=datetime.fromisoformat("2026-01-01"))

    with pytest.raises(ValueError, match="OHLC"):
        OHLCVBar(
            symbol="BTCUSDT",
            timeframe="1m",
            open_time=datetime(2026, 1, 1, tzinfo=UTC),
            close_time=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
            event_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
            received_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
            available_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
            open=Decimal("100"),
            high=Decimal("99"),
            low=Decimal("98"),
            close=Decimal("100"),
            volume=Decimal("1"),
            is_closed=True,
        )


def test_ohlcv_bar_requires_event_time_before_receipt() -> None:
    bar = make_bar()

    with pytest.raises(ValueError, match="event_at.*received_at"):
        replace(bar, received_at=bar.event_at - timedelta(seconds=1))


def test_ohlcv_bar_requires_receipt_before_availability() -> None:
    bar = make_bar()

    with pytest.raises(ValueError, match="received_at.*available_at"):
        replace(bar, available_at=bar.received_at - timedelta(seconds=1))


def test_market_snapshot_cannot_include_event_after_as_of() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    event_after_decision = make_bar(
        opened_at=start + timedelta(minutes=2),
        available_at=start + timedelta(minutes=3),
    )

    with pytest.raises(ValueError, match="available"):
        MarketSnapshot(as_of=start + timedelta(minutes=2, seconds=30), bars=(event_after_decision,))


def test_market_snapshot_excludes_data_not_yet_available_at_decision_time() -> None:
    as_of = datetime(2026, 1, 1, 0, 2, tzinfo=UTC)
    visible = make_bar()
    future = make_bar(
        opened_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        available_at=datetime(2026, 1, 1, 0, 3, tzinfo=UTC),
    )

    snapshot = MarketSnapshot(as_of=as_of, bars=(visible,))
    assert snapshot.bars == (visible,)
    with pytest.raises(ValueError, match="available"):
        MarketSnapshot(as_of=as_of, bars=(visible, future))


def test_bars_available_as_of_filters_late_data_and_keeps_event_order() -> None:
    as_of = datetime(2026, 1, 1, 0, 3, tzinfo=UTC)
    early = make_bar(opened_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC))
    late = make_bar(
        opened_at=datetime(2026, 1, 1, 0, 2, tzinfo=UTC),
        available_at=datetime(2026, 1, 1, 0, 4, tzinfo=UTC),
    )

    assert bars_available_as_of((late, early), as_of) == (early,)


def test_inspect_bar_series_reports_duplicates_gaps_and_unavailable_bars() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first = make_bar(opened_at=start)
    duplicate = make_bar(opened_at=start, close_price="100.5")
    third = make_bar(opened_at=start + timedelta(minutes=2))
    future = make_bar(
        opened_at=start + timedelta(minutes=3),
        available_at=start + timedelta(minutes=5),
    )

    quality = inspect_bar_series(
        (first, duplicate, third, future),
        as_of=start + timedelta(minutes=4),
        expected_interval=timedelta(minutes=1),
        max_event_age=timedelta(minutes=2),
    )

    assert quality.duplicate_keys == (("BTCUSDT", "1m", start),)
    assert quality.gap_after == (("BTCUSDT", "1m", start),)
    assert quality.future_available_count == 1
    assert quality.stale is False
    assert quality.is_usable is False


def test_inspect_bar_series_flags_stale_last_event() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    quality = inspect_bar_series(
        (make_bar(opened_at=start),),
        as_of=start + timedelta(minutes=10),
        expected_interval=timedelta(minutes=1),
        max_event_age=timedelta(minutes=2),
    )

    assert quality.stale is True


def test_inspect_bar_series_flags_any_stale_symbol_in_a_multi_symbol_batch() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    stale_bar = make_bar(opened_at=start)
    recent_bar = replace(
        make_bar(opened_at=start + timedelta(minutes=8)),
        symbol="ETHUSDT",
    )

    quality = inspect_bar_series(
        (stale_bar, recent_bar),
        as_of=start + timedelta(minutes=10),
        expected_interval=timedelta(minutes=1),
        max_event_age=timedelta(minutes=2),
    )

    assert quality.stale is True


def test_inspect_bar_series_marks_open_candles_unusable() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    closed = make_bar(opened_at=start)
    open_bar = OHLCVBar(
        symbol="BTCUSDT",
        timeframe="1m",
        open_time=start + timedelta(minutes=1),
        close_time=start + timedelta(minutes=2),
        event_at=start + timedelta(minutes=1, seconds=30),
        received_at=start + timedelta(minutes=1, seconds=30),
        available_at=start + timedelta(minutes=1, seconds=30),
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("1"),
        is_closed=False,
    )

    quality = inspect_bar_series(
        (closed, open_bar),
        as_of=start + timedelta(minutes=2),
        expected_interval=timedelta(minutes=1),
        max_event_age=timedelta(minutes=2),
    )

    assert quality.unclosed_count == 1
    assert quality.is_usable is False


def test_inspect_bar_series_reports_missing_range_and_boundary_bars() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = (
        make_bar(opened_at=start + timedelta(minutes=1)),
        make_bar(opened_at=start + timedelta(minutes=2)),
    )

    quality = inspect_bar_series(
        bars,
        as_of=start + timedelta(minutes=5),
        expected_interval=timedelta(minutes=1),
        max_event_age=timedelta(minutes=10),
        expected_range_start=start,
        expected_range_end=start + timedelta(minutes=4),
        backfill_complete=True,
    )

    assert quality.missing_expected_open_times == (
        ("BTCUSDT", "1m", start),
        ("BTCUSDT", "1m", start + timedelta(minutes=3)),
    )
    assert quality.missing_boundary_open_times == quality.missing_expected_open_times
    assert quality.is_usable is False


def test_requested_range_stays_unusable_until_backfill_is_confirmed() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = tuple(make_bar(opened_at=start + timedelta(minutes=i)) for i in range(4))
    params = {
        "as_of": start + timedelta(minutes=5),
        "expected_interval": timedelta(minutes=1),
        "max_event_age": timedelta(minutes=10),
        "expected_range_start": start,
        "expected_range_end": start + timedelta(minutes=4),
    }

    pending = inspect_bar_series(bars, **params, backfill_complete=False)
    complete = inspect_bar_series(bars, **params, backfill_complete=True)

    assert pending.missing_expected_open_times == ()
    assert pending.backfill_complete is False
    assert pending.is_usable is False
    assert complete.backfill_complete is True
    assert complete.is_usable is True


@pytest.mark.parametrize(
    "range_overrides",
    [
        {"expected_range_start": datetime.fromisoformat("2026-01-01")},
        {"expected_range_start": datetime(2026, 1, 1, tzinfo=UTC)},
        {
            "expected_range_start": datetime(2026, 1, 1, tzinfo=UTC),
            "expected_range_end": datetime(2026, 1, 1, 0, 4, 30, tzinfo=UTC),
        },
        {
            "expected_range_start": datetime(2026, 1, 1, tzinfo=UTC),
            "expected_range_end": datetime(2026, 1, 1, 0, 1, 30, tzinfo=UTC),
        },
    ],
)
def test_inspect_bar_series_rejects_invalid_or_unbounded_ranges(
    range_overrides: dict[str, datetime],
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="múi giờ|Cần truyền|expected range"):
        inspect_bar_series(
            (make_bar(opened_at=start),),
            as_of=start + timedelta(minutes=2),
            expected_interval=timedelta(minutes=1),
            max_event_age=timedelta(minutes=2),
            **range_overrides,
        )


def test_inspect_bar_series_rejects_ranges_over_the_validation_limit() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="100000"):
        inspect_bar_series(
            (make_bar(opened_at=start),),
            as_of=start + timedelta(days=2),
            expected_interval=timedelta(seconds=1),
            max_event_age=timedelta(days=2),
            expected_range_start=start,
            expected_range_end=start + timedelta(days=2),
        )


def test_inspect_bar_series_reports_empty_range_when_stream_context_is_supplied() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    quality = inspect_bar_series(
        (),
        as_of=start + timedelta(minutes=3),
        expected_interval=timedelta(minutes=1),
        max_event_age=timedelta(minutes=5),
        expected_range_start=start,
        expected_range_end=start + timedelta(minutes=2),
        backfill_complete=True,
        expected_streams=(("BTCUSDT", "1m"),),
    )

    assert quality.missing_expected_open_times == (
        ("BTCUSDT", "1m", start),
        ("BTCUSDT", "1m", start + timedelta(minutes=1)),
    )
