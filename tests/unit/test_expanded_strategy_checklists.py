"""Unit test parity cho ``strategy_checklist``: 6 rule mở rộng và 8 Strategy Cards L0–L6."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.application.strategy_catalog import PRODUCT_CARD_IDS, strategy_registry
from trading_bot.application.strategy_pipeline.card_strategy import strategy_card_release_id
from trading_bot.application.strategy_pipeline.demo_cards import build_default_demo_cards
from trading_bot.domain.market.features import ema, rsi
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.strategy.checklists import ChecklistItem, strategy_checklist
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    FeatureObservation,
    FeatureQuality,
    MarketSeries,
    NoTrade,
    SeriesQuality,
    StrategyCandidate,
    StrategySelection,
)
from trading_bot.domain.strategy.setup_progress import SETUP_RULES, setup_progress

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
    tbuy = None if taker_buy_base is None else Decimal(str(taker_buy_base))
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
        volume=Decimal(str(volume)),
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
    features: tuple[FeatureObservation, ...],
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
        snapshot_id="snap-checklist-test",
        series=qualified,
        features=features,
        position_is_flat=position_is_flat,
        macro_blackout=macro_blackout,
        macro_evidence_ids=("macro-1",),
        macro_evidence_available_at=latest,
        macro_is_fresh=True,
    )


def _selection(release_id: str, *, published: bool = True) -> StrategySelection:
    return StrategySelection(
        settings_version_id="settings-checklist-1",
        active_release_ids=(release_id,),
        published=published,
    )


def _by_key(items: tuple[ChecklistItem, ...]) -> dict[str, ChecklistItem]:
    return {item.key: item for item in items}


def _assert_parity(
    release_id: str,
    snapshot: DecisionSnapshot,
    settings: StrategySelection,
    *,
    expected_candidate: bool | None = None,
) -> tuple[StrategyCandidate | NoTrade, tuple[ChecklistItem, ...]]:
    strategy = strategy_registry().resolve(release_id, _selection(release_id))
    result = strategy.evaluate(snapshot, settings)
    items = strategy_checklist(release_id, snapshot, settings)
    assert items, f"Checklist không được rỗng cho {release_id}"
    all_passed = all(item.passed is True for item in items)
    if expected_candidate is True:
        assert isinstance(result, StrategyCandidate), result
    elif expected_candidate is False:
        assert isinstance(result, NoTrade), result
    if isinstance(result, StrategyCandidate):
        assert all_passed, [item.as_dict() for item in items]
    else:
        assert isinstance(result, NoTrade)
        assert not all_passed, (result.code, [item.as_dict() for item in items])
    return result, items


# ---------------------------------------------------------------------------
# Builders dữ liệu chuẩn cho 6 rule mở rộng
# ---------------------------------------------------------------------------
def _h02_snap(
    *, direction: str = "long", bars_count: int = 260, rv: float | None = 0.012
) -> DecisionSnapshot:
    step = 0.35 if direction == "long" else -0.35 if direction == "short" else 0.0
    base = 100.0 if direction != "short" else 300.0
    bars = tuple(_bar(i, "4h", base + i * step) for i in range(bars_count))
    at = bars[-1].available_at
    feats: list[FeatureObservation] = [_feature("atr_14", "4h", 1.5, at)]
    if rv is not None:
        feats.append(_feature("realized_volatility_20", "4h", rv, at))
    return _snapshot((MarketSeries("4h", bars),), tuple(feats))


def _h04_snap(
    *,
    direction: str = "long",
    squeeze: bool = True,
    expand_bw: bool = True,
    vol_mult: float = 2.4,
    breakout: bool = True,
    bars_count: int = 100,
    with_qvols: bool = True,
) -> DecisionSnapshot:
    bars: list[OHLCVBar] = []
    if squeeze:
        wavy_end = min(80 if not expand_bw else 85, bars_count - 15)
        for i in range(wavy_end):
            c = 102.0 if (not expand_bw and i == wavy_end - 1) else 100.0 + math.sin(i * 0.4) * 2.5
            bars.append(
                _bar(i, "15m", c, high=c + 1.5, low=c - 1.5, volume=100.0, quote_volume=10_000.0)
            )
        for i in range(len(bars), bars_count - 1):
            c = 100.0 + (0.05 if i % 2 == 0 else -0.05)
            bars.append(
                _bar(i, "15m", c, high=c + 0.12, low=c - 0.12, volume=90.0, quote_volume=9_000.0)
            )
    else:
        # Giữ lịch sử nén nhẹ rồi giãn mạnh chênh lệch close gần cuối -> BB vượt ngoài KC và > P25
        for i in range(bars_count - 5):
            c = 100.0 + math.sin(i * 0.3) * 0.2
            bars.append(
                _bar(i, "15m", c, high=c + 0.25, low=c - 0.25, volume=100.0, quote_volume=10_000.0)
            )
        for idx, c in enumerate((104.0, 96.0, 106.0, 94.0), start=bars_count - 5):
            bars.append(
                _bar(idx, "15m", c, high=c + 0.2, low=c - 0.2, volume=100.0, quote_volume=10_000.0)
            )

    last_idx = len(bars)
    if breakout:
        close_last = 104.5 if direction == "long" else 95.5
        open_last = 100.1 if direction == "long" else 99.9
        high_last = 104.8 if direction == "long" else 100.0
        low_last = 100.0 if direction == "long" else 95.2
    else:
        close_last = 100.02 if expand_bw else 100.0
        open_last = 100.0
        high_last = 100.1 if not expand_bw else 101.0
        low_last = 99.9 if not expand_bw else 99.0
    vol_last = 90.0 * vol_mult
    bars.append(
        _bar(
            last_idx,
            "15m",
            close_last,
            open_price=open_last,
            high=high_last,
            low=low_last,
            volume=vol_last,
            quote_volume=vol_last * 100.0,
        )
    )
    qvols = tuple(float(b.quote_volume or 0) for b in bars) if with_qvols else None
    at = bars[-1].available_at
    return _snapshot(
        (MarketSeries("15m", tuple(bars), quote_volumes=qvols),),
        (_feature("atr_14", "15m", 0.6, at),),
    )


def _h05_snap(
    *,
    direction: str = "long",
    adx: float = 24.0,
    rsi_val: float = 52.0,
    htf_ema200: float | None = None,
    reclaim: bool = True,
    bars_1h_count: int = 100,
) -> DecisionSnapshot:
    if direction == "long":
        bars_1h = [_bar(i, "1h", 100.0 + i * 0.5) for i in range(max(1, bars_1h_count - 5))]
        if bars_1h_count >= 20:
            bars_1h.extend(
                [
                    _bar(len(bars_1h), "1h", 145.5, open_price=147.0, high=147.5, low=145.0),
                    _bar(len(bars_1h) + 1, "1h", 144.0, open_price=145.5, high=145.8, low=143.5),
                    _bar(len(bars_1h) + 2, "1h", 143.2, open_price=144.0, high=144.2, low=142.8),
                    _bar(len(bars_1h) + 3, "1h", 142.8, open_price=143.2, high=143.5, low=142.4),
                    _bar(
                        len(bars_1h) + 4,
                        "1h",
                        144.2 if reclaim else 142.9,
                        open_price=142.6 if reclaim else 143.4,
                        high=144.5,
                        low=141.5,
                    ),
                ]
            )
        bars_4h = tuple(_bar(i, "4h", 100.0 + i * 2.0) for i in range(210))
        ema20, ema80, ema80_prev = 143.5, 136.0, 135.7
        ema200_default = 350.0
    else:
        bars_1h = [_bar(i, "1h", 200.0 - i * 0.5) for i in range(max(1, bars_1h_count - 1))]
        bars_1h.append(
            _bar(
                len(bars_1h),
                "1h",
                152.0 if reclaim else 154.0,
                open_price=153.5 if reclaim else 153.0,
                high=154.0,
                low=151.8,
            )
        )
        bars_4h = tuple(_bar(i, "4h", 300.0 - i * 1.0) for i in range(30))
        ema20, ema80, ema80_prev = 153.2, 160.0, 160.4
        ema200_default = 350.0

    at = bars_1h[-1].available_at
    feats = (
        _feature("atr_14", "1h", 1.8, at),
        _feature("ema_20", "1h", ema20, at, previous=ema20),
        _feature("ema_80", "1h", ema80, at, previous=ema80_prev),
        _feature("rsi_14", "1h", rsi_val, at),
        _feature("adx_14", "1h", adx, at),
        _feature(
            "ema_200",
            "4h",
            ema200_default if htf_ema200 is None else htf_ema200,
            bars_4h[-1].available_at,
        ),
    )
    return _snapshot((MarketSeries("1h", tuple(bars_1h)), MarketSeries("4h", bars_4h)), feats)


def _h07_snap(
    *,
    direction: str = "long",
    adx: float = 15.0,
    er: float = 0.18,
    vwap_val: float = 100.0,
    atr_val: float = 0.8,
    cross_midnight: bool = False,
    bars_count: int = 15,
) -> DecisionSnapshot:
    start_dt = datetime(2026, 9, 1, 23, 20, tzinfo=UTC) if cross_midnight else START
    if bars_count < 3:
        bars = [_bar(i, "5m", 100.0, start=start_dt) for i in range(bars_count)]
    elif direction == "long":
        bars = [_bar(i, "5m", 100.0, start=start_dt) for i in range(bars_count - 2)] + [
            _bar(bars_count - 2, "5m", 97.2, open_price=98.5, high=98.8, low=97.0, start=start_dt),
            _bar(bars_count - 1, "5m", 97.8, open_price=97.2, high=98.1, low=97.1, start=start_dt),
        ]
    else:
        bars = [_bar(i, "5m", 100.0, start=start_dt) for i in range(bars_count - 2)] + [
            _bar(
                bars_count - 2, "5m", 103.0, open_price=101.5, high=103.3, low=101.4, start=start_dt
            ),
            _bar(
                bars_count - 1, "5m", 102.2, open_price=103.0, high=103.1, low=102.0, start=start_dt
            ),
        ]
    at = bars[-1].available_at
    feats = (
        _feature("session_vwap", "5m", vwap_val, at),
        _feature("atr_14", "5m", atr_val, at),
        _feature("adx_14", "5m", adx, at),
        _feature("efficiency_ratio_40", "5m", er, at),
    )
    return _snapshot((MarketSeries("5m", tuple(bars)),), feats)


def _h21_snap(
    *,
    direction: str = "long",
    adx: float = 26.0,
    cross: bool = True,
    di_match: bool = True,
    htf_match: bool = True,
    bars_count: int = 40,
) -> DecisionSnapshot:
    if direction == "long":
        prices = [110.0 - i * 0.15 for i in range(bars_count)]
        if bars_count >= 2:
            prices[-2:] = [104.0, 108.5] if cross else [103.5, 102.0]
        pdi, mdi = (28.0, 14.0) if di_match else (14.0, 28.0)
        bars_4h = tuple(_bar(i, "4h", 100.0 + i * 0.5) for i in range(max(1, bars_count // 4)))
        ema200_4h = 95.0 if htf_match else 350.0
    else:
        prices = [100.0 + i * 0.15 for i in range(bars_count)]
        if cross and bars_count >= 2:
            prices[-2:] = [106.0, 100.5]
        pdi, mdi = (12.0, 29.0) if di_match else (29.0, 12.0)
        bars_4h = tuple(_bar(i, "4h", 300.0 - i * 1.0) for i in range(max(1, bars_count // 4)))
        ema200_4h = 350.0 if htf_match else 95.0
    bars_1h = tuple(_bar(i, "1h", p) for i, p in enumerate(prices))
    at = bars_1h[-1].available_at
    feats = (
        _feature("atr_14", "1h", 1.2, at),
        _feature("adx_14", "1h", adx, at),
        _feature("plus_di_14", "1h", pdi, at),
        _feature("minus_di_14", "1h", mdi, at),
        _feature("ema_200", "4h", ema200_4h, bars_4h[-1].available_at),
    )
    return _snapshot((MarketSeries("1h", bars_1h), MarketSeries("4h", bars_4h)), feats)


def _h22_snap(
    *,
    direction: str = "long",
    adx: float = 23.0,
    vol_last: float = 220.0,
    taker_buy_last: float | None = None,
    htf_ema80: float | None = None,
    bars_count: int = 22,
) -> DecisionSnapshot:
    if direction == "long":
        bars_15m = [
            _bar(i, "15m", 100.0, high=100.5, low=99.5, volume=100.0, taker_buy_base=52.0)
            for i in range(bars_count - 1)
        ]
        tbuy = min(176.0 if taker_buy_last is None else taker_buy_last, vol_last * 0.8)
        bars_15m.append(
            _bar(
                bars_count - 1,
                "15m",
                102.0,
                open_price=100.2,
                high=102.3,
                low=100.0,
                volume=vol_last,
                taker_buy_base=tbuy,
            )
        )
        bars_1h = tuple(_bar(i, "1h", 102.0) for i in range(10))
        ema80 = 98.0 if htf_ema80 is None else htf_ema80
    else:
        bars_15m = [
            _bar(i, "15m", 100.0, high=100.5, low=99.5, volume=100.0, taker_buy_base=46.0)
            for i in range(bars_count - 1)
        ]
        tbuy = 40.0 if taker_buy_last is None else taker_buy_last
        bars_15m.append(
            _bar(
                bars_count - 1,
                "15m",
                97.5,
                open_price=99.8,
                high=99.9,
                low=97.2,
                volume=vol_last,
                taker_buy_base=tbuy,
            )
        )
        bars_1h = tuple(_bar(i, "1h", 95.0) for i in range(5))
        ema80 = 102.0 if htf_ema80 is None else htf_ema80
    at = bars_15m[-1].available_at
    feats = (
        _feature("atr_14", "15m", 0.9, at),
        _feature("adx_14", "15m", adx, at),
        _feature("ema_80", "1h", ema80, bars_1h[-1].available_at),
        _feature("atr_14", "1h", 1.8, bars_1h[-1].available_at),
    )
    return _snapshot((MarketSeries("15m", tuple(bars_15m)), MarketSeries("1h", bars_1h)), feats)


# ---------------------------------------------------------------------------
# 1. Parity & Đạt / Chưa đạt / Thiếu dữ liệu cho 6 rule mở rộng
# ---------------------------------------------------------------------------
def test_h02_checklist_pass_fail_and_missing() -> None:
    rel = "h02-v1"
    sel = _selection(rel)

    # Đạt LONG & SHORT + khớp SETUP_RULES["H02"].required
    for direction in ("long", "short"):
        cand, items = _assert_parity(
            rel, _h02_snap(direction=direction), sel, expected_candidate=True
        )
        assert cand.direction == direction
        assert tuple(item.key for item in items) == SETUP_RULES["H02"].required

    # Chưa đạt: RV ngoài biên, động lượng không đồng thuận, macro blackout, vị thế chưa phẳng
    _, items_rv = _assert_parity(rel, _h02_snap(rv=0.12), sel, expected_candidate=False)
    assert _by_key(items_rv)["rv_regime"].passed is False

    _, items_flat = _assert_parity(rel, _h02_snap(direction="flat"), sel, expected_candidate=False)
    assert _by_key(items_flat)["tsmom_horizon"].passed is False

    _, items_macro = _assert_parity(
        rel, replace(_h02_snap(), macro_blackout=True), sel, expected_candidate=False
    )
    assert _by_key(items_macro)["macro_clear"].passed is False

    _, items_pos = _assert_parity(
        rel, replace(_h02_snap(), position_is_flat=False), sel, expected_candidate=False
    )
    assert _by_key(items_pos)["position_flat"].passed is False

    # Thiếu dữ liệu: thiếu warmup 253 nến, thiếu RV, thiếu ATR, series STALE
    _, items_warmup = _assert_parity(rel, _h02_snap(bars_count=120), sel, expected_candidate=False)
    assert _by_key(items_warmup)["tsmom_horizon"].passed is None

    _, items_no_rv = _assert_parity(rel, _h02_snap(rv=None), sel, expected_candidate=False)
    assert _by_key(items_no_rv)["rv_regime"].passed is None

    snap_no_atr = replace(_h02_snap(), features=(_h02_snap().features[1],))
    _, items_no_atr = _assert_parity(rel, snap_no_atr, sel, expected_candidate=False)
    assert _by_key(items_no_atr)["atr_valid"].passed is None

    snap_stale = replace(
        _h02_snap(),
        series=(replace(_h02_snap().series[0], quality=SeriesQuality.STALE),),
    )
    _, items_stale = _assert_parity(rel, snap_stale, sel, expected_candidate=False)
    assert _by_key(items_stale)["tsmom_horizon"].passed is None


def test_h04_checklist_pass_fail_and_missing() -> None:
    rel = "h04-v1"
    sel = _selection(rel)

    for direction in ("long", "short"):
        cand, items = _assert_parity(
            rel, _h04_snap(direction=direction), sel, expected_candidate=True
        )
        assert cand.direction == direction
        assert tuple(item.key for item in items) == SETUP_RULES["H04"].required

    # Chưa đạt: không nén, không giãn BW, thiếu volume surge, chưa phá biên
    _, items_no_sq = _assert_parity(rel, _h04_snap(squeeze=False), sel, expected_candidate=False)
    assert _by_key(items_no_sq)["prior_squeeze"].passed is False

    _, items_no_bw = _assert_parity(
        rel, _h04_snap(expand_bw=False, breakout=False), sel, expected_candidate=False
    )
    assert _by_key(items_no_bw)["bw_expansion"].passed is False

    _, items_low_vol = _assert_parity(rel, _h04_snap(vol_mult=0.9), sel, expected_candidate=False)
    assert _by_key(items_low_vol)["volume_surge"].passed is False

    _, items_no_brk = _assert_parity(rel, _h04_snap(breakout=False), sel, expected_candidate=False)
    assert _by_key(items_no_brk)["band_breakout"].passed is False

    # Thiếu dữ liệu: thiếu nến warmup, thiếu quote_volumes
    _, items_short = _assert_parity(rel, _h04_snap(bars_count=30), sel, expected_candidate=False)
    mapped_short = _by_key(items_short)
    assert mapped_short["prior_squeeze"].passed is None
    assert mapped_short["volume_surge"].passed is None

    _, items_no_qvol = _assert_parity(
        rel, _h04_snap(with_qvols=False), sel, expected_candidate=False
    )
    assert _by_key(items_no_qvol)["volume_surge"].passed is None


def test_h05_checklist_pass_fail_and_missing() -> None:
    rel = "h05-v1"
    sel = _selection(rel)

    for direction in ("long", "short"):
        cand, items = _assert_parity(
            rel, _h05_snap(direction=direction), sel, expected_candidate=True
        )
        assert cand.direction == direction
        assert tuple(item.key for item in items) == SETUP_RULES["H05"].required

    # Chưa đạt: ADX thấp, RSI ngoài [40, 60], 4h ngược EMA200, chưa giữ EMA20, lệch hướng 4h/1h
    _, items_adx = _assert_parity(rel, _h05_snap(adx=15.0), sel, expected_candidate=False)
    assert _by_key(items_adx)["adx"].passed is False

    _, items_rsi = _assert_parity(rel, _h05_snap(rsi_val=72.0), sel, expected_candidate=False)
    assert _by_key(items_rsi)["rsi_band"].passed is False

    _, items_htf = _assert_parity(rel, _h05_snap(htf_ema200=999.0), sel, expected_candidate=False)
    assert _by_key(items_htf)["ema_alignment"].passed is False

    _, items_reclaim = _assert_parity(rel, _h05_snap(reclaim=False), sel, expected_candidate=False)
    assert _by_key(items_reclaim)["pullback_reclaim"].passed is False

    # Thiếu dữ liệu: thiếu warmup 1h, thiếu feature 4h EMA200
    _, items_warmup = _assert_parity(
        rel, _h05_snap(bars_1h_count=10), sel, expected_candidate=False
    )
    assert _by_key(items_warmup)["pullback_reclaim"].passed is None

    snap_no_4h_feat = replace(_h05_snap(), features=_h05_snap().features[:-1])
    _, items_no_4h = _assert_parity(rel, snap_no_4h_feat, sel, expected_candidate=False)
    assert _by_key(items_no_4h)["htf_trend"].passed is None


def test_h07_checklist_pass_fail_and_missing() -> None:
    rel = "h07-v1"
    sel = _selection(rel)

    for direction in ("long", "short"):
        cand, items = _assert_parity(
            rel, _h07_snap(direction=direction), sel, expected_candidate=True
        )
        assert cand.direction == direction
        assert tuple(item.key for item in items) == SETUP_RULES["H07"].required

    # Chưa đạt: qua nửa đêm (< 12 nến UTC trong ngày), ADX >= 20, chi phí < 0.3%
    _, items_sess = _assert_parity(
        rel, _h07_snap(cross_midnight=True), sel, expected_candidate=False
    )
    assert _by_key(items_sess)["session_warmup"].passed is False

    _, items_adx = _assert_parity(rel, _h07_snap(adx=25.0), sel, expected_candidate=False)
    assert _by_key(items_adx)["range_regime"].passed is False

    _, items_cost = _assert_parity(
        rel, _h07_snap(vwap_val=97.9, atr_val=0.04), sel, expected_candidate=False
    )
    assert _by_key(items_cost)["cost_hurdle"].passed is False

    # Thiếu dữ liệu: < 12 nến 5m, thiếu session_vwap
    _, items_short = _assert_parity(rel, _h07_snap(bars_count=5), sel, expected_candidate=False)
    mapped_short = _by_key(items_short)
    assert mapped_short["session_warmup"].passed is None
    assert mapped_short["vwap_deviation"].passed is None

    snap_no_vwap = replace(_h07_snap(), features=_h07_snap().features[1:])
    _, items_no_vwap = _assert_parity(rel, snap_no_vwap, sel, expected_candidate=False)
    assert _by_key(items_no_vwap)["vwap_deviation"].passed is None
    assert _by_key(items_no_vwap)["cost_hurdle"].passed is None


def test_h21_checklist_pass_fail_and_missing() -> None:
    rel = "h21-v1"
    sel = _selection(rel)

    for direction in ("long", "short"):
        cand, items = _assert_parity(
            rel, _h21_snap(direction=direction), sel, expected_candidate=True
        )
        assert cand.direction == direction
        assert tuple(item.key for item in items) == SETUP_RULES["H21"].required

    # Chưa đạt: ADX < 22, không cross MACD, ±DI ngược chiều, 4h ngược EMA200
    _, items_adx = _assert_parity(rel, _h21_snap(adx=16.0), sel, expected_candidate=False)
    assert _by_key(items_adx)["adx"].passed is False

    _, items_nocross = _assert_parity(rel, _h21_snap(cross=False), sel, expected_candidate=False)
    assert _by_key(items_nocross)["macd_hist_cross"].passed is False

    _, items_di = _assert_parity(rel, _h21_snap(di_match=False), sel, expected_candidate=False)
    assert _by_key(items_di)["di_alignment"].passed is False

    _, items_htf = _assert_parity(rel, _h21_snap(htf_match=False), sel, expected_candidate=False)
    assert _by_key(items_htf)["di_alignment"].passed is False

    # Thiếu dữ liệu: < 35 nến 1h, thiếu plus_di_14
    _, items_warmup = _assert_parity(rel, _h21_snap(bars_count=20), sel, expected_candidate=False)
    assert _by_key(items_warmup)["macd_hist_cross"].passed is None

    snap_no_di = replace(
        _h21_snap(),
        features=tuple(f for f in _h21_snap().features if f.name != "plus_di_14"),
    )
    _, items_no_di = _assert_parity(rel, snap_no_di, sel, expected_candidate=False)
    assert _by_key(items_no_di)["di_alignment"].passed is None


def test_h22_checklist_pass_fail_and_missing() -> None:
    rel = "h22-v1"
    sel = _selection(rel)

    for direction in ("long", "short"):
        cand, items = _assert_parity(
            rel, _h22_snap(direction=direction), sel, expected_candidate=True
        )
        assert cand.direction == direction
        assert tuple(item.key for item in items) == SETUP_RULES["H22"].required

    # Chưa đạt: ADX < 18, volume thấp, taker imbalance ngược (fakeout), 1h ngược EMA80
    _, items_adx = _assert_parity(rel, _h22_snap(adx=14.0), sel, expected_candidate=False)
    assert _by_key(items_adx)["adx"].passed is False

    _, items_vol = _assert_parity(rel, _h22_snap(vol_last=100.0), sel, expected_candidate=False)
    assert _by_key(items_vol)["volume_surge"].passed is False

    _, items_fake = _assert_parity(
        rel, _h22_snap(taker_buy_last=55.0), sel, expected_candidate=False
    )
    assert _by_key(items_fake)["taker_imbalance_breakout"].passed is False

    _, items_ema = _assert_parity(rel, _h22_snap(htf_ema80=110.0), sel, expected_candidate=False)
    assert _by_key(items_ema)["taker_imbalance_breakout"].passed is False

    # Thiếu dữ liệu: thiếu taker_buy_base_volume, thiếu warmup 21 nến, thiếu 1h:atr_14
    snap_missing_taker = replace(
        _h22_snap(),
        series=(
            replace(
                _h22_snap().series[0],
                bars=_h22_snap().series[0].bars[:-1]
                + (replace(_h22_snap().series[0].bars[-1], taker_buy_base_volume=None),),
            ),
            _h22_snap().series[1],
        ),
    )
    _, items_no_taker = _assert_parity(rel, snap_missing_taker, sel, expected_candidate=False)
    assert _by_key(items_no_taker)["taker_flow_valid"].passed is None

    _, items_warmup = _assert_parity(rel, _h22_snap(bars_count=10), sel, expected_candidate=False)
    assert _by_key(items_warmup)["taker_flow_valid"].passed is None
    assert _by_key(items_warmup)["volume_surge"].passed is None

    snap_no_1h_atr = replace(_h22_snap(), features=_h22_snap().features[:-1])
    _, items_no_1h_atr = _assert_parity(rel, snap_no_1h_atr, sel, expected_candidate=False)
    assert _by_key(items_no_1h_atr)["atr_valid_1h"].passed is None


def test_expanded_rules_setup_progress_and_unpublished_settings() -> None:
    snap = _h02_snap()
    sel = _selection("h02-v1")
    cand, items = _assert_parity("h02-v1", snap, sel, expected_candidate=True)
    now = cand.signal_at + timedelta(seconds=1)
    decisions = [
        {
            "evaluated_at": cand.signal_at.isoformat(),
            "status": "CANDIDATE",
            "reason_code": cand.reason_code,
            "detail_vi": cand.detail_vi,
            "checklist": [item.as_dict() for item in items],
            "candidate": {"direction": cand.direction, "expires_at": cand.expires_at.isoformat()},
        }
    ]
    progress = setup_progress(strategy_release_id="h02-v1", decisions=decisions, bonus=[], now=now)
    assert progress["status"] == "ready"
    assert progress["required"] == {"passed": 2, "total": 2}

    # Settings chưa published -> STRATEGY_NOT_ACTIVE -> parity vẫn phải giữ
    unpub = _selection("h02-v1", published=False)
    res = strategy_registry().resolve("h02-v1", sel).evaluate(snap, unpub)
    assert isinstance(res, NoTrade)
    chk = strategy_checklist("h02-v1", snap, unpub)
    assert not all(item.passed is True for item in chk)
    assert _by_key(chk)["strategy_active"].passed is False


# ---------------------------------------------------------------------------
# 2. Builders & Tests cho toàn bộ 8 Strategy Cards (sc-binance-usdm-*)
# ---------------------------------------------------------------------------
def _card_release_map() -> dict[str, str]:
    return {card.card_id: strategy_card_release_id(card) for card in build_default_demo_cards()}


def _card001_snap(*, hammer: bool = True, atr: float | None = 1.3) -> DecisionSnapshot:
    bar = _bar(
        0,
        "1h",
        100.2 if hammer else 101.0,
        open_price=100.0,
        high=100.3 if hammer else 102.0,
        low=99.0 if hammer else 100.0,
    )
    feats = (_feature("atr_14", "1h", atr, bar.available_at),) if atr is not None else ()
    return _snapshot((MarketSeries("1h", (bar,)),), feats)


def _card002_snap(
    *, direction: str = "long", body_large: bool = False, atr: float = 1.2
) -> DecisionSnapshot:
    bars = [_bar(i, "15m", 100.0 + ((i % 2) - 0.5) * 0.4, high=100.5, low=99.5) for i in range(204)]
    if direction == "long":
        last = _bar(
            204,
            "15m",
            96.5 if body_large else 97.6,
            open_price=99.5 if body_large else 97.3,
            high=99.6 if body_large else 97.9,
            low=96.0 if body_large else 93.5,
        )
    else:
        last = _bar(
            204,
            "15m",
            102.4,
            open_price=102.7,
            high=106.5,
            low=102.1,
        )
    bars.append(last)
    return _snapshot(
        (MarketSeries("15m", tuple(bars)),), (_feature("atr_14", "15m", atr, last.available_at),)
    )


def _card005_snap(
    *, direction: str = "long", adx: float = 24.0, rsi_ok: bool = True, bars_count: int = 90
) -> DecisionSnapshot:
    # Dựng chuỗi 1h thỏa mãn EMA20/EMA80/RSI14 trong pipeline_engine cho pullback
    if direction == "long":
        closes = [100.0 + i * 0.4 for i in range(max(1, bars_count - 5))]
        bars_1h = [
            _bar(i, "1h", c, open_price=c - 0.1, high=c + 0.5, low=c - 0.5)
            for i, c in enumerate(closes)
        ]
        if bars_count >= 83:
            # Điều chỉnh nhẹ để RSI14 về [40, 60] rồi nến cuối chạm EMA20 và đóng trên EMA20
            pull_closes = [closes[-1] - 0.9, closes[-1] - 1.8, closes[-1] - 2.6, closes[-1] - 3.3]
            for pc in pull_closes:
                idx = len(bars_1h)
                bars_1h.append(
                    _bar(idx, "1h", pc, open_price=pc + 0.4, high=pc + 0.6, low=pc - 0.3)
                )
            e20_series = ema([float(b.close) for b in bars_1h] + [closes[-1] - 1.2], 20)
            e20_val = float(e20_series[-1] or closes[-1])
            if rsi_ok:
                last_close = e20_val + 0.3
                last_open = e20_val - 0.2
                last_low = e20_val - 0.8
            else:
                # Đẩy vọt làm RSI > 60
                last_close = closes[-1] + 25.0
                last_open = e20_val - 0.2
                last_low = e20_val - 0.8
            bars_1h.append(
                _bar(
                    len(bars_1h),
                    "1h",
                    last_close,
                    open_price=last_open,
                    high=last_close + 0.4,
                    low=last_low,
                )
            )
            # Xác nhận RSI nằm đúng vùng mong muốn
            r_val = rsi([float(b.close) for b in bars_1h], 14)[-1]
            if rsi_ok and r_val is not None and not (40.0 <= r_val <= 60.0):
                # Chỉnh nhẹ độ sâu 4 nến hồi sao cho RSI vào [40, 60]
                for step_adj in (0.5, 1.0, 1.5, -0.5, -1.0):
                    test_bars = list(bars_1h[:-5])
                    for k, pc in enumerate(pull_closes):
                        adj_c = pc - step_adj * (k + 1) * 0.4
                        test_bars.append(_bar(len(test_bars), "1h", adj_c, open_price=adj_c + 0.4))
                    e20_t = float(
                        ema([float(b.close) for b in test_bars] + [adj_c + 0.5], 20)[-1] or adj_c
                    )
                    test_bars.append(
                        _bar(
                            len(test_bars),
                            "1h",
                            e20_t + 0.3,
                            open_price=e20_t - 0.2,
                            high=e20_t + 0.6,
                            low=e20_t - 0.6,
                        )
                    )
                    rt = rsi([float(b.close) for b in test_bars], 14)[-1]
                    if rt is not None and 40.0 <= rt <= 60.0:
                        bars_1h = test_bars
                        break
        bars_4h = tuple(_bar(i, "4h", 100.0 + i * 2.0) for i in range(max(1, len(bars_1h) // 4)))
        ema200 = 120.0
    else:
        closes = [250.0 - i * 0.4 for i in range(max(1, bars_count - 5))]
        bars_1h = [
            _bar(i, "1h", c, open_price=c + 0.1, high=c + 0.5, low=c - 0.5)
            for i, c in enumerate(closes)
        ]
        if bars_count >= 83:
            bounce_closes = [closes[-1] + 0.8, closes[-1] + 1.6, closes[-1] + 2.3, closes[-1] + 2.9]
            for bc in bounce_closes:
                bars_1h.append(
                    _bar(len(bars_1h), "1h", bc, open_price=bc - 0.4, high=bc + 0.3, low=bc - 0.5)
                )
            e20_val = float(
                ema([float(b.close) for b in bars_1h] + [closes[-1] + 1.0], 20)[-1] or closes[-1]
            )
            bars_1h.append(
                _bar(
                    len(bars_1h),
                    "1h",
                    e20_val - 0.3,
                    open_price=e20_val + 0.2,
                    high=e20_val + 0.6,
                    low=e20_val - 0.5,
                )
            )
            r_val = rsi([float(b.close) for b in bars_1h], 14)[-1]
            if rsi_ok and r_val is not None and not (40.0 <= r_val <= 60.0):
                for step_adj in (0.5, 1.0, 1.5, -0.5, -1.0):
                    test_bars = list(bars_1h[:-5])
                    for k, bc in enumerate(bounce_closes):
                        adj_c = bc + step_adj * (k + 1) * 0.4
                        test_bars.append(_bar(len(test_bars), "1h", adj_c, open_price=adj_c - 0.4))
                    e20_t = float(
                        ema([float(b.close) for b in test_bars] + [adj_c - 0.5], 20)[-1] or adj_c
                    )
                    test_bars.append(
                        _bar(
                            len(test_bars),
                            "1h",
                            e20_t - 0.3,
                            open_price=e20_t + 0.2,
                            high=e20_t + 0.6,
                            low=e20_t - 0.6,
                        )
                    )
                    rt = rsi([float(b.close) for b in test_bars], 14)[-1]
                    if rt is not None and 40.0 <= rt <= 60.0:
                        bars_1h = test_bars
                        break
        bars_4h = tuple(_bar(i, "4h", 300.0 - i * 1.0) for i in range(max(1, len(bars_1h) // 4)))
        ema200 = 350.0

    at = bars_1h[-1].available_at
    feats = (
        _feature("atr_14", "1h", 1.5, at),
        _feature("adx_14", "1h", adx, at),
        _feature("ema_200", "4h", ema200, bars_4h[-1].available_at),
    )
    return _snapshot((MarketSeries("1h", tuple(bars_1h)), MarketSeries("4h", bars_4h)), feats)


def _card006_snap(
    *,
    direction: str = "long",
    adx: float = 15.0,
    close_loc_ok: bool = True,
    bars_count: int = 15,
    dev_ok: bool = True,
) -> DecisionSnapshot:
    bars = [_bar(i, "15m", 100.0) for i in range(max(1, bars_count - 1))]
    if bars_count >= 2:
        if direction == "long":
            c_last = 98.0 if dev_ok else 99.5
            bars.append(
                _bar(
                    bars_count - 1,
                    "15m",
                    c_last,
                    open_price=(97.0 if close_loc_ok else 99.1) if dev_ok else 99.4,
                    high=(98.2 if close_loc_ok else 99.2) if dev_ok else 99.8,
                    low=(96.8 if close_loc_ok else 97.9) if dev_ok else 99.0,
                )
            )
        else:
            c_last = 102.0 if dev_ok else 100.5
            bars.append(
                _bar(
                    bars_count - 1,
                    "15m",
                    c_last,
                    open_price=103.0 if close_loc_ok else 100.9,
                    high=103.2 if close_loc_ok else 102.1,
                    low=101.8 if close_loc_ok else 100.8,
                )
            )
    at = bars[-1].available_at
    feats = (
        _feature("session_vwap", "15m", 100.0, at),
        _feature("atr_14", "15m", 0.8, at),
        _feature("adx_14", "15m", adx, at),
    )
    return _snapshot((MarketSeries("15m", tuple(bars)),), feats)


def _card008_snap(
    *,
    direction: str = "long",
    adx: float = 23.0,
    vol_last: float = 260.0,
    taker_buy_last: float | None = None,
    missing_taker: bool = False,
    bars_count: int = 205,
) -> DecisionSnapshot:
    if direction == "long":
        bars_15m = [
            _bar(
                i,
                "15m",
                100.0 + (i % 3) * 0.05,
                high=100.6,
                low=99.4,
                volume=100.0,
                taker_buy_base=52.0,
            )
            for i in range(bars_count - 1)
        ]
        if bars_count >= 205:
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
                )
        tbuy = (
            None
            if missing_taker
            else min(215.0 if taker_buy_last is None else taker_buy_last, vol_last * 0.85)
        )
        bars_15m.append(
            _bar(
                len(bars_15m),
                "15m",
                106.5,
                open_price=103.5,
                high=106.8,
                low=103.4,
                volume=vol_last,
                taker_buy_base=tbuy,
            )
        )
        bars_1h = tuple(
            _bar(i, "1h", 90.0 + i * 0.2, volume=300.0, taker_buy_base=170.0) for i in range(25)
        )
        ema80 = 91.0
    else:
        bars_15m = [
            _bar(
                i,
                "15m",
                100.0 - (i % 3) * 0.05,
                high=100.6,
                low=99.4,
                volume=100.0,
                taker_buy_base=46.0,
            )
            for i in range(bars_count - 1)
        ]
        tbuy = None if missing_taker else (30.0 if taker_buy_last is None else taker_buy_last)
        bars_15m.append(
            _bar(
                len(bars_15m),
                "15m",
                96.5,
                open_price=99.5,
                high=99.6,
                low=96.2,
                volume=vol_last,
                taker_buy_base=tbuy,
            )
        )
        bars_1h = tuple(
            _bar(i, "1h", 120.0 - i * 0.2, volume=300.0, taker_buy_base=120.0) for i in range(25)
        )
        ema80 = 130.0
    at = bars_15m[-1].available_at
    feats = (
        _feature("atr_14", "15m", 0.9, at),
        _feature("adx_14", "15m", adx, at),
        _feature("ema_80", "1h", ema80, bars_1h[-1].available_at),
    )
    return _snapshot((MarketSeries("15m", tuple(bars_15m)), MarketSeries("1h", bars_1h)), feats)


def test_all_8_strategy_cards_are_mapped_and_bare_card_id_stays_empty() -> None:
    rel_map = _card_release_map()
    assert len(rel_map) == 8
    assert tuple(rel_map) == PRODUCT_CARD_IDS
    # Bảo toàn contract của test_sig3_signal_card: card_id không có suffix `-v...` vẫn trả rỗng
    assert (
        strategy_checklist("sc-binance-usdm-vol-breakout-001", _card001_snap(), _selection("x"))
        == ()
    )


def test_card_001_vol_breakout_checklist() -> None:
    rel = _card_release_map()["SC-BINANCE-USDM-VOL-BREAKOUT-001"]
    sel = _selection(rel)

    cand, items = _assert_parity(
        rel, _card001_snap(hammer=True, atr=1.3), sel, expected_candidate=True
    )
    assert cand.direction == "long"
    assert _by_key(items)["l5_trigger"].passed is True

    # Chưa đạt L5
    _, items_no_trig = _assert_parity(
        rel, _card001_snap(hammer=False, atr=1.3), sel, expected_candidate=False
    )
    assert _by_key(items_no_trig)["l5_trigger"].passed is False

    # Chưa đạt L4 (ATR thấp + macro blackout)
    _, items_low_atr = _assert_parity(
        rel, _card001_snap(hammer=True, atr=0.001), sel, expected_candidate=False
    )
    assert _by_key(items_low_atr)["l4_volatility_atr"].passed is False
    assert _by_key(items_low_atr)["l5_trigger"].passed is None

    _, items_macro = _assert_parity(
        rel, replace(_card001_snap(), macro_blackout=True), sel, expected_candidate=False
    )
    assert _by_key(items_macro)["macro_clear"].passed is False

    # Chưa đạt L0 (nến hỏng high < low)
    bad_snap = _card001_snap()
    bad_bar = bad_snap.series[0].bars[0]
    object.__setattr__(bad_bar, "high", Decimal("90"))
    object.__setattr__(bad_bar, "low", Decimal("100"))
    _, items_l0 = _assert_parity(rel, bad_snap, sel, expected_candidate=False)
    assert _by_key(items_l0)["l0_data_hygiene"].passed is False

    # Thiếu dữ liệu: 1 nến thiếu ATR (single_bar_range) và snapshot rỗng
    _, items_single = _assert_parity(
        rel, _card001_snap(hammer=True, atr=None), sel, expected_candidate=False
    )
    assert _by_key(items_single)["indicator_warmup"].passed is None

    empty_snap = _snapshot((), ())
    _, items_empty = _assert_parity(rel, empty_snap, sel, expected_candidate=False)
    assert _by_key(items_empty)["l0_data_hygiene"].passed is None


def test_card_002_mean_revert_checklist() -> None:
    rel = _card_release_map()["SC-BINANCE-USDM-MEAN-REVERT-002"]
    sel = _selection(rel)

    for direction in ("long", "short"):
        cand, _ = _assert_parity(
            rel, _card002_snap(direction=direction), sel, expected_candidate=True
        )
        assert cand.direction == direction

    # Chưa đạt: thân nến quá lớn (L5 wick), Z-score chưa cực trị, ATR thấp, macro blackout
    _, items_body = _assert_parity(
        rel, _card002_snap(body_large=True), sel, expected_candidate=False
    )
    mapped = _by_key(items_body)
    assert mapped["l5_zscore_extreme"].passed is True
    assert mapped["l5_wick_reversal"].passed is False

    flat_bars = tuple(_bar(i, "15m", 100.0 + (i % 2) * 0.1) for i in range(205))
    snap_flat = _snapshot(
        (MarketSeries("15m", flat_bars),),
        (_feature("atr_14", "15m", 1.2, flat_bars[-1].available_at),),
    )
    _, items_noz = _assert_parity(rel, snap_flat, sel, expected_candidate=False)
    assert _by_key(items_noz)["l5_zscore_extreme"].passed is False

    _, items_atr = _assert_parity(rel, _card002_snap(atr=0.001), sel, expected_candidate=False)
    assert _by_key(items_atr)["l4_volatility_atr"].passed is False

    # Thiếu dữ liệu: 1 nến warmup incomplete
    single = _bar(0, "15m", 97.6, open_price=97.3, high=97.9, low=93.5)
    snap_1 = _snapshot(
        (MarketSeries("15m", (single,)),), (_feature("atr_14", "15m", 1.2, single.available_at),)
    )
    _, items_w = _assert_parity(rel, snap_1, sel, expected_candidate=False)
    assert _by_key(items_w)["indicator_warmup"].passed is None
    assert _by_key(items_w)["l5_zscore_extreme"].passed is None


def test_card_003_tsmom_checklist() -> None:
    rel = _card_release_map()["SC-BINANCE-USDM-TSMOM-003"]
    sel = _selection(rel)

    for direction in ("long", "short"):
        cand, _ = _assert_parity(rel, _h02_snap(direction=direction), sel, expected_candidate=True)
        assert cand.direction == direction

    # Chưa đạt: L4 RV ngoài biên, L5 tsmom = 0
    _, items_rv = _assert_parity(rel, _h02_snap(rv=0.12), sel, expected_candidate=False)
    assert _by_key(items_rv)["l4_rv_regime"].passed is False
    assert _by_key(items_rv)["l5_tsmom_trigger"].passed is None

    _, items_flat = _assert_parity(rel, _h02_snap(direction="flat"), sel, expected_candidate=False)
    assert _by_key(items_flat)["l5_tsmom_trigger"].passed is False

    # Thiếu dữ liệu: < 253 nến
    _, items_w = _assert_parity(
        rel, _h02_snap(bars_count=50, rv=None), sel, expected_candidate=False
    )
    assert _by_key(items_w)["indicator_warmup"].passed is None
    assert _by_key(items_w)["l5_tsmom_trigger"].passed is None


def test_card_004_bbkc_squeeze_checklist() -> None:
    rel = _card_release_map()["SC-BINANCE-USDM-BBKC-SQUEEZE-004"]
    sel = _selection(rel)

    for direction in ("long", "short"):
        cand, _ = _assert_parity(rel, _h04_snap(direction=direction), sel, expected_candidate=True)
        assert cand.direction == direction

    # Chưa đạt: L4 ATR thấp, L5 không đủ volume / không nén
    low_atr_snap = replace(
        _h04_snap(),
        features=(_feature("atr_14", "15m", 0.001, _h04_snap().decision_time),),
    )
    _, items_atr = _assert_parity(rel, low_atr_snap, sel, expected_candidate=False)
    assert _by_key(items_atr)["l4_volatility_atr"].passed is False

    _, items_vol = _assert_parity(rel, _h04_snap(vol_mult=0.8), sel, expected_candidate=False)
    assert _by_key(items_vol)["l5_squeeze_trigger"].passed is False

    # Thiếu dữ liệu: < 60 nến
    _, items_w = _assert_parity(rel, _h04_snap(bars_count=30), sel, expected_candidate=False)
    assert _by_key(items_w)["indicator_warmup"].passed is None
    assert _by_key(items_w)["l5_squeeze_trigger"].passed is None


def test_card_005_trend_pullback_checklist() -> None:
    rel = _card_release_map()["SC-BINANCE-USDM-TREND-PULLBACK-005"]
    sel = _selection(rel)

    for direction in ("long", "short"):
        cand, _ = _assert_parity(
            rel, _card005_snap(direction=direction), sel, expected_candidate=True
        )
        assert cand.direction == direction

    # Chưa đạt: L4 ADX < 18, L5 RSI ngoài [40, 60]
    _, items_adx = _assert_parity(rel, _card005_snap(adx=12.0), sel, expected_candidate=False)
    assert _by_key(items_adx)["l4_adx_regime"].passed is False
    assert _by_key(items_adx)["l5_pullback_trigger"].passed is None

    _, items_rsi = _assert_parity(rel, _card005_snap(rsi_ok=False), sel, expected_candidate=False)
    assert _by_key(items_rsi)["l5_pullback_trigger"].passed is False

    # Thiếu dữ liệu: < 83 nến
    _, items_w = _assert_parity(rel, _card005_snap(bars_count=40), sel, expected_candidate=False)
    assert _by_key(items_w)["indicator_warmup"].passed is None
    assert _by_key(items_w)["l5_pullback_trigger"].passed is None


def test_card_006_vwap_revert_checklist() -> None:
    rel = _card_release_map()["SC-BINANCE-USDM-VWAP-REVERT-006"]
    sel = _selection(rel)

    for direction in ("long", "short"):
        cand, _ = _assert_parity(
            rel, _card006_snap(direction=direction), sel, expected_candidate=True
        )
        assert cand.direction == direction

    # Chưa đạt: L4 ADX >= 20, L5 close_location sai hướng, L5 dev nhỏ
    _, items_adx = _assert_parity(rel, _card006_snap(adx=25.0), sel, expected_candidate=False)
    assert _by_key(items_adx)["l4_adx_regime"].passed is False
    assert _by_key(items_adx)["l5_vwap_deviation"].passed is None

    _, items_loc = _assert_parity(
        rel, _card006_snap(close_loc_ok=False), sel, expected_candidate=False
    )
    mapped_loc = _by_key(items_loc)
    assert mapped_loc["l5_vwap_deviation"].passed is True
    assert mapped_loc["l5_close_location"].passed is False

    _, items_dev = _assert_parity(rel, _card006_snap(dev_ok=False), sel, expected_candidate=False)
    assert _by_key(items_dev)["l5_vwap_deviation"].passed is False

    # Thiếu dữ liệu: < 8 nến trong ngày UTC
    _, items_w = _assert_parity(rel, _card006_snap(bars_count=5), sel, expected_candidate=False)
    assert _by_key(items_w)["indicator_warmup"].passed is None
    assert _by_key(items_w)["l5_vwap_deviation"].passed is None


def test_card_007_macd_adx_checklist() -> None:
    rel = _card_release_map()["SC-BINANCE-USDM-MACD-ADX-007"]
    sel = _selection(rel)

    for direction in ("long", "short"):
        cand, _ = _assert_parity(rel, _h21_snap(direction=direction), sel, expected_candidate=True)
        assert cand.direction == direction

    # Chưa đạt: L4 ADX < 22, L5 không cross MACD
    _, items_adx = _assert_parity(rel, _h21_snap(adx=16.0), sel, expected_candidate=False)
    assert _by_key(items_adx)["l4_adx_regime"].passed is False
    assert _by_key(items_adx)["l5_macd_trigger"].passed is None

    _, items_nocross = _assert_parity(rel, _h21_snap(cross=False), sel, expected_candidate=False)
    assert _by_key(items_nocross)["l5_macd_trigger"].passed is False

    # Thiếu dữ liệu: < 35 nến 1h
    _, items_w = _assert_parity(rel, _h21_snap(bars_count=20), sel, expected_candidate=False)
    assert _by_key(items_w)["indicator_warmup"].passed is None
    assert _by_key(items_w)["l5_macd_trigger"].passed is None


def test_card_008_taker_flow_checklist() -> None:
    rel = _card_release_map()["SC-BINANCE-USDM-TAKER-FLOW-008"]
    sel = _selection(rel)

    for direction in ("long", "short"):
        cand, _ = _assert_parity(
            rel, _card008_snap(direction=direction), sel, expected_candidate=True
        )
        assert cand.direction == direction

    # Chưa đạt: L4 ADX < 18, L5 fakeout (taker_buy ngược chiều) hoặc volume thấp
    _, items_adx = _assert_parity(rel, _card008_snap(adx=14.0), sel, expected_candidate=False)
    assert _by_key(items_adx)["l4_adx_regime"].passed is False

    _, items_fake = _assert_parity(
        rel, _card008_snap(taker_buy_last=40.0), sel, expected_candidate=False
    )
    assert _by_key(items_fake)["l5_flow_trigger"].passed is False

    _, items_vol = _assert_parity(rel, _card008_snap(vol_last=100.0), sel, expected_candidate=False)
    assert _by_key(items_vol)["l5_flow_trigger"].passed is False

    # Thiếu dữ liệu: thiếu taker_buy_base_volume hoặc < 21 nến
    _, items_no_taker = _assert_parity(
        rel, _card008_snap(missing_taker=True), sel, expected_candidate=False
    )
    assert _by_key(items_no_taker)["indicator_warmup"].passed is None
    assert _by_key(items_no_taker)["l5_flow_trigger"].passed is None

    _, items_w = _assert_parity(rel, _card008_snap(bars_count=15), sel, expected_candidate=False)
    assert _by_key(items_w)["indicator_warmup"].passed is None


def test_stale_feature_and_unclosed_bar_keep_parity_for_expanded_rules() -> None:
    snap_ok = _h02_snap()
    stale_rv = replace(snap_ok.features[1], quality=FeatureQuality.STALE)
    snap_stale_feat = replace(snap_ok, features=(snap_ok.features[0], stale_rv))
    _, items_stale_f = _assert_parity(
        "h02-v1", snap_stale_feat, _selection("h02-v1"), expected_candidate=False
    )
    assert _by_key(items_stale_f)["rv_regime"].passed is None

    unclosed = replace(
        snap_ok,
        series=(
            replace(
                snap_ok.series[0],
                bars=snap_ok.series[0].bars[:-1]
                + (replace(snap_ok.series[0].bars[-1], is_closed=False),),
            ),
        ),
    )
    _, items_unclosed = _assert_parity(
        "h02-v1", unclosed, _selection("h02-v1"), expected_candidate=False
    )
    assert _by_key(items_unclosed)["tsmom_horizon"].passed is None


@pytest.mark.parametrize(
    "release_id",
    [
        "h02-v1",
        "sc-binance-usdm-vol-breakout-001-v1.0.0",
        "sc-binance-usdm-tsmom-003-v1.0.0",
    ],
)
async def test_bot_dispatcher_persists_expanded_and_card_checklists(release_id: str) -> None:
    from tests.unit.test_bot_dispatcher import BOT_A, FixtureSnapshots, _context, _evidence

    from trading_bot.application.bot_dispatcher import (
        BotDecision,
        BotDecisionJob,
        BotDecisionStatus,
        evaluate_bot_decision,
    )

    snap = (
        _h02_snap() if release_id != "sc-binance-usdm-vol-breakout-001-v1.0.0" else _card001_snap()
    )
    ev = _evidence(
        observed_at=snap.decision_time - timedelta(minutes=1),
        available_at=snap.decision_time,
        validity_end=snap.decision_time + timedelta(hours=1),
    )
    context = _context(strategy_release_id=release_id)
    job = BotDecisionJob.create(evidence=ev, context=context, evaluated_at=ev.available_at)
    decision = await evaluate_bot_decision(
        evidence=ev,
        job=job,
        strategies=strategy_registry(),
        snapshots=FixtureSnapshots({BOT_A: snap}),
    )
    assert decision.status is BotDecisionStatus.CANDIDATE
    payload = decision.canonical_payload()
    assert payload["checklist"]["items"]
    assert all(item["passed"] is True for item in payload["checklist"]["items"])
    assert BotDecision.from_payload(payload, decision.decision_hash) == decision
