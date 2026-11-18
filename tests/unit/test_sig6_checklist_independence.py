"""Unit test SIG-6 (CRY-419): độ độc lập điều kiện checklist ('x/y điều kiện độc lập')."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.application.strategy_pipeline.card_strategy import strategy_card_release_id
from trading_bot.application.strategy_pipeline.demo_cards import build_default_demo_cards
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.strategy.checklists import (
    CHECKLIST_INDEPENDENCE_VERSION,
    INDEPENDENCE_THRESHOLD,
    MIN_INDEPENDENCE_SAMPLES,
    ChecklistIndependenceSummary,
    ChecklistItem,
    checklist_condition_series,
    checklist_independence,
    strategy_checklist,
)
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    FeatureObservation,
    FeatureQuality,
    MarketSeries,
    SeriesQuality,
    StrategyCandidate,
    StrategySelection,
)
from trading_bot.domain.strategy.rules import (
    H01EmaTrendRule,
    H03DonchianBreakoutRule,
    H06RangeMeanReversionRule,
)

START = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
INTERVALS = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
}


def _bar(
    index: int,
    timeframe: str,
    close: float,
    *,
    open_price: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 100.0,
    quote_volume: float | None = None,
    taker_buy_base: float | None = 52.0,
    start: datetime = START,
    is_closed: bool = True,
    available_offset: timedelta = timedelta(0),
) -> OHLCVBar:
    duration = INTERVALS[timeframe]
    opened = start + index * duration
    closed = opened + duration - timedelta(microseconds=1)
    c = Decimal(str(round(close, 4)))
    o = Decimal(str(round(close if open_price is None else open_price, 4)))
    h = Decimal(str(round(max(close, float(o)) + 1.0 if high is None else high, 4)))
    low_val = Decimal(str(round(min(close, float(o)) - 1.0 if low is None else low, 4)))
    qvol = Decimal(str(round(volume * close if quote_volume is None else quote_volume, 4)))
    tbuy = None if taker_buy_base is None else Decimal(str(round(taker_buy_base, 4)))
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe=timeframe,
        open_time=opened,
        close_time=closed,
        event_at=closed,
        received_at=closed,
        available_at=closed + available_offset,
        open=o,
        high=h,
        low=low_val,
        close=c,
        volume=Decimal(str(round(volume, 4))),
        quote_volume=qvol,
        taker_buy_base_volume=tbuy,
        is_closed=is_closed,
    )


def _feature(
    name: str,
    timeframe: str,
    value: float | None,
    at: datetime,
    *,
    previous: float | None = None,
    quality: FeatureQuality = FeatureQuality.READY,
) -> FeatureObservation:
    return FeatureObservation(
        feature_id=f"{timeframe}:{name}:{int(at.timestamp())}",
        name=name,
        timeframe=timeframe,
        value=value,
        previous_value=value if previous is None else previous,
        available_at=at,
        quality=quality,
    )


def _snapshot(
    series: tuple[MarketSeries, ...],
    features: tuple[FeatureObservation, ...] = (),
    *,
    decision_time: datetime | None = None,
    position_is_flat: bool | None = True,
    macro_blackout: bool | None = False,
    series_quality: SeriesQuality = SeriesQuality.READY,
    backfill_complete: bool | None = True,
) -> DecisionSnapshot:
    latest = decision_time or max(
        (bar.available_at for item in series for bar in item.bars),
        default=START,
    )
    qualified = tuple(
        MarketSeries(
            timeframe=item.timeframe,
            bars=item.bars,
            quote_volumes=item.quote_volumes,
            quality=series_quality,
            expected_interval=INTERVALS[item.timeframe],
            max_event_age=2 * INTERVALS[item.timeframe],
            backfill_complete=backfill_complete,
        )
        for item in series
    )
    return DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=latest,
        snapshot_id="snap-sig6-checklist-independence",
        series=qualified,
        features=features,
        position_is_flat=position_is_flat,
        macro_blackout=macro_blackout,
        macro_evidence_ids=("macro-1",),
        macro_evidence_available_at=latest,
        macro_is_fresh=True,
    )


def _selection(release_id: str, **parameters: int) -> StrategySelection:
    return StrategySelection(
        settings_version_id="settings-sig6-1",
        active_release_ids=(release_id,),
        published=True,
        parameters=tuple(sorted(parameters.items())),
    )


def _multi_regime_bars(
    count: int = 240,
    timeframe: str = "1h",
    *,
    start: datetime = START,
) -> tuple[tuple[OHLCVBar, ...], tuple[float, ...]]:
    """Sinh chuỗi nến đa chế độ tất định có biến động giá, biên độ và khối lượng độc lập."""
    bars: list[OHLCVBar] = []
    qvols: list[float] = []
    close = 100.0
    for idx in range(count):
        if idx < 60:
            drift = math.sin(idx / 4.5) * 1.15 + math.cos(idx / 2.3) * 0.55
            spread = 0.75 + abs(math.sin(idx * 0.9)) * 0.65
            vol = 110.0 + 35.0 * math.sin(idx * 0.67 + 0.4)
        elif idx < 110:
            drift = 0.08 * math.sin(idx * 1.4)
            spread = 0.2 + 0.1 * abs(math.cos(idx * 0.8))
            vol = 85.0 + 25.0 * math.cos(idx * 1.1)
        elif idx < 170:
            drift = 0.45 + 0.9 * math.sin(idx / 3.2)
            spread = 0.85 + 0.5 * abs(math.sin(idx * 0.7))
            vol = 140.0 + 60.0 * math.sin(idx * 1.3 + 1.2)
        else:
            drift = -0.35 + 1.25 * math.sin(idx / 4.1)
            spread = 0.9 + 0.55 * abs(math.cos(idx * 0.85))
            vol = 135.0 + 55.0 * math.cos(idx * 0.73 + 0.8)

        open_p = close
        close = max(15.0, open_p + drift)
        high = max(open_p, close) + spread * (0.4 + 0.5 * abs(math.sin(idx * 0.9)))
        low = min(open_p, close) - spread * (0.4 + 0.5 * abs(math.cos(idx * 1.1)))
        qvol = max(100.0, vol * (95.0 + 15.0 * math.sin(idx * 0.53 + 2.1)))
        bars.append(
            _bar(
                idx,
                timeframe,
                close,
                open_price=open_p,
                high=high,
                low=low,
                volume=vol,
                quote_volume=qvol,
                taker_buy_base=vol * (0.5 + 0.2 * math.sin(idx * 0.8)),
                start=start,
            )
        )
        qvols.append(round(qvol, 4))
    return tuple(bars), tuple(qvols)


def _aggregate_1h_to_4h(bars_1h: tuple[OHLCVBar, ...]) -> tuple[OHLCVBar, ...]:
    """Gộp mỗi 4 nến 1h liên tiếp thành 1 nến 4h đã đóng đúng mốc thời gian (zero lookahead)."""
    bars_4h: list[OHLCVBar] = []
    num_groups = len(bars_1h) // 4
    for g in range(num_groups):
        chunk = bars_1h[g * 4 : (g + 1) * 4]
        opened = chunk[0].open_time
        closed = chunk[-1].close_time
        high = max(b.high for b in chunk)
        low = min(b.low for b in chunk)
        vol = sum((b.volume for b in chunk), Decimal("0"))
        qvol = sum((b.quote_volume or Decimal("0") for b in chunk), Decimal("0"))
        bars_4h.append(
            OHLCVBar(
                symbol=chunk[0].symbol,
                timeframe="4h",
                open_time=opened,
                close_time=closed,
                event_at=closed,
                received_at=closed,
                available_at=closed,
                open=chunk[0].open,
                high=high,
                low=low,
                close=chunk[-1].close,
                volume=vol,
                quote_volume=qvol,
                is_closed=True,
            )
        )
    return tuple(bars_4h)


def _h01_rich_snapshot(bars_1h_count: int = 240) -> DecisionSnapshot:
    bars_1h, _ = _multi_regime_bars(bars_1h_count, "1h")
    bars_4h = _aggregate_1h_to_4h(bars_1h)
    at = bars_1h[-1].available_at
    feats = (
        _feature("ema_20", "1h", 105.0, at, previous=103.0),
        _feature("ema_80", "1h", 104.0, at, previous=103.5),
        _feature("adx_14", "1h", 24.0, at),
        _feature("atr_14", "1h", 1.8, at),
        _feature(
            "ema_200",
            "4h",
            float(bars_4h[-1].close) - 5.0 if bars_4h else 100.0,
            bars_4h[-1].available_at if bars_4h else at,
        ),
    )
    series: tuple[MarketSeries, ...] = (
        (MarketSeries("1h", bars_1h), MarketSeries("4h", bars_4h))
        if bars_4h
        else (MarketSeries("1h", bars_1h),)
    )
    return _snapshot(series, feats, decision_time=at)


def _h03_rich_snapshot(
    bars_count: int = 160,
    timeframe: str = "1h",
    *,
    with_quote_volumes: bool = True,
) -> DecisionSnapshot:
    bars, qvols = _multi_regime_bars(bars_count, timeframe)
    # Đảm bảo nến cuối phá kênh Donchian 55 nến và quote_volume > median 20 nến để đạt candidate
    if bars_count >= 56:
        prior_high = max(float(b.high) for b in bars[-56:-1])
        prior_qvol_med = sorted(qvols[-21:-1])[10]
        last_close = prior_high + 2.5
        last_qvol = prior_qvol_med * 1.6
        last_bar = _bar(
            bars_count - 1,
            timeframe,
            last_close,
            open_price=last_close - 1.0,
            high=last_close + 0.5,
            low=last_close - 1.5,
            volume=180.0,
            quote_volume=last_qvol,
        )
        bars = (*bars[:-1], last_bar)
        qvols = (*qvols[:-1], round(last_qvol, 4))
    at = bars[-1].available_at
    return _snapshot(
        (MarketSeries(timeframe, bars, quote_volumes=qvols if with_quote_volumes else None),),
        (_feature("atr_14", timeframe, 2.0, at),),
        decision_time=at,
    )


def _h06_rich_snapshot(bars_count: int = 180) -> DecisionSnapshot:
    bars, _ = _multi_regime_bars(bars_count, "15m")
    if bars_count >= 121:
        floor = min(float(b.low) for b in bars[-121:-1])
        ceil = max(float(b.high) for b in bars[-121:-1])
        safe_close = 0.5 * (floor + ceil)
        last_bar = _bar(
            bars_count - 1,
            "15m",
            safe_close,
            open_price=safe_close + 0.2,
            high=safe_close + 0.5,
            low=max(floor + 0.1, safe_close - 0.5),
        )
        bars = (*bars[:-1], last_bar)
    at = bars[-1].available_at
    feats = (
        _feature("adx_14", "15m", 16.5, at),
        _feature("efficiency_ratio_40", "15m", 0.18, at),
        _feature("zscore_20", "15m", -2.35, at),
        _feature("rsi_14", "15m", 26.0, at),
        _feature("atr_14", "15m", 1.2, at),
    )
    return _snapshot((MarketSeries("15m", bars),), feats, decision_time=at)


def test_h01_h03_h06_orthogonal_independence_and_checklist_integration() -> None:
    """Đủ mẫu nến đa chế độ: H01 (3/3), H03 (2/2), H06 (4/4) điều kiện độc lập và giữ parity."""
    # 1. H01: 3/3 điều kiện độc lập
    snap_h01 = _h01_rich_snapshot(240)
    sel_h01 = _selection("h01-v1")
    ind_h01 = checklist_independence("h01-v1", snap_h01, sel_h01)
    assert isinstance(ind_h01, ChecklistIndependenceSummary)
    assert ind_h01.version == CHECKLIST_INDEPENDENCE_VERSION
    assert ind_h01.status == "ok"
    assert ind_h01.sample_size == 240
    assert ind_h01.total_conditions == 3
    assert ind_h01.independent_conditions == 3
    assert ind_h01.summary_vi == "3/3 điều kiện độc lập"
    assert ind_h01.warnings == ()
    assert ind_h01.high_correlation_pairs == ()
    assert ind_h01.inactive_conditions == ()

    res_h01 = H01EmaTrendRule().evaluate(snap_h01, sel_h01)
    assert isinstance(res_h01, StrategyCandidate)
    items_h01 = strategy_checklist("h01-v1", snap_h01, sel_h01)
    assert len(items_h01) == 3
    assert all(item.passed is True for item in items_h01)
    assert all(item.independence_summary == "3/3 điều kiện độc lập" for item in items_h01)
    assert all(
        item.as_dict()["independence_summary"] == "3/3 điều kiện độc lập" for item in items_h01
    )

    # 2. H03 (v1, v2, v3-15m, v3-1h, v3-4h): 2/2 điều kiện độc lập
    for rel, tf in (
        ("h03-v1", "1h"),
        ("h03-v2", "1h"),
        ("h03-v3-15m", "15m"),
        ("h03-v3-1h", "1h"),
        ("h03-v3-4h", "4h"),
    ):
        snap_h03 = _h03_rich_snapshot(160, tf)
        sel_h03 = _selection(rel)
        ind_h03 = checklist_independence(rel, snap_h03, sel_h03)
        assert ind_h03.status == "ok"
        assert ind_h03.sample_size == 160
        assert ind_h03.total_conditions == 2
        assert ind_h03.independent_conditions == 2
        assert ind_h03.summary_vi == "2/2 điều kiện độc lập"
        assert ind_h03.warnings == ()

        items_h03 = strategy_checklist(rel, snap_h03, sel_h03)
        assert len(items_h03) == 2
        assert all(item.passed is True for item in items_h03)
        assert all(item.independence_summary == "2/2 điều kiện độc lập" for item in items_h03)

    res_h03 = H03DonchianBreakoutRule().evaluate(
        _h03_rich_snapshot(160, "1h"), _selection("h03-v1")
    )
    assert isinstance(res_h03, StrategyCandidate)

    # 3. H06 (v1, v2): 4/4 điều kiện độc lập
    for rel in ("h06-v1", "h06-v2"):
        snap_h06 = _h06_rich_snapshot(180)
        sel_h06 = _selection(rel)
        ind_h06 = checklist_independence(rel, snap_h06, sel_h06)
        assert ind_h06.status == "ok"
        assert ind_h06.sample_size == 180
        assert ind_h06.total_conditions == 4
        assert ind_h06.independent_conditions == 4
        assert ind_h06.summary_vi == "4/4 điều kiện độc lập"
        assert ind_h06.warnings == ()

        items_h06 = strategy_checklist(rel, snap_h06, sel_h06)
        assert len(items_h06) == 5
        assert all(item.passed is True for item in items_h06)
        assert all(item.independence_summary == "4/4 điều kiện độc lập" for item in items_h06)

    res_h06 = H06RangeMeanReversionRule().evaluate(_h06_rich_snapshot(180), _selection("h06-v1"))
    assert isinstance(res_h06, StrategyCandidate)


def test_collinear_conditions_reduce_independent_count_and_emit_warning() -> None:
    """Khi nhiều điều kiện thực chất đo cùng một biến động (|r| >= 0.85), gộp nhóm và cảnh báo."""
    # 1. H03: 59 nến phẳng + 1 nến bứt phá đồng thời cả giá và quote_volume -> r ≈ 1.0 -> 1/2
    bars_h03 = [_bar(i, "1h", 96.0, high=100.0, low=95.0) for i in range(59)]
    bars_h03.append(_bar(59, "1h", 102.0, high=102.5, low=95.0))
    qvols_h03 = (5.0,) * 59 + (12.0,)
    snap_h03_col = _snapshot(
        (MarketSeries("1h", tuple(bars_h03), quote_volumes=qvols_h03),),
        (_feature("atr_14", "1h", 2.0, bars_h03[-1].available_at),),
    )
    ind_h03 = checklist_independence("h03-v1", snap_h03_col, _selection("h03-v1"))
    assert ind_h03.status == "warning"
    assert ind_h03.sample_size == 60
    assert ind_h03.total_conditions == 2
    assert ind_h03.independent_conditions == 1
    assert ind_h03.summary_vi == "1/2 điều kiện độc lập"
    assert ind_h03.groups == (("donchian_breakout", "volume_above_median"),)
    assert len(ind_h03.high_correlation_pairs) == 1
    assert abs(ind_h03.high_correlation_pairs[0][2]) >= INDEPENDENCE_THRESHOLD
    assert ind_h03.warnings
    items_h03 = strategy_checklist("h03-v1", snap_h03_col, _selection("h03-v1"))
    assert all(item.independence_summary == "1/2 điều kiện độc lập" for item in items_h03)

    # 2. H06: nửa đầu đi ngang hẹp (ADX thấp, ER thấp) -> nửa sau xu hướng thẳng (ADX cao, ER cao)
    # khiến range_adx và range_er tương quan mạnh (r > 0.85) -> 3/4 điều kiện độc lập.
    bars_h06: list[OHLCVBar] = []
    c = 100.0
    for i in range(125):
        if i < 62:
            c = 100.0 + (0.4 if i % 2 == 0 else -0.4) + 0.05 * math.sin(i)
            h, low_v = c + 0.45, c - 0.45
        else:
            c += 0.8 + 0.02 * math.sin(i)
            h, low_v = c + 0.2, c - 0.2
        bars_h06.append(_bar(i, "15m", c, high=h, low=low_v))
    at_h06 = bars_h06[-1].available_at
    snap_h06_col = _snapshot(
        (MarketSeries("15m", tuple(bars_h06)),),
        (
            _feature("adx_14", "15m", 17.0, at_h06),
            _feature("efficiency_ratio_40", "15m", 0.20, at_h06),
            _feature("zscore_20", "15m", -2.2, at_h06),
            _feature("rsi_14", "15m", 25.0, at_h06),
            _feature("atr_14", "15m", 1.0, at_h06),
        ),
    )
    ind_h06 = checklist_independence("h06-v1", snap_h06_col, _selection("h06-v1"))
    assert ind_h06.status == "warning"
    assert ind_h06.total_conditions == 4
    assert ind_h06.independent_conditions == 3
    assert ind_h06.summary_vi == "3/4 điều kiện độc lập"
    assert ("range_adx", "range_er") in {p[:2] for p in ind_h06.high_correlation_pairs}
    assert ind_h06.warnings

    # 3. H01: 1h và 4h cùng nhảy bậc tại cuối chuỗi -> ema_cross và htf_trend đồng tuyến
    bars_1h_col = [_bar(i, "1h", 100.0, high=100.5, low=99.5) for i in range(236)]
    for i in range(236, 240):
        bars_1h_col.append(_bar(i, "1h", 140.0, high=141.0, low=139.0))
    bars_4h_col = _aggregate_1h_to_4h(tuple(bars_1h_col))
    at_h01 = bars_1h_col[-1].available_at
    snap_h01_col = _snapshot(
        (MarketSeries("1h", tuple(bars_1h_col)), MarketSeries("4h", bars_4h_col)),
        (
            _feature("ema_20", "1h", 115.0, at_h01, previous=100.0),
            _feature("ema_80", "1h", 104.0, at_h01, previous=100.0),
            _feature("adx_14", "1h", 25.0, at_h01),
            _feature("atr_14", "1h", 2.0, at_h01),
            _feature("ema_200", "4h", 101.0, bars_4h_col[-1].available_at),
        ),
    )
    ind_h01 = checklist_independence("h01-v1", snap_h01_col, _selection("h01-v1"))
    assert ind_h01.status == "warning"
    assert ind_h01.total_conditions == 3
    assert ind_h01.independent_conditions is not None
    assert ind_h01.independent_conditions < 3
    assert ind_h01.summary_vi == f"{ind_h01.independent_conditions}/3 điều kiện độc lập"
    assert ind_h01.warnings


def test_insufficient_data_on_short_flat_stale_or_unmeasured_series() -> None:
    """Thiếu mẫu (< 50 nến), chuỗi phẳng hoàn toàn hoặc stale phải trả insufficient_data."""
    # 1. Ít hơn MIN_INDEPENDENCE_SAMPLES (49 nến)
    short_h01 = _h01_rich_snapshot(MIN_INDEPENDENCE_SAMPLES - 1)
    ind_short = checklist_independence("h01-v1", short_h01, _selection("h01-v1"))
    assert ind_short.status == "insufficient_data"
    assert ind_short.sample_size == MIN_INDEPENDENCE_SAMPLES - 1
    assert ind_short.total_conditions == 3
    assert ind_short.independent_conditions is None
    assert "Chưa đủ dữ liệu" in ind_short.summary_vi
    items_short = strategy_checklist("h01-v1", short_h01, _selection("h01-v1"))
    assert all(item.independence_summary == ind_short.summary_vi for item in items_short)

    # 2. Chuỗi phẳng hoàn toàn (không có phương sai ở bất kỳ điều kiện nào)
    flat_bars = tuple(
        _bar(i, "1h", 100.0, open_price=100.0, high=100.0, low=100.0, quote_volume=1000.0)
        for i in range(60)
    )
    flat_snap = _snapshot(
        (MarketSeries("1h", flat_bars, quote_volumes=(1000.0,) * 60),),
        (_feature("atr_14", "1h", 1.0, flat_bars[-1].available_at),),
    )
    ind_flat = checklist_independence("h03-v1", flat_snap, _selection("h03-v1"))
    assert ind_flat.status == "insufficient_data"
    assert ind_flat.independent_conditions is None
    assert set(ind_flat.inactive_conditions) == {"donchian_breakout", "volume_above_median"}

    # 3. H03 thiếu quote_volumes -> volume_above_median bị đưa vào inactive_conditions, trả 1/2
    snap_no_qvol = _h03_rich_snapshot(80, "1h", with_quote_volumes=False)
    ind_no_qvol = checklist_independence("h03-v1", snap_no_qvol, _selection("h03-v1"))
    assert ind_no_qvol.status == "ok"
    assert ind_no_qvol.total_conditions == 2
    assert ind_no_qvol.independent_conditions == 1
    assert ind_no_qvol.inactive_conditions == ("volume_above_median",)
    assert ind_no_qvol.summary_vi == "1/2 điều kiện độc lập"

    # 4. H01 chỉ có 1 nến 4h -> htf_trend không có biến thiên lịch sử, trả 2/3
    bars_1h, _ = _multi_regime_bars(80, "1h")
    one_4h = (_bar(0, "4h", 105.0),)
    snap_one_4h = _snapshot(
        (MarketSeries("1h", bars_1h), MarketSeries("4h", one_4h)),
        (_feature("adx_14", "1h", 25.0, bars_1h[-1].available_at),),
        decision_time=bars_1h[-1].available_at,
    )
    ind_one_4h = checklist_independence("h01-v1", snap_one_4h, _selection("h01-v1"))
    assert ind_one_4h.status == "ok"
    assert ind_one_4h.total_conditions == 3
    assert ind_one_4h.independent_conditions == 2
    assert ind_one_4h.inactive_conditions == ("htf_trend",)
    assert ind_one_4h.summary_vi == "2/3 điều kiện độc lập"

    # 5. Series STALE hoặc backfill_complete=False -> insufficient_data
    snap_stale = _snapshot(
        (MarketSeries("15m", _multi_regime_bars(80, "15m")[0]),),
        series_quality=SeriesQuality.STALE,
    )
    ind_stale = checklist_independence("h06-v1", snap_stale, _selection("h06-v1"))
    assert ind_stale.status == "insufficient_data"
    assert ind_stale.sample_size == 0
    assert ind_stale.independent_conditions is None

    # 6. Release không tồn tại -> insufficient_data, total_conditions = 0
    ind_unknown = checklist_independence("unknown-release-v1", short_h01, _selection("x"))
    assert ind_unknown.status == "insufficient_data"
    assert ind_unknown.total_conditions == 0
    assert ind_unknown.independent_conditions is None


@pytest.mark.parametrize(
    ("release_id", "builder", "full_count", "prefix_count"),
    [
        ("h01-v1", _h01_rich_snapshot, 160, 96),
        ("h03-v1", _h03_rich_snapshot, 140, 80),
        ("h06-v1", _h06_rich_snapshot, 160, 90),
    ],
)
def test_zero_lookahead_prefix_invariance_and_future_bar_filtering(
    release_id: str,
    builder: object,
    full_count: int,
    prefix_count: int,
) -> None:
    """Không nhìn trước: điểm số tại nến t chỉ phụ thuộc các nến <= t; nến tương lai bị loại."""
    full_snap: DecisionSnapshot = builder(full_count)  # type: ignore[operator]
    # Cắt đúng prefix_count nến đầu tiên của khung chính và lọc khung phụ <= decision_time mới
    primary_tf = "15m" if release_id.startswith("h06") else "1h"
    primary_series = full_snap.get_series(primary_tf)
    assert primary_series is not None
    cutoff_bars = primary_series.bars[:prefix_count]
    cutoff_time = cutoff_bars[-1].available_at

    truncated_series: list[MarketSeries] = []
    for s in full_snap.series:
        if s.timeframe == primary_tf:
            qvols = s.quote_volumes[:prefix_count] if s.quote_volumes is not None else None
            truncated_series.append(replace(s, bars=cutoff_bars, quote_volumes=qvols))
        else:
            valid_htf = tuple(b for b in s.bars if b.available_at <= cutoff_time)
            truncated_series.append(replace(s, bars=valid_htf))

    prefix_snap = replace(
        full_snap,
        decision_time=cutoff_time,
        series=tuple(truncated_series),
        macro_evidence_available_at=cutoff_time,
    )

    sel = _selection(release_id)
    full_scores = checklist_condition_series(release_id, full_snap, sel)
    prefix_scores = checklist_condition_series(release_id, prefix_snap, sel)
    assert set(full_scores) == set(prefix_scores)
    for key, prefix_vals in prefix_scores.items():
        assert len(prefix_vals) == prefix_count
        assert full_scores[key][:prefix_count] == pytest.approx(prefix_vals)

    # Gắn thêm nến tương lai (available_at > cutoff_time) và nến chưa đóng (is_closed=False)
    # vào prefix_snap nhưng giữ nguyên decision_time = cutoff_time -> kết quả không được đổi!
    future_bar = _bar(
        prefix_count,
        primary_tf,
        9999.0,
        high=10500.0,
        low=50.0,
        volume=99999.0,
        quote_volume=99999999.0,
        available_offset=timedelta(hours=10),
    )
    unclosed_bar = _bar(
        prefix_count + 1,
        primary_tf,
        1.0,
        high=2.0,
        low=0.5,
        volume=50000.0,
        quote_volume=50000.0,
        is_closed=False,
    )
    contaminated_series: list[MarketSeries] = []
    for s in prefix_snap.series:
        if s.timeframe == primary_tf:
            ext_bars = (*s.bars, future_bar, unclosed_bar)
            ext_qvols = (
                (*s.quote_volumes, 99999999.0, 50000.0) if s.quote_volumes is not None else None
            )
            contaminated_series.append(replace(s, bars=ext_bars, quote_volumes=ext_qvols))
        else:
            contaminated_series.append(s)

    contaminated_snap = replace(prefix_snap, series=tuple(contaminated_series))
    assert checklist_condition_series(release_id, contaminated_snap, sel) == prefix_scores
    assert checklist_independence(release_id, contaminated_snap, sel) == checklist_independence(
        release_id, prefix_snap, sel
    )


def test_checklist_item_backward_compatibility_and_strategy_card_delegation() -> None:
    """ChecklistItem giữ tương thích ngược khi khởi tạo 6 đối số; hỗ trợ cả Strategy Card."""
    legacy_item = ChecklistItem("adx", "ADX14 1h đủ mạnh", "22.5", ">=", "20", True)
    assert legacy_item.independence_summary is None
    assert legacy_item.as_dict() == {
        "key": "adx",
        "label_vi": "ADX14 1h đủ mạnh",
        "actual": "22.5",
        "operator": ">=",
        "threshold": "20",
        "passed": True,
    }

    enriched_item = replace(legacy_item, independence_summary="3/3 điều kiện độc lập")
    assert enriched_item.as_dict()["independence_summary"] == "3/3 điều kiện độc lập"

    card = build_default_demo_cards()[0]
    card_rel = strategy_card_release_id(card)
    bars_1h, _ = _multi_regime_bars(120, "1h")
    snap_card = _snapshot(
        (MarketSeries("1h", bars_1h),),
        (_feature("atr_14", "1h", 1.5, bars_1h[-1].available_at),),
    )
    ind_card = checklist_independence(card_rel, snap_card, _selection(card_rel))
    assert ind_card.status == "ok"
    assert ind_card.independent_conditions == ind_card.total_conditions == 2
    assert ind_card.summary_vi == "2/2 điều kiện độc lập"
    assert ind_card.as_dict()["summary_vi"] == "2/2 điều kiện độc lập"


def test_h03_custom_parameters_affect_condition_series() -> None:
    """Tham số runtime donchian_lookback / volume_median_lookback được phản ánh đúng."""
    snap = _h03_rich_snapshot(120, "1h")
    default_series = checklist_condition_series("h03-v1", snap, _selection("h03-v1"))
    custom_series = checklist_condition_series(
        "h03-v1",
        snap,
        _selection("h03-v1", donchian_lookback=20, volume_median_lookback=10),
    )
    assert len(default_series["donchian_breakout"]) == 120
    assert len(custom_series["donchian_breakout"]) == 120
    assert default_series["donchian_breakout"] != custom_series["donchian_breakout"]
    assert default_series["volume_above_median"] != custom_series["volume_above_median"]


@pytest.mark.asyncio
async def test_bot_dispatcher_persists_checklist_independence_and_roundtrips_hash() -> None:
    """BotDecision lưu được independence_summary vào payload canonical và kiểm băm hash đạt."""
    from tests.unit.test_bot_dispatcher import BOT_A, FixtureSnapshots, _context, _evidence

    from trading_bot.application.bot_dispatcher import (
        BotDecision,
        BotDecisionJob,
        BotDecisionStatus,
        evaluate_bot_decision,
    )
    from trading_bot.domain.strategy.registry import StrategyRegistry

    snap = _h03_rich_snapshot(160, "1h")
    ev = _evidence(
        observed_at=snap.decision_time - timedelta(minutes=1),
        available_at=snap.decision_time,
        validity_end=snap.decision_time + timedelta(hours=1),
    )
    context = _context(strategy_release_id="h03-v1")
    job = BotDecisionJob.create(evidence=ev, context=context, evaluated_at=ev.available_at)
    decision = await evaluate_bot_decision(
        evidence=ev,
        job=job,
        strategies=StrategyRegistry(),
        snapshots=FixtureSnapshots({BOT_A: snap}),
    )
    assert decision.status is BotDecisionStatus.CANDIDATE
    payload = decision.canonical_payload()
    items = payload["checklist"]["items"]
    assert len(items) == 2
    assert all(item["independence_summary"] == "2/2 điều kiện độc lập" for item in items)
    assert BotDecision.from_payload(payload, decision.decision_hash) == decision
