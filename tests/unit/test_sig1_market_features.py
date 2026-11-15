"""Unit test SIG-1: cấu trúc, trendline, phiên/thanh khoản, volume profile, MACD."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading_bot.domain.market.features import ema, macd
from trading_bot.domain.market.sessions import (
    DEFAULT_SESSIONS,
    liquidity_levels,
    session_ranges,
    session_windows,
)
from trading_bot.domain.market.structure import (
    auto_trendlines,
    confirmed_swings,
    market_structure,
    structure_state,
)
from trading_bot.domain.market.volume_profile import (
    bar_delta,
    cumulative_delta,
    volume_profile,
)

# Chuỗi zigzag tay: đỉnh tại 2 (12), đáy tại 5 (8), đỉnh tại 8 (14), đáy tại 11 (10), ...
HIGHS = [10, 11, 12, 11, 10, 9, 10, 12, 14, 13, 12, 11, 12, 13, 15, 16, 17]
LOWS = [9, 10, 11, 10, 9, 8, 9, 11, 13, 12, 11, 10, 11, 12, 14, 15, 16]
CLOSES = [9.5, 10.5, 11.5, 10.5, 9.5, 8.5, 9.5, 11.5, 13.5, 12.5, 11.5, 10.5, 11.5, 12.5,
          14.5, 15.5, 16.5]  # fmt: skip


def test_swings_are_confirmed_only_after_right_bars_and_labelled() -> None:
    swings = confirmed_swings(HIGHS, LOWS, left_bars=2, right_bars=2)
    assert [(s.kind, s.index, s.confirmed_index, s.label) for s in swings] == [
        ("high", 2, 4, None),
        ("low", 5, 7, None),
        ("high", 8, 10, "HH"),
        ("low", 11, 13, "HL"),
    ]


def test_structure_bos_uses_close_and_only_known_swings() -> None:
    result = market_structure(HIGHS, LOWS, CLOSES, left_bars=2, right_bars=2)
    # Đỉnh 12 (xác nhận ở nến 4) bị phá khi close 13.5 > 12 ở nến 8 → BOS tăng.
    # Đỉnh 14 (xác nhận ở nến 10) bị phá ở nến 14 (close 14.5).
    assert [(b.kind, b.direction, b.level, b.break_index) for b in result.breaks] == [
        ("BOS", "bullish", 12.0, 8),
        ("BOS", "bullish", 14.0, 14),
    ]
    assert result.trend[7] is None
    assert result.trend[8] == "up"
    assert result.trend[-1] == "up"


def test_choch_is_first_break_against_trend() -> None:
    highs = [*HIGHS[:14], 13, 12, 11]
    lows = [*LOWS[:14], 9.5, 9, 8]
    closes = [*CLOSES[:14], 10.0, 9.2, 8.5]
    result = market_structure(highs, lows, closes, left_bars=2, right_bars=2)
    # Xu hướng tăng (BOS ở nến 8); đáy 10 xác nhận ở nến 13; close 9.2 < 10 ở nến 15 → CHoCH.
    assert result.breaks[-1].kind == "CHOCH"
    assert result.breaks[-1].direction == "bearish"
    assert result.breaks[-1].break_index == 15
    assert result.trend[-1] == "down"


def test_structure_streaming_equals_batch() -> None:
    full = market_structure(HIGHS, LOWS, CLOSES, left_bars=2, right_bars=2)
    for end in range(1, len(HIGHS) + 1):
        partial = market_structure(HIGHS[:end], LOWS[:end], CLOSES[:end], left_bars=2, right_bars=2)
        assert partial.trend == full.trend[:end]
        assert partial.breaks == tuple(b for b in full.breaks if b.break_index < end)
        assert partial.swings == tuple(s for s in full.swings if s.confirmed_index < end)


def test_structure_rejects_invalid_bar() -> None:
    with pytest.raises(ValueError, match="close phải nằm trong"):
        market_structure([1.0], [0.5], [2.0])


def test_trendline_from_two_confirmed_lows_and_invalidation() -> None:
    atr_values = [1.0] * len(HIGHS)
    result = auto_trendlines(HIGHS, LOWS, CLOSES, atr_values, left_bars=2, right_bars=2)
    support = [line for line in result.lines if line.side == "support"]
    assert len(support) == 1
    line = support[0]
    assert (line.p1_index, line.p2_index, line.confirmed_index) == (5, 11, 13)
    assert line.slope_per_bar == pytest.approx((10 - 8) / 6)
    assert line.invalidated_index is None
    assert result.active("support") is line

    # Giá đóng cửa sụp dưới line quá 0,5 ATR → line bị vô hiệu tại nến đó.
    closes = [*CLOSES[:14], 10.0, 15.5, 16.5]
    lows = [*LOWS[:14], 9.5, 15, 16]
    broken = auto_trendlines(HIGHS, lows, closes, atr_values, left_bars=2, right_bars=2)
    assert next(x for x in broken.lines if x.side == "support").invalidated_index == 14


def test_trendline_needs_atr_and_state_classification() -> None:
    result = auto_trendlines(HIGHS, LOWS, CLOSES, [None] * len(HIGHS), left_bars=2, right_bars=2)
    assert result.lines == ()
    assert structure_state(result) == "unknown"
    ok = auto_trendlines(HIGHS, LOWS, CLOSES, [1.0] * len(HIGHS), left_bars=2, right_bars=2)
    assert structure_state(ok) == "uptrend"


def test_session_windows_follow_dst_per_market() -> None:
    london = next(s for s in DEFAULT_SESSIONS if s.key == "london")
    new_york = next(s for s in DEFAULT_SESSIONS if s.key == "new_york")
    # 2026-03-09: New York đã sang giờ mùa hè (EDT, từ 08/03), London chưa (BST từ 29/03).
    day = datetime(2026, 3, 9, tzinfo=UTC)
    ((ny_open, ny_close),) = [
        w for w in session_windows(new_york, day, day + timedelta(days=1)) if w[0].day == 9
    ]
    ((ld_open, _),) = [
        w for w in session_windows(london, day, day + timedelta(days=1)) if w[0].day == 9
    ]
    assert ny_open == datetime(2026, 3, 9, 13, 30, tzinfo=UTC)
    assert ny_close == datetime(2026, 3, 9, 20, 0, tzinfo=UTC)
    assert ld_open == datetime(2026, 3, 9, 8, 0, tzinfo=UTC)
    # Tháng 7: London BST → mở 07:00 UTC.
    july = datetime(2026, 7, 6, tzinfo=UTC)
    ((summer_open, _),) = [
        w for w in session_windows(london, july, july + timedelta(days=1)) if w[0].day == 6
    ]
    assert summer_open == datetime(2026, 7, 6, 7, 0, tzinfo=UTC)


def _hourly(start: datetime, count: int) -> list[datetime]:
    return [start + timedelta(hours=i) for i in range(count)]


def test_session_range_and_sweep_rejected_then_expiry() -> None:
    # Ngày 2026-11-16 (thứ Hai): Tokyo 09:00–18:00 JST = 00:00–09:00 UTC.
    times = _hourly(datetime(2026, 11, 16, 0, tzinfo=UTC), 12)
    highs = [100.0] * 12
    lows = [90.0] * 12
    closes = [95.0] * 12
    highs[3] = 105.0  # đỉnh phiên Á
    lows[5] = 85.0  # đáy phiên Á
    highs[10] = 106.0  # nến 10:00 UTC quét đỉnh 105 rồi đóng 95 → rejected (sweep)
    ranges = [r for r in session_ranges(times, highs, lows, timedelta(hours=1)) if r.key == "asia"]
    assert len(ranges) == 1
    asia = ranges[0]
    assert (asia.high, asia.low, asia.bar_count, asia.complete) == (105.0, 85.0, 9, True)

    levels = {
        level.key: level
        for level in liquidity_levels(times, highs, lows, closes, timedelta(hours=1))
        if level.key.startswith("asia")
    }
    assert levels["asia_high"].state == "rejected"
    assert levels["asia_high"].is_sweep
    assert levels["asia_high"].event_index == 10
    assert levels["asia_low"].state == "armed"


def test_level_accepted_when_close_stays_outside() -> None:
    times = _hourly(datetime(2026, 11, 16, 0, tzinfo=UTC), 11)
    highs = [100.0] * 11
    lows = [90.0] * 11
    closes = [95.0] * 11
    highs[10] = 110.0
    closes[10] = 108.0
    asia_high = next(
        level
        for level in liquidity_levels(times, highs, lows, closes, timedelta(hours=1))
        if level.key == "asia_high"
    )
    assert asia_high.state == "accepted"


def test_incomplete_session_does_not_arm_levels() -> None:
    times = _hourly(datetime(2026, 11, 16, 0, tzinfo=UTC), 5)
    values = [1.0] * 5
    assert liquidity_levels(times, [2.0] * 5, values, [1.5] * 5, timedelta(hours=1)) == ()


def test_previous_day_levels_from_completed_utc_day() -> None:
    times = _hourly(datetime(2026, 11, 15, 0, tzinfo=UTC), 30)
    highs = [100.0] * 30
    lows = [90.0] * 30
    closes = [95.0] * 30
    highs[5] = 120.0
    levels = {
        level.key: level
        for level in liquidity_levels(times, highs, lows, closes, timedelta(hours=1))
    }
    assert levels["pdh"].price == 120.0
    assert levels["pdh"].armed_at == datetime(2026, 11, 16, tzinfo=UTC)
    assert levels["pdl"].price == 90.0


def test_bar_delta_and_cvd_never_invent_missing_taker() -> None:
    assert bar_delta(10.0, 7.0) == 4.0
    assert bar_delta(10.0, None) is None
    with pytest.raises(ValueError, match="taker buy"):
        bar_delta(10.0, 11.0)
    cvd = cumulative_delta([10, 10, 10, 10], [7, None, 6, 5], ["a", "a", "a", "b"])
    assert cvd == (4.0, None, None, 0.0)


def test_volume_profile_poc_value_area_and_split() -> None:
    highs = [10.0, 10.0, 12.0, 14.0]
    lows = [10.0, 10.0, 11.0, 13.0]
    volumes = [50.0, 50.0, 20.0, 10.0]
    profile = volume_profile(highs, lows, volumes, None, bins=4)
    assert profile is not None
    assert profile.total_volume == pytest.approx(130.0)
    assert profile.poc_price == pytest.approx(10.5)
    assert profile.value_area_low == pytest.approx(10.0)
    assert profile.value_area_high >= profile.poc_price
    assert all(item.buy is None for item in profile.bins)
    assert profile.imbalance_reference is None

    skewed = volume_profile(highs, lows, volumes, [40.0, 40.0, 10.0, 1.0], bins=4)
    assert skewed is not None
    assert skewed.bins[0].buy == pytest.approx(80.0)
    assert skewed.bins[0].sell == pytest.approx(20.0)
    assert skewed.bins[0].imbalance == pytest.approx(0.6)

    # Bin 0 cân bằng và khối lượng lớn; bin 3 chỉ có bán chủ động.
    # Eref = 10/130 ≈ 0,077 → bin 3 (E=1) ≥ 2·Eref; bin 0 (E=0, 100 ≥ 1,25× trung bình 43,3).
    split = volume_profile(highs, lows, volumes, [25.0, 25.0, 10.0, 0.0], bins=4)
    assert split is not None
    assert split.imbalance_reference == pytest.approx(10 / 130)
    assert split.bins[0].category == "high_activity_low_imbalance"
    assert split.bins[1].category == "normal"
    assert split.bins[3].category == "high_imbalance"
    assert split.bins[3].imbalance == pytest.approx(-1.0)
    assert volume_profile([1.0], [1.0], [0.0]) is None


def test_macd_matches_ema_definition() -> None:
    closes = [100 + (i % 7) * 1.5 + i * 0.2 for i in range(60)]
    line, signal, hist = macd(closes)
    fast, slow = ema(closes, 12), ema(closes, 26)
    assert line[24] is None
    assert line[25] == pytest.approx(fast[25] - slow[25])  # type: ignore[operator]
    assert signal[32] is None
    assert signal[33] is not None
    assert hist[40] == pytest.approx(line[40] - signal[40])  # type: ignore[operator]
    with pytest.raises(ValueError, match="fast phải nhỏ hơn slow"):
        macd(closes, fast=26, slow=12)
