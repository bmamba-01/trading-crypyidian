"""CK-1/CK-2: đồng hồ nến, phiên đang chạy, biên ngày và ma trận xu hướng đa khung."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading_bot.domain.market.overview import (
    active_sessions,
    candle_clock,
    day_range,
    mtf_consensus,
    timeframe_trend,
)


def test_candle_clock_aligns_to_utc_interval_boundaries() -> None:
    now = datetime(2026, 11, 15, 10, 7, 30, tzinfo=UTC)
    clock = candle_clock(now, 300)
    assert clock.open_at == datetime(2026, 11, 15, 10, 5, tzinfo=UTC)
    assert clock.close_at == datetime(2026, 11, 15, 10, 10, tzinfo=UTC)
    assert clock.seconds_left == 150

    # Nến 4h và 1d neo theo mốc UTC, kể cả khi qua ngày.
    late = datetime(2026, 11, 15, 23, 59, 59, tzinfo=UTC)
    assert candle_clock(late, 14_400).close_at == datetime(2026, 11, 16, tzinfo=UTC)
    assert candle_clock(late, 86_400).seconds_left == 1


def test_candle_clock_rejects_naive_time_and_unsupported_interval() -> None:
    with pytest.raises(ValueError, match="timezone"):
        candle_clock(datetime(2026, 11, 15, 10, 0), 300)  # noqa: DTZ001 - cố ý thiếu tz
    with pytest.raises(ValueError, match="khung"):
        candle_clock(datetime(2026, 11, 15, tzinfo=UTC), 7 * 86_400)


def test_active_sessions_follow_local_time_and_daylight_saving() -> None:
    # 15/11 (giờ mùa đông): London 08:00–17:00 GMT, New York 09:30–16:00 EST = 14:30–21:00 UTC.
    overlap = datetime(2026, 11, 15, 15, 0, tzinfo=UTC)
    assert [item.key for item in active_sessions(overlap)] == ["london", "new_york"]
    # 15/07 (giờ mùa hè): New York mở 13:30 UTC.
    summer = datetime(2026, 7, 15, 13, 45, tzinfo=UTC)
    assert "new_york" in [item.key for item in active_sessions(summer)]
    # Tokyo 09:00–18:00 JST = 00:00–09:00 UTC; 22:00 UTC không phiên nào.
    assert active_sessions(datetime(2026, 11, 15, 22, 0, tzinfo=UTC)) == ()


def _hourly(start: datetime, count: int) -> list[datetime]:
    return [start + timedelta(hours=index) for index in range(count)]


def test_day_range_uses_utc_day_and_flags_incomplete_previous_day() -> None:
    start = datetime(2026, 11, 14, 0, 0, tzinfo=UTC)
    times = _hourly(start, 34)  # 24 nến ngày 14 + 10 nến ngày 15
    highs = [100.0 + index for index in range(34)]
    lows = [90.0 + index for index in range(34)]
    now = datetime(2026, 11, 15, 10, 20, tzinfo=UTC)

    result = day_range(times, highs, lows, timedelta(hours=1), now, live_price=200.0)
    assert result.day_start == datetime(2026, 11, 15, tzinfo=UTC)
    assert result.high == 200.0  # giá trực tiếp vượt đỉnh nến đã đóng
    assert result.low == 114.0
    assert result.previous_high == 123.0
    assert result.previous_low == 90.0
    assert result.previous_complete is True

    partial = day_range(times[5:], highs[5:], lows[5:], timedelta(hours=1), now)
    assert partial.previous_complete is False
    assert partial.high == 133.0  # không có giá trực tiếp thì chỉ dùng nến đã đóng


def _zigzag(legs: list[tuple[float, float]], step: int = 4) -> tuple[list[float], ...]:
    closes: list[float] = []
    for start, end in legs:
        for index in range(step):
            closes.append(start + (end - start) * index / step)
    closes.append(legs[-1][1])
    highs = [value + 0.5 for value in closes]
    lows = [value - 0.5 for value in closes]
    return highs, lows, closes


def _times(count: int, minutes: int) -> list[datetime]:
    start = datetime(2026, 11, 10, tzinfo=UTC)
    return [start + timedelta(minutes=minutes * index) for index in range(count)]


def test_timeframe_trend_reports_direction_invalidation_and_range_position() -> None:
    # Đáy cao dần, đỉnh cao dần → xu hướng tăng; mức vô hiệu là đáy swing gần nhất.
    highs, lows, closes = _zigzag(
        [(100, 110), (110, 104), (104, 116), (116, 109), (109, 122), (122, 115), (115, 120)]
    )
    cell = timeframe_trend("1h", _times(len(closes), 60), highs, lows, closes)
    assert cell.trend == "up"
    assert cell.invalidation_price == pytest.approx(114.5)
    assert cell.range_low == pytest.approx(114.5)
    assert cell.range_high == pytest.approx(122.5)
    assert cell.position_pct == pytest.approx((120 - 114.5) / (122.5 - 114.5) * 100)
    assert cell.zone == "premium"
    assert cell.last_break is not None
    assert cell.last_break.direction == "bullish"

    down = timeframe_trend(
        "15m",
        _times(len(closes), 15),
        [200 - value for value in lows],
        [200 - value for value in highs],
        [200 - value for value in closes],
    )
    assert down.trend == "down"
    assert down.invalidation_price == pytest.approx(200 - 114.5)
    assert down.zone == "discount"


def test_timeframe_trend_without_enough_bars_is_unknown_not_guessed() -> None:
    cell = timeframe_trend("5m", _times(5, 5), [2.0] * 5, [1.0] * 5, [1.5] * 5)
    assert cell.trend == "unknown"
    assert cell.invalidation_price is None
    assert cell.position_pct is None


def test_mtf_consensus_counts_alignment_with_primary_timeframe() -> None:
    highs, lows, closes = _zigzag(
        [(100, 110), (110, 104), (104, 116), (116, 109), (109, 122), (122, 115), (115, 120)]
    )
    up = timeframe_trend("4h", _times(len(closes), 240), highs, lows, closes)
    mirrored = timeframe_trend(
        "5m",
        _times(len(closes), 5),
        [200 - value for value in lows],
        [200 - value for value in highs],
        [200 - value for value in closes],
    )
    unknown = timeframe_trend("15m", _times(5, 15), [2.0] * 5, [1.0] * 5, [1.5] * 5)

    consensus = mtf_consensus([up, unknown, mirrored], primary="4h")
    assert consensus.direction == "up"
    assert (consensus.aligned, consensus.known) == (1, 2)
    assert consensus.label_vi == "Khung chính 4H TĂNG · 1/2 khung có xu hướng cùng chiều"

    empty = mtf_consensus([unknown], primary="4h")
    assert empty.direction == "unknown"
    assert empty.label_vi == "Chưa đủ dữ liệu để xác định xu hướng đa khung"
