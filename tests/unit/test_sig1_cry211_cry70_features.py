"""Unit test cho CRY-211 (SIG-1 còn lại) và CRY-70 (IND-2 Anchored VWAP)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from trading_bot.application.strategy_market_requirements import feature_settings_for
from trading_bot.domain.market.anchored_vwap import (
    ANCHORED_VWAP_FORMULA_VERSION,
    AnchorRule,
    ConfirmedAnchorEvent,
    anchored_vwap_confirmed_events,
    anchored_vwap_from_swings,
    anchored_vwap_month_utc,
    anchored_vwap_week_utc,
)
from trading_bot.domain.market.feature_snapshot import (
    EXTRA_FEATURE_REGISTRY,
    FeatureCalculator,
    FeatureSettings,
    feature_schema_hash,
)
from trading_bot.domain.market.features import obv
from trading_bot.domain.market.models import MarketSnapshot, OHLCVBar
from trading_bot.domain.market.sessions import (
    LIQUIDITY_FORMULA_VERSION,
    SESSION_STATS_FORMULA_VERSION,
    LiquidityLevel,
    SessionRange,
    liquidity_base_rates,
    liquidity_levels,
    previous_week_high_low,
    session_range_percentiles,
)
from trading_bot.domain.market.structure import (
    ADAPTIVE_PIVOT_FORMULA_VERSION,
    adaptive_confirmed_swings,
    adaptive_market_structure,
    adaptive_pivot_lookback,
)

WORKSPACE_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def _hourly(start: datetime, count: int) -> list[datetime]:
    return [start + timedelta(hours=i) for i in range(count)]


# ---------------------------------------------------------------------------
# 1. PWH / PWL (Previous Week High / Low)
# ---------------------------------------------------------------------------


def test_pwh_pwl_uses_only_completed_utc_week_never_open_week() -> None:
    # Bắt đầu đúng thứ Hai 2026-10-26 00:00 UTC, chạy 9 ngày (216 nến 1h).
    start = datetime(2026, 10, 26, 0, tzinfo=UTC)
    times = _hourly(start, 216)
    highs = [100.0] * 216
    lows = [90.0] * 216
    closes = [95.0] * 216

    # Cực trị tuần 1 (chỉ số 0..167)
    highs[50] = 125.0
    lows[90] = 80.0
    # Cực trị tuần 2 đang mở (chỉ số 168..215) — tuyệt đối không được rò vào PWH/PWL.
    highs[180] = 150.0
    lows[190] = 60.0

    pwh, pwl = previous_week_high_low(times, highs, lows, timedelta(hours=1))
    # Trước khi nến cuối tuần 1 đóng (0..166): chưa hoàn tất tuần -> None.
    assert all(value is None for value in pwh[:167])
    assert all(value is None for value in pwl[:167])
    # Nến 167 (Chủ Nhật 23:00 -> đóng Thứ Hai 00:00 UTC) và toàn bộ tuần 2: giữ đúng tuần 1.
    assert all(value == 125.0 for value in pwh[167:])
    assert all(value == 80.0 for value in pwl[167:])

    # Kiểm tra liquidity_levels phát pwh/pwl đúng mốc armed_at và trạng thái.
    levels = {
        level.key: level
        for level in liquidity_levels(times, highs, lows, closes, timedelta(hours=1))
        if level.key in {"pwh", "pwl"}
    }
    assert set(levels) == {"pwh", "pwl"}
    assert levels["pwh"].price == 125.0
    assert levels["pwh"].armed_at == datetime(2026, 11, 2, 0, tzinfo=UTC)
    assert levels["pwh"].expires_at == datetime(2026, 11, 9, 0, tzinfo=UTC)
    assert levels["pwh"].state == "rejected"  # high[180]=150 > 125 nhưng close=95 <= 125
    assert levels["pwh"].event_index == 180
    assert levels["pwl"].price == 80.0
    assert levels["pwl"].state == "rejected"  # low[190]=60 < 80 nhưng close=95 >= 80
    assert levels["pwl"].event_index == 190


def test_pwh_pwl_ignores_partial_first_week() -> None:
    # Bắt đầu từ thứ Tư 2026-10-28 00:00 UTC (tuần đầu bị khuyết) -> chỉ có PWH/PWL sau khi
    # tuần trọn vẹn tiếp theo (2026-11-02 .. 2026-11-09) kết thúc.
    start = datetime(2026, 10, 28, 0, tzinfo=UTC)
    times = _hourly(start, 24 * 8)  # Tới 2026-11-05 (chưa xong tuần 2)
    highs = [110.0] * len(times)
    lows = [90.0] * len(times)
    closes = [100.0] * len(times)

    pwh, pwl = previous_week_high_low(times, highs, lows, timedelta(hours=1))
    assert all(value is None for value in pwh)
    assert all(value is None for value in pwl)
    weekly = [
        lvl
        for lvl in liquidity_levels(times, highs, lows, closes, timedelta(hours=1))
        if lvl.key in {"pwh", "pwl"}
    ]
    assert weekly == []


def test_pwh_pwl_prefix_invariance() -> None:
    start = datetime(2026, 10, 26, 0, tzinfo=UTC)
    times = _hourly(start, 190)
    highs = [100.0 + (i % 13) for i in range(190)]
    lows = [90.0 - (i % 7) for i in range(190)]
    full_pwh, full_pwl = previous_week_high_low(times, highs, lows, timedelta(hours=1))
    for cut in (10, 166, 167, 168, 189):
        sub_pwh, sub_pwl = previous_week_high_low(
            times[: cut + 1], highs[: cut + 1], lows[: cut + 1], timedelta(hours=1)
        )
        assert sub_pwh == full_pwh[: cut + 1]
        assert sub_pwl == full_pwl[: cut + 1]


# ---------------------------------------------------------------------------
# 2. Độ rộng phiên theo phân vị + base rate kèm n
# ---------------------------------------------------------------------------


def test_session_range_percentiles_causal_and_fail_closed() -> None:
    base = datetime(2026, 10, 1, 0, tzinfo=UTC)
    widths = [10.0, 20.0, 30.0, 40.0, 50.0, 30.0, 60.0]
    ranges = [
        SessionRange(
            key="asia",
            label_vi="Phiên Á (Tokyo)",
            open_at=base + timedelta(days=i),
            close_at=base + timedelta(days=i, hours=9),
            high=100.0 + width,
            low=100.0,
            high_index=i * 10,
            low_index=i * 10 + 1,
            bar_count=9,
            complete=True,
        )
        for i, width in enumerate(widths)
    ]
    # Thêm 1 phiên chưa hoàn tất -> phải bị bỏ qua.
    ranges.append(
        SessionRange(
            key="asia",
            label_vi="Phiên Á (Tokyo)",
            open_at=base + timedelta(days=7),
            close_at=base + timedelta(days=7, hours=9),
            high=999.0,
            low=100.0,
            high_index=70,
            low_index=71,
            bar_count=3,
            complete=False,
        )
    )

    result = session_range_percentiles(ranges, min_samples=5)
    assert len(result) == 7
    assert all(item.formula_version == SESSION_STATS_FORMULA_VERSION for item in result)
    # 5 phiên đầu có n = 0..4 < 5 -> fail-closed (None)
    for i in range(5):
        assert result[i].sample_size == i
        assert result[i].percentile is None
    # Phiên thứ 6 (width=30) so với [10, 20, 30, 40, 50]: 2 nhỏ hơn, 1 bằng -> (2 + 0.5)/5 = 0.5
    assert result[5].sample_size == 5
    assert result[5].percentile == pytest.approx(0.5)
    # Phiên thứ 7 (width=60) so với 6 phiên trước -> lớn hơn tất cả -> 1.0
    assert result[6].sample_size == 6
    assert result[6].percentile == pytest.approx(1.0)


def test_liquidity_base_rates_excludes_armed_and_fail_closed() -> None:
    armed_at = datetime(2026, 10, 1, 9, tzinfo=UTC)
    expires_at = datetime(2026, 10, 2, 0, tzinfo=UTC)
    levels = [
        # 5 mức asia_high đã kết thúc (3 rejected, 1 accepted, 1 expired) + 2 mức đang armed
        LiquidityLevel("asia_high", "Đỉnh Á", "high", 100.0, armed_at, expires_at, "rejected", 1),
        LiquidityLevel("asia_high", "Đỉnh Á", "high", 100.0, armed_at, expires_at, "rejected", 2),
        LiquidityLevel("asia_high", "Đỉnh Á", "high", 100.0, armed_at, expires_at, "rejected", 3),
        LiquidityLevel("asia_high", "Đỉnh Á", "high", 100.0, armed_at, expires_at, "accepted", 4),
        LiquidityLevel("asia_high", "Đỉnh Á", "high", 100.0, armed_at, expires_at, "expired", None),
        LiquidityLevel("asia_high", "Đỉnh Á", "high", 100.0, armed_at, expires_at, "armed", None),
        LiquidityLevel("asia_high", "Đỉnh Á", "high", 100.0, armed_at, expires_at, "armed", None),
        # 2 mức london_low -> thiếu mẫu khi min_samples=5
        LiquidityLevel("london_low", "Đáy Âu", "low", 90.0, armed_at, expires_at, "rejected", 5),
        LiquidityLevel("london_low", "Đáy Âu", "low", 90.0, armed_at, expires_at, "accepted", 6),
    ]
    rates = {item.level_key: item for item in liquidity_base_rates(levels, min_samples=5)}
    asia = rates["asia_high"]
    assert asia.sample_size == 5
    assert (asia.swept_count, asia.accepted_count, asia.expired_count) == (3, 1, 1)
    assert asia.sweep_rate == pytest.approx(0.6)
    assert asia.acceptance_rate == pytest.approx(0.2)
    assert asia.touch_rate == pytest.approx(0.8)

    london = rates["london_low"]
    assert london.sample_size == 2
    assert london.sweep_rate is None
    assert london.acceptance_rate is None
    assert london.touch_rate is None


# ---------------------------------------------------------------------------
# 3. Lookback pivot thích ứng theo ATR
# ---------------------------------------------------------------------------


def test_adaptive_pivot_lookback_responds_to_atr_regime_and_fails_closed() -> None:
    # 4 mẫu None -> thiếu mẫu; sau đó 6 mẫu ATR=1.0 (ratio=1.0 -> base_bars=3),
    # tiếp theo 1 cú sốc ATR=3.0 (mở rộng lên max_bars=6) và ATR=0.2 (thu hẹp xuống min_bars=2).
    atr_series: list[float | None] = [
        None,
        None,
        None,
        None,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        3.0,
        0.2,
    ]
    lookbacks = adaptive_pivot_lookback(
        atr_series, base_bars=3, min_bars=2, max_bars=6, baseline_window=10, min_atr_samples=5
    )
    # 0..3: None vì ATR None; 4..7: None vì chưa đủ 5 mẫu ATR hợp lệ
    assert lookbacks[:8] == (None,) * 8
    assert lookbacks[8] == 3
    assert lookbacks[9] == 3
    assert lookbacks[10] == 6  # ATR tăng vọt -> cửa sổ rộng hơn
    assert lookbacks[11] == 2  # ATR giảm mạnh -> cửa sổ hẹp hơn


def test_adaptive_confirmed_swings_and_structure_prefix_invariant() -> None:
    highs = [10, 11, 12, 13, 15, 13, 12, 11, 10, 9, 10, 12, 14, 16, 14, 12, 11, 12, 14, 17, 15, 13]
    lows = [h - 1.5 for h in highs]
    closes = [h - 0.5 for h in highs]
    atr_series: list[float | None] = [
        *([1.0] * 10),
        *([2.2] * 6),
        *([0.6] * 6),
    ]

    full_swings = adaptive_confirmed_swings(
        highs, lows, atr_series, base_bars=3, min_bars=2, max_bars=5, min_atr_samples=3
    )
    assert full_swings
    for swing in full_swings:
        assert swing.lookback_bars is not None
        assert swing.confirmed_index == swing.index + swing.lookback_bars

    full_struct = adaptive_market_structure(
        highs, lows, closes, atr_series, base_bars=3, min_bars=2, max_bars=5, min_atr_samples=3
    )
    assert full_struct.formula_version == ADAPTIVE_PIVOT_FORMULA_VERSION

    for end in range(1, len(highs) + 1):
        sub_swings = adaptive_confirmed_swings(
            highs[:end],
            lows[:end],
            atr_series[:end],
            base_bars=3,
            min_bars=2,
            max_bars=5,
            min_atr_samples=3,
        )
        assert sub_swings == tuple(s for s in full_swings if s.confirmed_index < end)
        sub_struct = adaptive_market_structure(
            highs[:end],
            lows[:end],
            closes[:end],
            atr_series[:end],
            base_bars=3,
            min_bars=2,
            max_bars=5,
            min_atr_samples=3,
        )
        assert sub_struct.trend == full_struct.trend[:end]
        assert sub_struct.breaks == tuple(b for b in full_struct.breaks if b.break_index < end)


# ---------------------------------------------------------------------------
# 4. Anchored VWAP (CRY-70 / IND-2)
# ---------------------------------------------------------------------------


def test_anchored_vwap_week_and_month_utc_decimal_reset() -> None:
    # 2026-10-31 23:00 (Thứ Bảy, Tháng 10) -> 2026-11-01 00:00 (Chủ Nhật, Tháng 11 -> reset tháng)
    # -> 2026-11-02 00:00 (Thứ Hai, Tuần mới -> reset tuần)
    times = [
        datetime(2026, 10, 31, 23, tzinfo=UTC),
        datetime(2026, 11, 1, 0, tzinfo=UTC),
        datetime(2026, 11, 2, 0, tzinfo=UTC),
        datetime(2026, 11, 2, 1, tzinfo=UTC),
    ]
    highs = [Decimal("102"), Decimal("112"), Decimal("122"), Decimal("132")]
    lows = [Decimal("98"), Decimal("108"), Decimal("118"), Decimal("128")]
    closes = [Decimal("100"), Decimal("110"), Decimal("120"), Decimal("130")]
    volumes = [Decimal("10"), Decimal("20"), Decimal("30"), Decimal("10")]
    # Typical prices: 100, 110, 120, 130

    weekly = anchored_vwap_week_utc(times, highs, lows, closes, volumes)
    assert weekly.anchor_rule is AnchorRule.WEEK_START_UTC
    assert weekly.formula_version == ANCHORED_VWAP_FORMULA_VERSION
    assert weekly.active_anchor_indices == (0, 0, 2, 2)
    assert weekly.values[0] == Decimal("100")
    assert weekly.values[1] == (Decimal("100") * 10 + Decimal("110") * 20) / Decimal("30")
    assert weekly.values[2] == Decimal("120")  # Reset tại Thứ Hai 00:00 UTC
    assert weekly.values[3] == (Decimal("120") * 30 + Decimal("130") * 10) / Decimal("40")

    monthly = anchored_vwap_month_utc(times, highs, lows, closes, volumes)
    assert monthly.anchor_rule is AnchorRule.MONTH_START_UTC
    assert monthly.active_anchor_indices == (0, 1, 1, 1)
    assert monthly.values[0] == Decimal("100")
    assert monthly.values[1] == Decimal("110")  # Reset tại 01/11 00:00 UTC
    assert monthly.values[2] == (Decimal("110") * 20 + Decimal("120") * 30) / Decimal("50")


def test_anchored_vwap_confirmed_event_strictly_causal() -> None:
    times = _hourly(datetime(2026, 11, 1, 0, tzinfo=UTC), 6)
    highs = [Decimal(x) for x in (100, 110, 120, 110, 100, 105)]
    lows = [h - Decimal("2") for h in highs]
    closes = [h - Decimal("1") for h in highs]
    volumes = [Decimal("10")] * 6

    # Pivot tại index 2, nhưng chỉ được xác nhận tại index 4!
    event = ConfirmedAnchorEvent(anchor_index=2, confirmed_index=4, label="swing_high")
    result = anchored_vwap_confirmed_events(times, highs, lows, closes, volumes, [event])

    # Trước index 4: chưa biết pivot -> None!
    assert result.values[:4] == (None, None, None, None)
    assert result.active_anchor_indices[:4] == (None, None, None, None)
    # Từ index 4: tích lũy từ anchor_index=2 tới 4 (tp: 119, 109, 99 -> avg 109)
    assert result.active_anchor_indices[4] == 2
    assert result.values[4] == Decimal("109")
    assert result.active_anchor_indices[5] == 2
    assert result.values[5] == (
        Decimal("119") + Decimal("109") + Decimal("99") + Decimal("104")
    ) / Decimal("4")

    # Kiểm tra trực tiếp anchored_vwap_from_swings cũng không nhìn trước.
    swing_res = anchored_vwap_from_swings(
        times, highs, lows, closes, volumes, left_bars=2, right_bars=2
    )
    assert swing_res.values == result.values
    assert swing_res.active_anchor_indices == result.active_anchor_indices


def test_anchored_vwap_validation() -> None:
    with pytest.raises(ValueError, match="confirmed_index"):
        ConfirmedAnchorEvent(anchor_index=5, confirmed_index=3)
    times = _hourly(datetime(2026, 11, 1, 0, tzinfo=UTC), 2)
    with pytest.raises(ValueError, match="Khối lượng không được âm"):
        anchored_vwap_week_utc(
            times,
            [Decimal("10"), Decimal("10")],
            [Decimal("9"), Decimal("9")],
            [Decimal("9.5"), Decimal("9.5")],
            [Decimal("1"), Decimal("-1")],
        )


# ---------------------------------------------------------------------------
# 5. OBV (On-Balance Volume)
# ---------------------------------------------------------------------------


def test_obv_causal_cumulative_and_validation() -> None:
    closes = [100.0, 102.0, 102.0, 99.0, 101.0]
    volumes = [50.0, 20.0, 15.0, 30.0, 10.0]
    assert obv(closes, volumes) == (0.0, 20.0, 20.0, -10.0, 0.0)
    assert obv([], []) == ()
    with pytest.raises(ValueError, match="Khối lượng không được âm"):
        obv([100.0], [-1.0])


# ---------------------------------------------------------------------------
# 6. Tích hợp feature_snapshot + backward compatibility 100%
# ---------------------------------------------------------------------------


def _make_hourly_bars(count: int, *, start: datetime) -> tuple[OHLCVBar, ...]:
    bars: list[OHLCVBar] = []
    interval = timedelta(hours=1)
    for i in range(count):
        open_time = start + i * interval
        close_time = open_time + interval
        base = (
            Decimal("100")
            + Decimal(str((i % 9) - 4)) * Decimal("0.5")
            + Decimal(i) * Decimal("0.05")
        )
        bars.append(
            OHLCVBar(
                symbol="BTCUSDT",
                timeframe="1h",
                open_time=open_time,
                close_time=close_time,
                event_at=close_time,
                received_at=close_time + timedelta(seconds=10),
                available_at=close_time + timedelta(seconds=10),
                open=base,
                high=base + Decimal("1.5"),
                low=base - Decimal("1.5"),
                close=base + Decimal("0.2"),
                volume=Decimal("100") + Decimal(i % 17),
                is_closed=True,
                source="fixture",
                checksum=f"chk-{i}",
            )
        )
    return tuple(bars)


def test_feature_snapshot_extra_features_opt_in_preserves_legacy_hashes() -> None:
    start = datetime(2026, 10, 26, 0, tzinfo=UTC)
    bars = _make_hourly_bars(200, start=start)
    market = MarketSnapshot(as_of=bars[-1].available_at, bars=bars)
    event_ids = tuple(f"evt-{i}" for i in range(len(bars)))

    # 1. Mặc định không opt-in -> giữ nguyên schema_hash cũ 100%
    legacy_settings = feature_settings_for("1h")
    legacy_snap = FeatureCalculator(legacy_settings).calculate(
        market, workspace_id=WORKSPACE_ID, input_event_ids=event_ids
    )
    assert legacy_snap.schema_hash == feature_schema_hash()
    assert "obv" not in legacy_snap.features
    assert "anchored_vwap_week_utc" not in legacy_snap.features

    # 2. Opt-in các feature mới -> tính đủ, có hash công thức & schema xác định
    extras = (
        "obv",
        "macd_line",
        "macd_signal",
        "macd_histogram",
        "anchored_vwap_week_utc",
        "anchored_vwap_month_utc",
        "anchored_vwap_confirmed_swing",
        "pwh",
        "pwl",
        "adaptive_pivot_lookback",
        "asia_range_percentile",
        "london_range_percentile",
        "new_york_range_percentile",
    )
    assert set(extras) == set(EXTRA_FEATURE_REGISTRY)
    extended_settings = feature_settings_for("1h", extra_features=extras)
    extended_snap = FeatureCalculator(extended_settings).calculate(
        market, workspace_id=WORKSPACE_ID, input_event_ids=event_ids
    )
    assert extended_snap.schema_hash != legacy_snap.schema_hash
    assert extended_snap.formula_hash != legacy_snap.formula_hash
    for name in extras:
        assert name in extended_snap.features
        assert extended_snap.features[name] is not None
        assert name in extended_snap.warmup

    # 3. Replay tính lại -> cùng snapshot_id, formula_hash, schema_hash
    replay = FeatureCalculator(extended_settings).calculate(
        market,
        workspace_id=WORKSPACE_ID,
        input_event_ids=event_ids,
        computed_at=market.as_of + timedelta(minutes=3),
    )
    assert replay.snapshot_id == extended_snap.snapshot_id
    assert replay.formula_hash == extended_snap.formula_hash
    assert replay.schema_hash == extended_snap.schema_hash

    # 4. Từ chối extra_feature chưa đăng ký hoặc trùng tên
    with pytest.raises(ValueError, match="chưa được đăng ký"):
        FeatureSettings(code_hash="a" * 64, extra_features=("unknown_indicator",))
    with pytest.raises(ValueError, match="trùng tên"):
        FeatureSettings(code_hash="a" * 64, extra_features=("obv", "obv"))
    assert LIQUIDITY_FORMULA_VERSION == "sig1-liquidity-v1"
