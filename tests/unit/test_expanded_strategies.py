"""Bộ kiểm thử TDD (RED → GREEN) cho 6 chiến lược định lượng mở rộng (`expanded_rules.py`).

Bao gồm cả truyền thống (TSMOM H02, Bollinger–Keltner Squeeze H04, Trend Pullback H05,
MACD+ADX H21) và nâng cao cho Binance USDⓈ-M Perpetual Futures (Intraday Session VWAP H07,
Taker Order-Flow Imbalance Breakout H22).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from tests.unit.test_risk_engine import _settings as _planner_settings

from trading_bot.application.decision_snapshot_builder import (
    build_decision_snapshot_from_bars,
)
from trading_bot.application.learning_feature_vector import (
    _unit_for_feature,
    build_learning_feature_vector,
)
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.application.strategy_market_requirements import (
    ENTRY_TIMEFRAME_BY_RELEASE,
    FEATURES_BY_RELEASE,
    validate_requirements,
)
from trading_bot.domain.market.models import MarketBarSourceIdentity, OHLCVBar
from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    CostModel,
    TradePlan,
    TradePlanner,
)
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    FeatureObservation,
    MarketSeries,
    NoTrade,
    SeriesQuality,
    StrategyCandidate,
    StrategySelection,
)
from trading_bot.domain.strategy.registry import StrategyRegistry
from trading_bot.domain.strategy.setup_progress import SETUP_RULES

START = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

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
    taker_buy_base: float | None = 50.0,
    pinned_identity: bool = False,
) -> OHLCVBar:
    duration = INTERVALS[timeframe]
    opened = START + index * duration
    closed = opened + duration - timedelta(microseconds=1)
    c = Decimal(str(round(close, 4)))
    o = Decimal(str(round(close if open_price is None else open_price, 4)))
    h = Decimal(str(round(max(close, float(o)) + 1.0 if high is None else high, 4)))
    low_val = Decimal(str(round(min(close, float(o)) - 1.0 if low is None else low, 4)))
    vol = Decimal(str(volume))
    qvol = Decimal(str(round(volume * close if quote_volume is None else quote_volume, 4)))
    tbuy = None if taker_buy_base is None else Decimal(str(taker_buy_base))
    chk = f"{(index + 1):064x}"
    source_id = (
        MarketBarSourceIdentity(
            workspace_id=WORKSPACE_ID,
            record_id=UUID(int=10_000 + index + (1000 if timeframe == "4h" else 0)),
            logical_key=f"BTCUSDT:{timeframe}:{int(opened.timestamp())}",
            revision=1,
            supersedes_record_id=None,
            checksum=chk,
        )
        if pinned_identity
        else None
    )
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe=timeframe,
        open_time=opened,
        close_time=closed,
        event_at=closed,
        received_at=closed,
        available_at=closed,
        open=o,
        high=h,
        low=low_val,
        close=c,
        volume=vol,
        quote_volume=qvol,
        taker_buy_base_volume=tbuy,
        is_closed=True,
        checksum=chk if pinned_identity else None,
        source_identity=source_id,
    )


def _feature(
    name: str,
    timeframe: str,
    value: float | None,
    at: datetime,
    *,
    previous: float | None = None,
) -> FeatureObservation:
    return FeatureObservation(
        feature_id=f"{timeframe}:{name}:{int(at.timestamp())}",
        name=name,
        timeframe=timeframe,
        value=value,
        previous_value=value if previous is None else previous,
        available_at=at,
    )


def _snapshot(
    series: tuple[MarketSeries, ...],
    features: tuple[FeatureObservation, ...],
    *,
    position_is_flat: bool | None = True,
    macro_blackout: bool | None = False,
) -> DecisionSnapshot:
    latest = max(bar.available_at for item in series for bar in item.bars)
    qualified = tuple(
        MarketSeries(
            timeframe=item.timeframe,
            bars=item.bars,
            quote_volumes=item.quote_volumes,
            quality=SeriesQuality.READY,
            expected_interval=INTERVALS[item.timeframe],
            max_event_age=2 * INTERVALS[item.timeframe],
            backfill_complete=True,
        )
        for item in series
    )
    return DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=latest,
        snapshot_id="snap-expanded-test",
        series=qualified,
        features=features,
        position_is_flat=position_is_flat,
        macro_blackout=macro_blackout,
        macro_evidence_ids=("macro-1",),
        macro_evidence_available_at=latest,
        macro_is_fresh=True,
    )


def _selection(release_id: str) -> StrategySelection:
    return StrategySelection(
        settings_version_id="settings-expanded-1",
        active_release_ids=(release_id,),
        published=True,
    )


# ---------------------------------------------------------------------------
# 1. Registry, Market Requirements & Hash Invariants
# ---------------------------------------------------------------------------
def test_expanded_releases_registered_without_polluting_bare_registry() -> None:
    bare_ids = [r.release_id for r in StrategyRegistry().list_releases()]
    assert bare_ids == ["h01-v1", "h03-v1", "h03-v2", "h06-v1", "h06-v2"]

    product_registry = strategy_registry()
    product_ids = {r.release_id for r in product_registry.list_releases()}
    for rel in ("h02-v1", "h04-v1", "h05-v1", "h07-v1", "h21-v1", "h22-v1"):
        assert rel in product_ids
        meta = product_registry.get_release(rel)
        assert meta.status == "EXPERIMENTAL_UNVALIDATED"
        assert len(meta.content_hash) == 64

    validate_requirements(FEATURES_BY_RELEASE)
    for rel, expected_tf in (
        ("h02-v1", "4h"),
        ("h04-v1", "15m"),
        ("h05-v1", "1h"),
        ("h07-v1", "5m"),
        ("h21-v1", "1h"),
        ("h22-v1", "15m"),
    ):
        assert ENTRY_TIMEFRAME_BY_RELEASE[rel] == expected_tf
        assert expected_tf in FEATURES_BY_RELEASE[rel]
        for tf_features in FEATURES_BY_RELEASE[rel].values():
            assert all(_unit_for_feature(name) is not None for name in tf_features)

    for strategy_id in ("H02", "H04", "H05", "H07", "H21", "H22"):
        assert strategy_id in SETUP_RULES


# ---------------------------------------------------------------------------
# 2. H02 — Multi-Horizon Time-Series Momentum
# ---------------------------------------------------------------------------
def test_h02_time_series_momentum_warmup_volatility_gate_and_direction() -> None:
    rule = strategy_registry().resolve("h02-v1", _selection("h02-v1"))

    # Chưa đủ 253 nến 4h -> WARMUP_INCOMPLETE
    short_bars = tuple(_bar(i, "4h", 100.0 + i * 0.1) for i in range(120))
    short_snap = _snapshot(
        (MarketSeries("4h", short_bars),),
        (
            _feature("atr_14", "4h", 1.2, short_bars[-1].available_at),
            _feature("realized_volatility_20", "4h", 0.015, short_bars[-1].available_at),
        ),
    )
    res_warmup = rule.evaluate(short_snap, _selection("h02-v1"))
    assert isinstance(res_warmup, NoTrade)
    assert res_warmup.code == "WARMUP_INCOMPLETE"

    # Đủ 260 nến tăng đều -> LONG với stop 3 ATR, trail 4 ATR, max 252
    up_bars = tuple(_bar(i, "4h", 100.0 + i * 0.35) for i in range(260))
    last_at = up_bars[-1].available_at
    up_snap = _snapshot(
        (MarketSeries("4h", up_bars),),
        (
            _feature("atr_14", "4h", 1.5, last_at),
            _feature("realized_volatility_20", "4h", 0.012, last_at),
        ),
    )
    res_long = rule.evaluate(up_snap, _selection("h02-v1"))
    assert isinstance(res_long, StrategyCandidate)
    assert res_long.direction == "long"
    assert res_long.recipe.stop_atr_multiple == 3.0
    assert res_long.recipe.trailing_atr_multiple == 4.0
    assert res_long.recipe.max_hold_bars == 252

    # Đủ 260 nến giảm đều -> SHORT
    down_bars = tuple(_bar(i, "4h", 300.0 - i * 0.4) for i in range(260))
    down_snap = _snapshot(
        (MarketSeries("4h", down_bars),),
        (
            _feature("atr_14", "4h", 1.8, down_bars[-1].available_at),
            _feature("realized_volatility_20", "4h", 0.014, down_bars[-1].available_at),
        ),
    )
    res_short = rule.evaluate(down_snap, _selection("h02-v1"))
    assert isinstance(res_short, StrategyCandidate)
    assert res_short.direction == "short"

    # RV quá thấp (thị trường phẳng giả tạo) hoặc quá cao (crash) -> VOLATILITY_OUT_OF_RANGE
    extreme_rv_snap = _snapshot(
        (MarketSeries("4h", up_bars),),
        (
            _feature("atr_14", "4h", 1.5, last_at),
            _feature("realized_volatility_20", "4h", 0.12, last_at),
        ),
    )
    res_rv = rule.evaluate(extreme_rv_snap, _selection("h02-v1"))
    assert isinstance(res_rv, NoTrade)
    assert res_rv.code == "VOLATILITY_OUT_OF_RANGE"


# ---------------------------------------------------------------------------
# 3. H04 — Bollinger-Keltner Volatility Squeeze Breakout (No Look-Ahead)
# ---------------------------------------------------------------------------
def test_h04_squeeze_breakout_requires_prior_compression_volume_and_no_lookahead() -> None:
    rule = strategy_registry().resolve("h04-v1", _selection("h04-v1"))

    # Dựng 100 nến: 85 nến đầu dao động vừa (~2.0), sau đó nén chặt 14 nến cuối quanh 100.0±0.15,
    # nến cuối bứt phá mạnh lên 103.5 với khối lượng > 1.5× trung vị!
    bars = []
    for i in range(85):
        c = 100.0 + math.sin(i * 0.4) * 2.5
        bars.append(
            _bar(i, "15m", c, high=c + 1.5, low=c - 1.5, volume=100.0, quote_volume=10_000.0)
        )
    for i in range(85, 99):
        c = 100.0 + (0.05 if i % 2 == 0 else -0.05)
        bars.append(
            _bar(i, "15m", c, high=c + 0.12, low=c - 0.12, volume=90.0, quote_volume=9_000.0)
        )
    breakout_bar = _bar(
        99,
        "15m",
        104.5,
        open_price=100.1,
        high=104.8,
        low=100.0,
        volume=220.0,
        quote_volume=23_000.0,
    )
    bars.append(breakout_bar)
    qvols = tuple(float(b.quote_volume or 0) for b in bars)
    last_at = breakout_bar.available_at
    snap = _snapshot(
        (MarketSeries("15m", tuple(bars), quote_volumes=qvols),),
        (_feature("atr_14", "15m", 0.6, last_at),),
    )

    res = rule.evaluate(snap, _selection("h04-v1"))
    assert isinstance(res, StrategyCandidate)
    assert res.direction == "long"
    assert res.recipe.stop_atr_multiple == 1.5
    assert res.recipe.take_profit_atr_multiple == 3.0
    assert res.recipe.max_hold_bars == 32

    # Point-in-time: nến cuối nếu volume thấp không vượt 1.5× median -> từ chối
    low_vol_bars = bars[:-1] + [
        _bar(
            99,
            "15m",
            104.5,
            open_price=100.1,
            high=104.8,
            low=100.0,
            volume=80.0,
            quote_volume=8_000.0,
        )
    ]
    low_vol_snap = _snapshot(
        (
            MarketSeries(
                "15m",
                tuple(low_vol_bars),
                quote_volumes=tuple(float(b.quote_volume or 0) for b in low_vol_bars),
            ),
        ),
        (_feature("atr_14", "15m", 0.6, last_at),),
    )
    assert isinstance(rule.evaluate(low_vol_snap, _selection("h04-v1")), NoTrade)

    # Đối xứng SHORT khi nén rồi sập thủng LL20(t-1) & BB lower
    short_break_bars = bars[:-1] + [
        _bar(
            99,
            "15m",
            95.5,
            open_price=99.9,
            high=100.0,
            low=95.2,
            volume=240.0,
            quote_volume=24_000.0,
        )
    ]
    short_snap = _snapshot(
        (
            MarketSeries(
                "15m",
                tuple(short_break_bars),
                quote_volumes=tuple(float(b.quote_volume or 0) for b in short_break_bars),
            ),
        ),
        (_feature("atr_14", "15m", 0.6, last_at),),
    )
    res_short = rule.evaluate(short_snap, _selection("h04-v1"))
    assert isinstance(res_short, StrategyCandidate)
    assert res_short.direction == "short"


# ---------------------------------------------------------------------------
# 4. H05 — EMA Trend Pullback + RSI Regime Continuation
# ---------------------------------------------------------------------------
def test_h05_trend_pullback_requires_htf_ema_slope_and_rsi_band() -> None:
    rule = strategy_registry().resolve("h05-v1", _selection("h05-v1"))

    # Dựng chuỗi 1h tăng đều rồi điều chỉnh nhẹ ở 2 nến trước và rút chân giữ trên EMA20
    bars_1h = [_bar(i, "1h", 100.0 + i * 0.5) for i in range(95)]
    # Hạ nhẹ vài nến để tạo RSI vào [40, 60]
    bars_1h.extend(
        [
            _bar(95, "1h", 145.5, open_price=147.0, high=147.5, low=145.0),
            _bar(96, "1h", 144.0, open_price=145.5, high=145.8, low=143.5),
            _bar(97, "1h", 143.2, open_price=144.0, high=144.2, low=142.8),
            _bar(98, "1h", 142.8, open_price=143.2, high=143.5, low=142.4),
            # Nến tín hiệu nhúng chạm EMA20 nhưng đóng cửa trên EMA20 & close > open
            _bar(99, "1h", 144.2, open_price=142.6, high=144.5, low=141.5),
        ]
    )
    bars_4h = tuple(_bar(i, "4h", 100.0 + i * 2.0) for i in range(210))
    last_at = bars_1h[-1].available_at

    # Đặt FeatureObservations chuẩn cho pullback tăng
    feats = (
        _feature("atr_14", "1h", 1.8, last_at),
        _feature("ema_20", "1h", 143.5, last_at, previous=143.6),
        _feature("ema_80", "1h", 136.0, last_at, previous=135.7),
        _feature("rsi_14", "1h", 52.0, last_at),
        _feature("adx_14", "1h", 24.0, last_at),
        _feature("ema_200", "4h", 350.0, bars_4h[-1].available_at),
    )
    snap = _snapshot(
        (MarketSeries("1h", tuple(bars_1h)), MarketSeries("4h", bars_4h)),
        feats,
    )
    res = rule.evaluate(snap, _selection("h05-v1"))
    assert isinstance(res, StrategyCandidate)
    assert res.direction == "long"
    assert res.recipe.stop_atr_multiple == 2.0
    assert res.recipe.take_profit_atr_multiple == 3.0
    assert res.recipe.max_hold_bars == 48

    # Nếu 4h ngược xu hướng (C_4h < EMA200_4h) -> chặn LONG
    htf_bear_feats = feats[:-1] + (_feature("ema_200", "4h", 999.0, bars_4h[-1].available_at),)
    snap_htf_bear = _snapshot(
        (MarketSeries("1h", tuple(bars_1h)), MarketSeries("4h", bars_4h)),
        htf_bear_feats,
    )
    assert isinstance(rule.evaluate(snap_htf_bear, _selection("h05-v1")), NoTrade)


# ---------------------------------------------------------------------------
# 5. H07 — Intraday Session VWAP Reversion
# ---------------------------------------------------------------------------
def test_h07_session_vwap_reversion_respects_adx_and_3x_cost_hurdle() -> None:
    rule = strategy_registry().resolve("h07-v1", _selection("h07-v1"))

    # Tạo 15 nến M5 trong cùng ngày UTC (2026-09-01 00:00..01:10):
    bars = [_bar(i, "5m", 100.0) for i in range(13)]
    bars.append(_bar(13, "5m", 97.2, open_price=98.5, high=98.8, low=97.0))
    bars.append(_bar(14, "5m", 97.8, open_price=97.2, high=98.1, low=97.1))
    last_at = bars[-1].available_at

    # VWAP = 100.0, ATR = 0.8 => dev = -2.75 <= -2.0; gap = 2.2/97.8 = 2.25% >> 0.3%
    snap = _snapshot(
        (MarketSeries("5m", tuple(bars)),),
        (
            _feature("session_vwap", "5m", 100.0, last_at),
            _feature("atr_14", "5m", 0.8, last_at),
            _feature("adx_14", "5m", 15.0, last_at),
            _feature("efficiency_ratio_40", "5m", 0.18, last_at),
        ),
    )
    res_long = rule.evaluate(snap, _selection("h07-v1"))
    assert isinstance(res_long, StrategyCandidate)
    assert res_long.direction == "long"

    # Khoảng về VWAP < 0.3% (không đủ 3x phí khứ hồi VIP0) -> COST_HURDLE_NOT_MET
    tight_snap = _snapshot(
        (MarketSeries("5m", tuple(bars)),),
        (
            _feature("session_vwap", "5m", 97.9, last_at),
            _feature(
                "atr_14", "5m", 0.04, last_at
            ),  # dev = -2.5 nhưng gap = 0.1/97.8 = 0.1% < 0.3%!
            _feature("adx_14", "5m", 14.0, last_at),
            _feature("efficiency_ratio_40", "5m", 0.15, last_at),
        ),
    )
    res_cost = rule.evaluate(tight_snap, _selection("h07-v1"))
    assert isinstance(res_cost, NoTrade)
    assert res_cost.code == "COST_HURDLE_NOT_MET"

    # ADX >= 20 -> REGIME_NOT_RANGING
    trend_snap = _snapshot(
        (MarketSeries("5m", tuple(bars)),),
        (
            _feature("session_vwap", "5m", 100.0, last_at),
            _feature("atr_14", "5m", 0.8, last_at),
            _feature("adx_14", "5m", 24.0, last_at),
            _feature("efficiency_ratio_40", "5m", 0.18, last_at),
        ),
    )
    res_adx = rule.evaluate(trend_snap, _selection("h07-v1"))
    assert isinstance(res_adx, NoTrade)
    assert res_adx.code == "REGIME_NOT_RANGING"


# ---------------------------------------------------------------------------
# 6. H21 — MACD Histogram + ADX Regime Confirmation Momentum
# ---------------------------------------------------------------------------
def test_h21_macd_adx_momentum_detects_histogram_cross_and_filters_low_adx() -> None:
    rule = strategy_registry().resolve("h21-v1", _selection("h21-v1"))

    # Chuỗi 1h giảm nhẹ tạo histogram âm rồi hồi mạnh ở nến cuối đẩy histogram cắt lên > 0
    prices = [110.0 - i * 0.15 for i in range(40)]
    prices[-2:] = [104.0, 108.5]
    bars_1h = tuple(_bar(i, "1h", p) for i, p in enumerate(prices))
    bars_4h = tuple(_bar(i, "4h", 100.0 + i * 0.5) for i in range(30))
    last_at = bars_1h[-1].available_at

    snap_pass = _snapshot(
        (MarketSeries("1h", bars_1h), MarketSeries("4h", bars_4h)),
        (
            _feature("atr_14", "1h", 1.2, last_at),
            _feature("adx_14", "1h", 26.0, last_at),
            _feature("plus_di_14", "1h", 28.0, last_at),
            _feature("minus_di_14", "1h", 14.0, last_at),
            _feature("ema_200", "4h", 95.0, bars_4h[-1].available_at),
        ),
    )
    res = rule.evaluate(snap_pass, _selection("h21-v1"))
    assert isinstance(res, StrategyCandidate)
    assert res.direction == "long"

    # ADX < 22 -> chặn MACD whipsaw
    snap_chop = _snapshot(
        (MarketSeries("1h", bars_1h), MarketSeries("4h", bars_4h)),
        (
            _feature("atr_14", "1h", 1.2, last_at),
            _feature("adx_14", "1h", 16.0, last_at),
            _feature("plus_di_14", "1h", 28.0, last_at),
            _feature("minus_di_14", "1h", 14.0, last_at),
            _feature("ema_200", "4h", 95.0, bars_4h[-1].available_at),
        ),
    )
    assert isinstance(rule.evaluate(snap_chop, _selection("h21-v1")), NoTrade)


# ---------------------------------------------------------------------------
# 7. H22 — Taker Order-Flow Imbalance & Liquidity Breakout
# ---------------------------------------------------------------------------
def test_h22_taker_flow_breakout_validates_aggressor_flow_and_fail_closes() -> None:
    rule = strategy_registry().resolve("h22-v1", _selection("h22-v1"))

    # 22 nến 15m tích lũy quanh 100; nến cuối phá đỉnh kèm Taker Buy 80% (TI = +0.60)
    bars_15m = [
        _bar(i, "15m", 100.0, high=100.5, low=99.5, volume=100.0, taker_buy_base=52.0)
        for i in range(21)
    ]
    bars_15m.append(
        _bar(
            21,
            "15m",
            102.0,
            open_price=100.2,
            high=102.3,
            low=100.0,
            volume=220.0,
            taker_buy_base=176.0,
        )
    )
    bars_1h = tuple(_bar(i, "1h", 102.0) for i in range(10))
    last_at = bars_15m[-1].available_at

    snap_ok = _snapshot(
        (MarketSeries("15m", tuple(bars_15m)), MarketSeries("1h", bars_1h)),
        (
            _feature("atr_14", "15m", 0.9, last_at),
            _feature("adx_14", "15m", 23.0, last_at),
            _feature("ema_80", "1h", 98.0, bars_1h[-1].available_at),
            _feature("atr_14", "1h", 1.8, bars_1h[-1].available_at),
        ),
    )
    res_long = rule.evaluate(snap_ok, _selection("h22-v1"))
    assert isinstance(res_long, StrategyCandidate)
    assert res_long.direction == "long"

    # Nếu giá tăng phá đỉnh nhưng bị bán chủ động hấp thụ (TI = -0.50) -> từ chối fakeout!
    bars_fakeout = bars_15m[:-1] + [
        _bar(
            21,
            "15m",
            102.0,
            open_price=100.2,
            high=102.3,
            low=100.0,
            volume=220.0,
            taker_buy_base=55.0,
        )
    ]
    snap_fakeout = _snapshot(
        (MarketSeries("15m", tuple(bars_fakeout)), MarketSeries("1h", bars_1h)),
        snap_ok.features,
    )
    assert isinstance(rule.evaluate(snap_fakeout, _selection("h22-v1")), NoTrade)

    # Thiếu taker_buy_base_volume -> fail-closed TAKER_FLOW_UNAVAILABLE (không ước lượng)
    bars_missing_taker = bars_15m[:-1] + [
        _bar(
            21,
            "15m",
            102.0,
            open_price=100.2,
            high=102.3,
            low=100.0,
            volume=220.0,
            taker_buy_base=None,
        )
    ]
    snap_missing = _snapshot(
        (MarketSeries("15m", tuple(bars_missing_taker)), MarketSeries("1h", bars_1h)),
        snap_ok.features,
    )
    res_missing = rule.evaluate(snap_missing, _selection("h22-v1"))
    assert isinstance(res_missing, NoTrade)
    assert res_missing.code == "TAKER_FLOW_UNAVAILABLE"


# ---------------------------------------------------------------------------
# 8. End-to-End DecisionSnapshotBuilder + LearningVector + TradePlanner
# ---------------------------------------------------------------------------
def test_expanded_rule_integrates_end_to_end_with_snapshot_builder_and_trade_planner() -> None:
    # Sinh chuỗi đủ 205 nến warmup cho cả 15m và 1h tới tận decision_time (EMA200 warmup)
    origin_1h = START - timedelta(hours=210)
    bars_1h_list: list[OHLCVBar] = []
    for i in range(270):
        opened = origin_1h + i * timedelta(hours=1)
        closed = opened + timedelta(hours=1) - timedelta(microseconds=1)
        c = Decimal(str(round(90.0 + i * 0.15, 4)))
        chk = f"{(i + 5000):064x}"
        bars_1h_list.append(
            OHLCVBar(
                symbol="BTCUSDT",
                timeframe="1h",
                open_time=opened,
                close_time=closed,
                event_at=closed,
                received_at=closed,
                available_at=closed,
                open=c,
                high=c + Decimal("1.0"),
                low=c - Decimal("1.0"),
                close=c,
                volume=Decimal("200"),
                quote_volume=c * Decimal("200"),
                taker_buy_base_volume=Decimal("110"),
                is_closed=True,
                checksum=chk,
                source_identity=MarketBarSourceIdentity(
                    workspace_id=WORKSPACE_ID,
                    record_id=UUID(int=20_000 + i),
                    logical_key=f"BTCUSDT:1h:{int(opened.timestamp())}",
                    revision=1,
                    supersedes_record_id=None,
                    checksum=chk,
                ),
            )
        )

    bars_15m = [
        _bar(
            i,
            "15m",
            100.0 + (i % 3) * 0.1,
            high=100.6,
            low=99.4,
            volume=100.0,
            taker_buy_base=52.0,
            pinned_identity=True,
        )
        for i in range(204)
    ]
    for offset in range(15):
        idx = 189 + offset
        bars_15m[idx] = _bar(
            idx,
            "15m",
            100.0 + offset * 0.25,
            high=100.5 + offset * 0.25,
            low=99.7 + offset * 0.25,
            volume=150.0,
            taker_buy_base=95.0,
            pinned_identity=True,
        )
    bars_15m.append(
        _bar(
            204,
            "15m",
            106.5,
            open_price=103.5,
            high=106.8,
            low=103.4,
            volume=300.0,
            taker_buy_base=240.0,
            pinned_identity=True,
        )
    )

    decision_time = bars_15m[-1].available_at
    bars_1h = tuple(b for b in bars_1h_list if b.available_at <= decision_time)
    snapshot = build_decision_snapshot_from_bars(
        workspace_id=WORKSPACE_ID,
        symbol="BTCUSDT",
        strategy_release_id="h22-v1",
        decision_time=decision_time,
        series_quality_as_of=decision_time,
        bars_by_timeframe={"15m": tuple(bars_15m), "1h": bars_1h},
        position_is_flat=True,
        source_identity_required=True,
    )
    release = strategy_registry().get_release("h22-v1")
    rule = strategy_registry().resolve("h22-v1", _selection("h22-v1"))
    candidate = rule.evaluate(snapshot, _selection("h22-v1"))
    assert isinstance(candidate, StrategyCandidate), candidate

    # Kiểm định LearningFeatureVector từ chính snapshot
    vec = build_learning_feature_vector(
        snapshot,
        strategy_release_id="h22-v1",
        strategy_release_content_hash=release.content_hash,
    )
    assert len(vec.features) > 0

    # Đưa qua TradePlanner tạo TradePlan thật
    from dataclasses import replace

    base_settings = _planner_settings()
    settings_for_plan = replace(
        base_settings,
        workspace_id=str(WORKSPACE_ID),
        settings_version_id="settings-expanded-1",
        settings_hash="a" * 64,
        active_release_ids=("h22-v1",),
        release_content_hashes=(("h22-v1", release.content_hash),),
    )
    planner = TradePlanner()
    cost_model = CostModel(
        version_id="vip0-test",
        entry_fee_rate=Decimal("0.0005"),
        exit_fee_rate=Decimal("0.0005"),
        adverse_funding_per_unit=Decimal("0"),
        entry_slippage_rate=Decimal("0.0002"),
        exit_slippage_rate=Decimal("0.0002"),
    )
    plan = planner.plan(
        candidate,
        settings_for_plan,
        AccountSnapshot(
            workspace_id=str(WORKSPACE_ID),
            account_id="paper-1",
            settings_version_id="settings-expanded-1",
            captured_at=decision_time - timedelta(seconds=1),
            equity=Decimal("10000"),
            available_margin=Decimal("5000"),
            is_known=True,
        ),
        (),
        cost_model,
        now=decision_time + timedelta(seconds=1),
    )
    assert isinstance(plan, TradePlan), plan
    assert plan.direction == "long"


def test_all_expanded_releases_and_cards_pass_backtest_job_submission_validation() -> None:
    from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission

    all_new_releases = (
        "h02-v1",
        "h04-v1",
        "h05-v1",
        "h07-v1",
        "h21-v1",
        "h22-v1",
        "sc-binance-usdm-vol-breakout-001-v1.0.0",
        "sc-binance-usdm-mean-revert-002-v1.2.1",
        "sc-binance-usdm-tsmom-003-v1.0.0",
        "sc-binance-usdm-bbkc-squeeze-004-v1.0.0",
        "sc-binance-usdm-trend-pullback-005-v1.0.0",
        "sc-binance-usdm-vwap-revert-006-v1.0.0",
        "sc-binance-usdm-macd-adx-007-v1.0.0",
        "sc-binance-usdm-taker-flow-008-v1.0.0",
    )
    for rel in all_new_releases:
        interval = ENTRY_TIMEFRAME_BY_RELEASE[rel]
        sub = BacktestReplayJobSubmission(
            bundle_hash="b" * 64,
            strategy_release_id=rel,
            execution_interval=interval,
            starting_equity=Decimal("10000"),
            per_trade_risk_fraction=Decimal("0.01"),
            fee_rate=Decimal("0.0005"),
            slippage_rate=Decimal("0.0002"),
            seed=42,
        )
        assert sub.strategy_release_id == rel
        assert sub.execution_interval == interval


def test_expanded_short_signals_and_h04_strict_no_lookahead() -> None:
    registry = strategy_registry()

    # H05 SHORT pullback
    h05 = registry.resolve("h05-v1", _selection("h05-v1"))
    bars_1h = [_bar(i, "1h", 200.0 - i * 0.5) for i in range(96)]
    bars_1h.append(_bar(96, "1h", 152.0, open_price=153.5, high=154.0, low=151.8))
    bars_4h = tuple(_bar(i, "4h", 300.0 - i * 1.0) for i in range(30))
    at = bars_1h[-1].available_at
    snap_h05_short = _snapshot(
        (MarketSeries("1h", tuple(bars_1h)), MarketSeries("4h", bars_4h)),
        (
            _feature("atr_14", "1h", 1.5, at),
            _feature("ema_20", "1h", 153.2, at),
            _feature("ema_80", "1h", 160.0, at, previous=160.4),
            _feature("rsi_14", "1h", 48.0, at),
            _feature("adx_14", "1h", 25.0, at),
            _feature("ema_200", "4h", 350.0, bars_4h[-1].available_at),
        ),
    )
    res_h05 = h05.evaluate(snap_h05_short, _selection("h05-v1"))
    assert isinstance(res_h05, StrategyCandidate)
    assert res_h05.direction == "short"

    # H07 SHORT overextension above VWAP
    h07 = registry.resolve("h07-v1", _selection("h07-v1"))
    bars_5m = [_bar(i, "5m", 100.0) for i in range(13)] + [
        _bar(13, "5m", 103.0, open_price=101.5, high=103.3, low=101.4),
        _bar(14, "5m", 102.2, open_price=103.0, high=103.1, low=102.0),
    ]
    at5 = bars_5m[-1].available_at
    res_h07 = h07.evaluate(
        _snapshot(
            (MarketSeries("5m", tuple(bars_5m)),),
            (
                _feature("session_vwap", "5m", 100.0, at5),
                _feature("atr_14", "5m", 0.8, at5),
                _feature("adx_14", "5m", 14.0, at5),
                _feature("efficiency_ratio_40", "5m", 0.15, at5),
            ),
        ),
        _selection("h07-v1"),
    )
    assert isinstance(res_h07, StrategyCandidate)
    assert res_h07.direction == "short"

    # H21 SHORT histogram cross down + DI- > DI+ + 4h < EMA200
    h21 = registry.resolve("h21-v1", _selection("h21-v1"))
    up_then_drop = [100.0 + i * 0.15 for i in range(40)]
    up_then_drop[-2:] = [106.0, 100.5]
    bars_h21 = tuple(_bar(i, "1h", p) for i, p in enumerate(up_then_drop))
    at21 = bars_h21[-1].available_at
    res_h21 = h21.evaluate(
        _snapshot(
            (MarketSeries("1h", bars_h21), MarketSeries("4h", bars_4h)),
            (
                _feature("atr_14", "1h", 1.2, at21),
                _feature("adx_14", "1h", 27.0, at21),
                _feature("plus_di_14", "1h", 12.0, at21),
                _feature("minus_di_14", "1h", 29.0, at21),
                _feature("ema_200", "4h", 350.0, bars_4h[-1].available_at),
            ),
        ),
        _selection("h21-v1"),
    )
    assert isinstance(res_h21, StrategyCandidate)
    assert res_h21.direction == "short"

    # H22 SHORT breakdown with negative taker flow + 1h below EMA80
    h22 = registry.resolve("h22-v1", _selection("h22-v1"))
    bars_22 = [
        _bar(i, "15m", 100.0, high=100.5, low=99.5, volume=100.0, taker_buy_base=46.0)
        for i in range(21)
    ] + [
        _bar(
            21, "15m", 97.5, open_price=99.8, high=99.9, low=97.2, volume=240.0, taker_buy_base=40.0
        )
    ]
    bars_1h_bear = tuple(_bar(i, "1h", 95.0) for i in range(5))
    at22 = bars_22[-1].available_at
    res_h22 = h22.evaluate(
        _snapshot(
            (MarketSeries("15m", tuple(bars_22)), MarketSeries("1h", bars_1h_bear)),
            (
                _feature("atr_14", "15m", 0.9, at22),
                _feature("adx_14", "15m", 24.0, at22),
                _feature("ema_80", "1h", 102.0, bars_1h_bear[-1].available_at),
                _feature("atr_14", "1h", 1.8, bars_1h_bear[-1].available_at),
            ),
        ),
        _selection("h22-v1"),
    )
    assert isinstance(res_h22, StrategyCandidate)
    assert res_h22.direction == "short"

    # No-lookahead H04: nếu đỉnh ở t rất cao (high=120) nhưng close=104.5 vẫn vượt HH20(t-1),
    # nến t KHÔNG làm tăng HH20(t-1) khiến chính mình bị loại!
    h04 = registry.resolve("h04-v1", _selection("h04-v1"))
    sq_bars = []
    for i in range(85):
        c = 100.0 + math.sin(i * 0.4) * 2.5
        sq_bars.append(
            _bar(i, "15m", c, high=c + 1.5, low=c - 1.5, volume=100.0, quote_volume=10_000.0)
        )
    for i in range(85, 99):
        sq_bars.append(
            _bar(i, "15m", 100.0, high=100.12, low=99.88, volume=90.0, quote_volume=9_000.0)
        )
    sq_bars.append(
        _bar(
            99,
            "15m",
            104.5,
            open_price=100.1,
            high=120.0,
            low=100.0,
            volume=220.0,
            quote_volume=23_000.0,
        )
    )
    sq_snap = _snapshot(
        (
            MarketSeries(
                "15m",
                tuple(sq_bars),
                quote_volumes=tuple(float(b.quote_volume or 0) for b in sq_bars),
            ),
        ),
        (_feature("atr_14", "15m", 0.6, sq_bars[-1].available_at),),
    )
    assert isinstance(h04.evaluate(sq_snap, _selection("h04-v1")), StrategyCandidate)


def test_expanded_strategies_and_cards_execute_through_real_backtest_replay_executor() -> None:
    import hashlib
    from uuid import uuid5

    from trading_bot.application.backtest_dataset_manifest import build_backtest_dataset_manifest
    from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayInput
    from trading_bot.application.backtest_dataset_replay_bundle import (
        build_backtest_dataset_replay_bundle,
    )
    from trading_bot.application.backtest_replay_executor import build_backtest_replay_plan
    from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission
    from trading_bot.application.binance_rest_backfill import (
        BackfillRightsEvidence,
        BackfillRightsPurpose,
        RestBackfillRequest,
    )

    origin = START - timedelta(hours=215)
    eval_start = START + timedelta(hours=49)
    range_end = START + timedelta(hours=54)

    bars_15m_raw: list[OHLCVBar] = []
    count_15m = int((range_end - origin) / INTERVALS["15m"])
    for i in range(count_15m):
        opened = origin + i * INTERVALS["15m"]
        closed = opened + INTERVALS["15m"] - timedelta(milliseconds=1)
        is_break = opened == START + timedelta(hours=51)
        is_tp = opened == START + timedelta(hours=51, minutes=30)
        is_pullback = opened == START + timedelta(hours=52)
        is_follow = opened > START + timedelta(hours=51) and not is_tp and not is_pullback
        base = 100.0 + (i % 3) * 0.05
        if START + timedelta(hours=47) <= opened < START + timedelta(hours=51):
            step_idx = int((opened - (START + timedelta(hours=47))) / INTERVALS["15m"])
            base = 100.0 + step_idx * 0.25
        if is_break:
            open_p, high_p, low_p, close, vol, tbuy = 104.2, 109.0, 104.0, 108.5, 300.0, 250.0
        elif is_tp:
            open_p, high_p, low_p, close, vol, tbuy = 112.0, 117.5, 111.5, 116.8, 120.0, 75.0
        elif is_pullback:
            open_p, high_p, low_p, close, vol, tbuy = 116.8, 117.0, 103.5, 104.0, 120.0, 40.0
        elif is_follow:
            open_p, high_p, low_p, close, vol, tbuy = 110.5, 112.5, 109.8, 111.8, 110.0, 65.0
        else:
            open_p, high_p, low_p, close, vol, tbuy = (
                base,
                base + 0.5,
                base - 0.5,
                base,
                110.0,
                65.0,
            )
        cursor = f"exp-15m:{i}"
        bars_15m_raw.append(
            OHLCVBar(
                symbol="BTCUSDT",
                timeframe="15m",
                open_time=opened,
                close_time=closed,
                event_at=closed,
                received_at=closed,
                available_at=closed,
                open=Decimal(str(round(open_p, 4))),
                high=Decimal(str(round(high_p, 4))),
                low=Decimal(str(round(low_p, 4))),
                close=Decimal(str(round(close, 4))),
                volume=Decimal(str(vol)),
                quote_volume=Decimal(str(round(vol * close, 4))),
                taker_buy_base_volume=Decimal(str(tbuy)),
                is_closed=True,
                source="BINANCE_USDM_REST_BACKFILL",
                source_cursor=cursor,
                checksum=hashlib.sha256(cursor.encode()).hexdigest(),
            )
        )

    bars_1h_raw: list[OHLCVBar] = []
    count_1h = int((range_end - origin) / INTERVALS["1h"])
    for i in range(count_1h):
        opened = origin + i * INTERVALS["1h"]
        closed = opened + INTERVALS["1h"] - timedelta(milliseconds=1)
        c = 90.0 + i * 0.15
        cursor = f"exp-1h:{i}"
        bars_1h_raw.append(
            OHLCVBar(
                symbol="BTCUSDT",
                timeframe="1h",
                open_time=opened,
                close_time=closed,
                event_at=closed,
                received_at=closed,
                available_at=closed,
                open=Decimal(str(round(c, 4))),
                high=Decimal(str(round(c + 1.0, 4))),
                low=Decimal(str(round(c - 1.0, 4))),
                close=Decimal(str(round(c, 4))),
                volume=Decimal("200"),
                quote_volume=Decimal(str(round(200 * c, 4))),
                taker_buy_base_volume=Decimal("120"),
                is_closed=True,
                source="BINANCE_USDM_REST_BACKFILL",
                source_cursor=cursor,
                checksum=hashlib.sha256(cursor.encode()).hexdigest(),
            )
        )

    rights = BackfillRightsEvidence(
        workspace_id=WORKSPACE_ID,
        provider="BINANCE_USDM_REST",
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        terms_url="https://example.invalid/synthetic-expanded-terms",
        policy_fingerprint=hashlib.sha256(b"expanded-replay-policy").hexdigest(),
        verified_at=origin - timedelta(days=1),
        checked_at=origin - timedelta(hours=1),
        valid_until=range_end + timedelta(days=1),
    )
    m15 = build_backtest_dataset_manifest(
        RestBackfillRequest(
            symbol="BTCUSDT",
            interval="15m",
            start=origin,
            end=range_end,
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        ),
        evaluation_start=eval_start,
        warmup_bars=int((eval_start - origin) / INTERVALS["15m"]),
        bars=tuple(bars_15m_raw),
        rights_evidence=rights,
        prepared_at=range_end,
        schema_version=3,
    )
    m1h = build_backtest_dataset_manifest(
        RestBackfillRequest(
            symbol="BTCUSDT",
            interval="1h",
            start=origin,
            end=range_end,
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        ),
        evaluation_start=eval_start,
        warmup_bars=int((eval_start - origin) / INTERVALS["1h"]),
        bars=tuple(bars_1h_raw),
        rights_evidence=rights,
        prepared_at=range_end,
        schema_version=3,
    )
    bundle = build_backtest_dataset_replay_bundle(
        (
            BacktestDatasetReplayInput(m15, tuple(bars_15m_raw), rights),
            BacktestDatasetReplayInput(m1h, tuple(bars_1h_raw), rights),
        )
    )

    for rel in (
        "h04-v1",
        "h22-v1",
        "sc-binance-usdm-mean-revert-002-v1.2.1",
        "sc-binance-usdm-bbkc-squeeze-004-v1.0.0",
        "sc-binance-usdm-vwap-revert-006-v1.0.0",
        "sc-binance-usdm-taker-flow-008-v1.0.0",
    ):
        sub = BacktestReplayJobSubmission(
            bundle_hash=bundle.dataset_hash,
            strategy_release_id=rel,
            execution_interval="15m",
            starting_equity=Decimal("10000"),
            per_trade_risk_fraction=Decimal("0.01"),
            fee_rate=Decimal("0.0005"),
            slippage_rate=Decimal("0.0002"),
            seed=11,
        )
        plan = build_backtest_replay_plan(WORKSPACE_ID, uuid5(WORKSPACE_ID, rel), bundle, sub)
        comparison = plan.executor(bundle, plan.manifest)
        assert len(comparison.runs) >= 1
        base_run = comparison.runs[0]
        assert len(base_run.sensitivity_run.result.report_hash) == 64
        assert base_run.sensitivity_run.result.observation_count >= 1
        assert base_run.sensitivity_run.result.usable_observation_count >= 1
        if rel in {"h22-v1", "sc-binance-usdm-taker-flow-008-v1.0.0"}:
            assert base_run.sensitivity_run.result.trade_count >= 1


def test_expanded_4h_1h_and_5m_releases_execute_through_real_backtest_replay_executor() -> None:
    import hashlib
    from uuid import uuid5

    from trading_bot.application.backtest_dataset_manifest import build_backtest_dataset_manifest
    from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayInput
    from trading_bot.application.backtest_dataset_replay_bundle import (
        build_backtest_dataset_replay_bundle,
    )
    from trading_bot.application.backtest_replay_executor import build_backtest_replay_plan
    from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission
    from trading_bot.application.binance_rest_backfill import (
        BackfillRightsEvidence,
        BackfillRightsPurpose,
        RestBackfillRequest,
    )

    origin_4h = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    eval_4h = origin_4h + timedelta(hours=260 * 4)
    end_4h = eval_4h + timedelta(hours=12)

    def _gen_bars(
        tf: str, step: timedelta, origin_dt: datetime, end_dt: datetime
    ) -> tuple[OHLCVBar, ...]:
        out: list[OHLCVBar] = []
        n = int((end_dt - origin_dt) / step)
        for i in range(n):
            opened = origin_dt + i * step
            closed = opened + step - timedelta(milliseconds=1)
            c = 100.0 + i * 0.05 + math.sin(i * 0.3) * 0.4
            cursor = f"multi-{tf}:{i}"
            out.append(
                OHLCVBar(
                    symbol="BTCUSDT",
                    timeframe=tf,
                    open_time=opened,
                    close_time=closed,
                    event_at=closed,
                    received_at=closed,
                    available_at=closed,
                    open=Decimal(str(round(c - 0.1, 4))),
                    high=Decimal(str(round(c + 0.6, 4))),
                    low=Decimal(str(round(c - 0.6, 4))),
                    close=Decimal(str(round(c, 4))),
                    volume=Decimal("150"),
                    quote_volume=Decimal(str(round(150 * c, 4))),
                    taker_buy_base_volume=Decimal("85"),
                    is_closed=True,
                    source="BINANCE_USDM_REST_BACKFILL",
                    source_cursor=cursor,
                    checksum=hashlib.sha256(cursor.encode()).hexdigest(),
                )
            )
        return tuple(out)

    rights_4h = BackfillRightsEvidence(
        workspace_id=WORKSPACE_ID,
        provider="BINANCE_USDM_REST",
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        terms_url="https://example.invalid/synthetic-4h-terms",
        policy_fingerprint=hashlib.sha256(b"4h-policy").hexdigest(),
        verified_at=origin_4h - timedelta(days=1),
        checked_at=origin_4h - timedelta(hours=1),
        valid_until=end_4h + timedelta(days=1),
    )
    bars_4h = _gen_bars("4h", INTERVALS["4h"], origin_4h, end_4h)
    bars_1h = _gen_bars("1h", INTERVALS["1h"], origin_4h, end_4h)
    m4h = build_backtest_dataset_manifest(
        RestBackfillRequest(
            symbol="BTCUSDT",
            interval="4h",
            start=origin_4h,
            end=end_4h,
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        ),
        evaluation_start=eval_4h,
        warmup_bars=int((eval_4h - origin_4h) / INTERVALS["4h"]),
        bars=bars_4h,
        rights_evidence=rights_4h,
        prepared_at=end_4h,
        schema_version=3,
    )
    m1h = build_backtest_dataset_manifest(
        RestBackfillRequest(
            symbol="BTCUSDT",
            interval="1h",
            start=origin_4h,
            end=end_4h,
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        ),
        evaluation_start=eval_4h,
        warmup_bars=int((eval_4h - origin_4h) / INTERVALS["1h"]),
        bars=bars_1h,
        rights_evidence=rights_4h,
        prepared_at=end_4h,
        schema_version=3,
    )
    bundle_4h1h = build_backtest_dataset_replay_bundle(
        (
            BacktestDatasetReplayInput(m4h, bars_4h, rights_4h),
            BacktestDatasetReplayInput(m1h, bars_1h, rights_4h),
        )
    )
    for rel, exec_tf in (
        ("h02-v1", "4h"),
        ("sc-binance-usdm-tsmom-003-v1.0.0", "4h"),
        ("h05-v1", "1h"),
        ("h21-v1", "1h"),
        ("sc-binance-usdm-trend-pullback-005-v1.0.0", "1h"),
        ("sc-binance-usdm-macd-adx-007-v1.0.0", "1h"),
    ):
        sub = BacktestReplayJobSubmission(
            bundle_hash=bundle_4h1h.dataset_hash,
            strategy_release_id=rel,
            execution_interval=exec_tf,
            starting_equity=Decimal("10000"),
            per_trade_risk_fraction=Decimal("0.01"),
            fee_rate=Decimal("0.0005"),
            slippage_rate=Decimal("0.0002"),
            seed=12,
        )
        plan = build_backtest_replay_plan(WORKSPACE_ID, uuid5(WORKSPACE_ID, rel), bundle_4h1h, sub)
        comp = plan.executor(bundle_4h1h, plan.manifest)
        assert len(comp.runs[0].sensitivity_run.result.report_hash) == 64
        assert comp.runs[0].sensitivity_run.result.usable_observation_count >= 1

    origin_5m = START - timedelta(hours=20)
    eval_5m = START + timedelta(hours=2)
    end_5m = START + timedelta(hours=3)
    rights_5m = BackfillRightsEvidence(
        workspace_id=WORKSPACE_ID,
        provider="BINANCE_USDM_REST",
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        terms_url="https://example.invalid/synthetic-5m-terms",
        policy_fingerprint=hashlib.sha256(b"5m-policy").hexdigest(),
        verified_at=origin_5m - timedelta(days=1),
        checked_at=origin_5m - timedelta(hours=1),
        valid_until=end_5m + timedelta(days=1),
    )
    bars_5m = _gen_bars("5m", INTERVALS["5m"], origin_5m, end_5m)
    m5m = build_backtest_dataset_manifest(
        RestBackfillRequest(
            symbol="BTCUSDT",
            interval="5m",
            start=origin_5m,
            end=end_5m,
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        ),
        evaluation_start=eval_5m,
        warmup_bars=int((eval_5m - origin_5m) / INTERVALS["5m"]),
        bars=bars_5m,
        rights_evidence=rights_5m,
        prepared_at=end_5m,
        schema_version=3,
    )
    bundle_5m = build_backtest_dataset_replay_bundle(
        (BacktestDatasetReplayInput(m5m, bars_5m, rights_5m),)
    )
    sub_h07 = BacktestReplayJobSubmission(
        bundle_hash=bundle_5m.dataset_hash,
        strategy_release_id="h07-v1",
        execution_interval="5m",
        starting_equity=Decimal("10000"),
        per_trade_risk_fraction=Decimal("0.01"),
        fee_rate=Decimal("0.0005"),
        slippage_rate=Decimal("0.0002"),
        seed=13,
    )
    plan_h07 = build_backtest_replay_plan(
        WORKSPACE_ID, uuid5(WORKSPACE_ID, "h07-v1"), bundle_5m, sub_h07
    )
    comp_h07 = plan_h07.executor(bundle_5m, plan_h07.manifest)
    assert len(comp_h07.runs[0].sensitivity_run.result.report_hash) == 64
    assert comp_h07.runs[0].sensitivity_run.result.usable_observation_count >= 1
