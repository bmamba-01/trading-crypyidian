"""Golden test trên dữ liệu thật BTCUSDT 1h (BINANCE_USDM_REST_BACKFILL) từ PostgreSQL.

Xác minh:
1. Tính tất định (deterministic) trên dữ liệu thị trường thật 240 nến 1h (2026-10-26 -> 2026-11-04),
   phủ trọn 1 tuần UTC hoàn tất, qua ranh giới tháng 10->11 và 10 ngày phiên Á/Âu/Mỹ.
2. Bất biến tiền tố (prefix invariance — tuyệt đối không nhìn trước): cắt chuỗi tại ``t``
   cho kết quả tại ``t`` trùng khớp 100% với chuỗi dài hơn tại ``t``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from trading_bot.application.strategy_market_requirements import feature_settings_for
from trading_bot.domain.market.anchored_vwap import (
    anchored_vwap_from_swings,
    anchored_vwap_month_utc,
    anchored_vwap_week_utc,
)
from trading_bot.domain.market.feature_snapshot import FeatureCalculator
from trading_bot.domain.market.features import atr, obv
from trading_bot.domain.market.models import MarketSnapshot, OHLCVBar
from trading_bot.domain.market.sessions import (
    liquidity_base_rates,
    liquidity_levels,
    previous_week_high_low,
    session_range_percentiles,
    session_ranges,
)
from trading_bot.domain.market.structure import (
    adaptive_confirmed_swings,
    adaptive_market_structure,
    adaptive_pivot_lookback,
)

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "btcusdt_1h_sig1_golden.json"
WORKSPACE_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
INTERVAL = timedelta(hours=1)


def _load_golden() -> dict[str, object]:
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    bars_raw = raw["bars"]
    open_times = [
        datetime.fromisoformat(item["open_time"].replace("Z", "+00:00")) for item in bars_raw
    ]
    dec_highs = [Decimal(item["high"]) for item in bars_raw]
    dec_lows = [Decimal(item["low"]) for item in bars_raw]
    dec_closes = [Decimal(item["close"]) for item in bars_raw]
    dec_volumes = [Decimal(item["volume"]) for item in bars_raw]
    highs = [float(v) for v in dec_highs]
    lows = [float(v) for v in dec_lows]
    closes = [float(v) for v in dec_closes]
    volumes = [float(v) for v in dec_volumes]
    ohlcv_bars = tuple(
        OHLCVBar(
            symbol="BTCUSDT",
            timeframe="1h",
            open_time=open_times[i],
            close_time=open_times[i] + INTERVAL,
            event_at=open_times[i] + INTERVAL,
            received_at=open_times[i] + INTERVAL + timedelta(seconds=5),
            available_at=open_times[i] + INTERVAL + timedelta(seconds=5),
            open=Decimal(item["open"]),
            high=dec_highs[i],
            low=dec_lows[i],
            close=dec_closes[i],
            volume=dec_volumes[i],
            quote_volume=Decimal(item["quote_volume"])
            if item["quote_volume"] is not None
            else None,
            taker_buy_base_volume=(
                Decimal(item["taker_buy_base_volume"])
                if item["taker_buy_base_volume"] is not None
                else None
            ),
            trade_count=item["trade_count"],
            is_closed=True,
            source=raw["source"],
            checksum=f"golden-btc-1h-{i}",
        )
        for i, item in enumerate(bars_raw)
    )
    return {
        "open_times": open_times,
        "dec_highs": dec_highs,
        "dec_lows": dec_lows,
        "dec_closes": dec_closes,
        "dec_volumes": dec_volumes,
        "highs": highs,
        "lows": lows,
        "closes": closes,
        "volumes": volumes,
        "bars": ohlcv_bars,
    }


def test_real_btcusdt_golden_fixture_integrity() -> None:
    data = _load_golden()
    open_times: list[datetime] = data["open_times"]  # type: ignore[assignment]
    assert len(open_times) == 240
    assert open_times[0] == datetime(2026, 10, 26, 0, tzinfo=UTC)  # Thứ Hai 00:00 UTC
    assert open_times[144] == datetime(2026, 11, 1, 0, tzinfo=UTC)  # Đầu tháng 11 UTC
    assert open_times[168] == datetime(2026, 11, 2, 0, tzinfo=UTC)  # Thứ Hai tuần kế tiếp
    assert open_times[-1] == datetime(2026, 11, 4, 23, tzinfo=UTC)


def test_real_btcusdt_golden_prefix_invariance_no_lookahead() -> None:
    """Cắt chuỗi dữ liệu thật tại nhiều mốc t (trước/tại/sau đổi tháng & đổi tuần):
    mọi feature tại t phải trùng khớp tuyệt đối với kết quả trên toàn chuỗi 240 nến.
    """
    data = _load_golden()
    open_times: list[datetime] = data["open_times"]  # type: ignore[assignment]
    dec_highs: list[Decimal] = data["dec_highs"]  # type: ignore[assignment]
    dec_lows: list[Decimal] = data["dec_lows"]  # type: ignore[assignment]
    dec_closes: list[Decimal] = data["dec_closes"]  # type: ignore[assignment]
    dec_volumes: list[Decimal] = data["dec_volumes"]  # type: ignore[assignment]
    highs: list[float] = data["highs"]  # type: ignore[assignment]
    lows: list[float] = data["lows"]  # type: ignore[assignment]
    closes: list[float] = data["closes"]  # type: ignore[assignment]
    volumes: list[float] = data["volumes"]  # type: ignore[assignment]

    full_pwh, full_pwl = previous_week_high_low(open_times, highs, lows, INTERVAL)
    full_avwap_w = anchored_vwap_week_utc(open_times, dec_highs, dec_lows, dec_closes, dec_volumes)
    full_avwap_m = anchored_vwap_month_utc(open_times, dec_highs, dec_lows, dec_closes, dec_volumes)
    full_avwap_s = anchored_vwap_from_swings(
        open_times, dec_highs, dec_lows, dec_closes, dec_volumes, left_bars=3, right_bars=3
    )
    full_obv = obv(closes, volumes)
    full_atr = atr(highs, lows, closes, 14)
    full_lookback = adaptive_pivot_lookback(full_atr)
    full_swings = adaptive_confirmed_swings(highs, lows, full_atr)
    full_struct = adaptive_market_structure(highs, lows, closes, full_atr)
    full_ranges = session_ranges(open_times, highs, lows, INTERVAL)
    full_percentiles = {
        (item.session_key, item.open_at): item
        for item in session_range_percentiles(full_ranges, min_samples=5)
    }

    # Xác nhận các mốc reset và giá trị thật đều sinh dữ liệu hợp lệ trên chuỗi 240 nến.
    assert full_pwh[166] is None
    assert full_pwh[167] == max(highs[:168])
    assert full_pwl[167] == min(lows[:168])
    assert full_avwap_m.active_anchor_indices[144] == 144  # Reset 01/11 00:00 UTC
    assert full_avwap_w.active_anchor_indices[168] == 168  # Reset Thứ Hai 02/11 00:00 UTC
    assert full_avwap_s.values[-1] is not None
    assert len(full_swings) >= 5
    assert any(item.percentile is not None for item in full_percentiles.values())

    full_levels = liquidity_levels(open_times, highs, lows, closes, INTERVAL)
    assert any(lvl.key == "pwh" for lvl in full_levels)
    assert any(lvl.key == "pwl" for lvl in full_levels)
    full_rates = {item.level_key: item for item in liquidity_base_rates(full_levels, min_samples=5)}
    assert full_rates["asia_high"].sample_size >= 5
    assert full_rates["asia_high"].sweep_rate is not None

    # Kiểm tra bất biến tiền tố (prefix invariance) tại các mốc cắt then chốt.
    for cut in (40, 120, 143, 144, 166, 167, 168, 200, 239):
        end = cut + 1
        sub_pwh, sub_pwl = previous_week_high_low(
            open_times[:end], highs[:end], lows[:end], INTERVAL
        )
        assert sub_pwh == full_pwh[:end]
        assert sub_pwl == full_pwl[:end]

        sub_w = anchored_vwap_week_utc(
            open_times[:end], dec_highs[:end], dec_lows[:end], dec_closes[:end], dec_volumes[:end]
        )
        assert sub_w.values == full_avwap_w.values[:end]
        assert sub_w.active_anchor_indices == full_avwap_w.active_anchor_indices[:end]

        sub_m = anchored_vwap_month_utc(
            open_times[:end], dec_highs[:end], dec_lows[:end], dec_closes[:end], dec_volumes[:end]
        )
        assert sub_m.values == full_avwap_m.values[:end]
        assert sub_m.active_anchor_indices == full_avwap_m.active_anchor_indices[:end]

        sub_s = anchored_vwap_from_swings(
            open_times[:end],
            dec_highs[:end],
            dec_lows[:end],
            dec_closes[:end],
            dec_volumes[:end],
            left_bars=3,
            right_bars=3,
        )
        assert sub_s.values == full_avwap_s.values[:end]
        assert sub_s.active_anchor_indices == full_avwap_s.active_anchor_indices[:end]

        assert obv(closes[:end], volumes[:end]) == full_obv[:end]

        sub_atr = atr(highs[:end], lows[:end], closes[:end], 14)
        assert adaptive_pivot_lookback(sub_atr) == full_lookback[:end]
        assert adaptive_confirmed_swings(highs[:end], lows[:end], sub_atr) == tuple(
            s for s in full_swings if s.confirmed_index < end
        )
        sub_struct = adaptive_market_structure(highs[:end], lows[:end], closes[:end], sub_atr)
        assert sub_struct.trend == full_struct.trend[:end]
        assert sub_struct.breaks == tuple(b for b in full_struct.breaks if b.break_index < end)

        sub_ranges = session_ranges(open_times[:end], highs[:end], lows[:end], INTERVAL)
        for item in session_range_percentiles(sub_ranges, min_samples=5):
            expected = full_percentiles[(item.session_key, item.open_at)]
            assert item == expected


def test_real_btcusdt_feature_snapshot_determinism_with_all_extra_features() -> None:
    data = _load_golden()
    bars: tuple[OHLCVBar, ...] = data["bars"]  # type: ignore[assignment]
    settings = feature_settings_for(
        "1h",
        extra_features=(
            "obv",
            "anchored_vwap_week_utc",
            "anchored_vwap_month_utc",
            "anchored_vwap_confirmed_swing",
            "pwh",
            "pwl",
            "adaptive_pivot_lookback",
            "asia_range_percentile",
            "london_range_percentile",
            "new_york_range_percentile",
        ),
    )
    market = MarketSnapshot(as_of=bars[-1].available_at, bars=bars)
    event_ids = tuple(f"btc-1h-{i}" for i in range(len(bars)))
    snap1 = FeatureCalculator(settings).calculate(
        market, workspace_id=WORKSPACE_ID, input_event_ids=event_ids
    )
    snap2 = FeatureCalculator(settings).calculate(
        market,
        workspace_id=WORKSPACE_ID,
        input_event_ids=event_ids,
        computed_at=market.as_of + timedelta(minutes=10),
    )
    assert snap1.snapshot_id == snap2.snapshot_id
    assert snap1.formula_hash == snap2.formula_hash
    assert snap1.schema_hash == snap2.schema_hash
    assert snap1.features["pwh"] == pytest.approx(max(float(b.high) for b in bars[:168]))
    assert snap1.features["pwl"] == pytest.approx(min(float(b.low) for b in bars[:168]))
    assert snap1.features["anchored_vwap_week_utc"] is not None
    assert snap1.features["anchored_vwap_month_utc"] is not None
    assert snap1.features["anchored_vwap_confirmed_swing"] is not None
    assert snap1.features["obv"] is not None
    assert snap1.features["adaptive_pivot_lookback"] is not None
    assert snap1.features["asia_range_percentile"] is not None
    assert snap1.features["london_range_percentile"] is not None
    assert snap1.features["new_york_range_percentile"] is not None
