"""SIG-4 lát 1: H18–H20 scalping M5 — điều kiện vào, bộ lọc chung, thang chốt lời theo release."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    CostModel,
    PlanRejected,
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
from trading_bot.domain.strategy.rules import release_content_hash
from trading_bot.domain.strategy.scalping_rules import (
    SCALPING_LADDER,
    SCALPING_RULES,
    H18SweepReclaimRule,
    H19VwapRangeReversionRule,
    H20BreakRetestRule,
    _near_funding,
    ladder_for_release,
)

FIVE = timedelta(minutes=5)
FIFTEEN = timedelta(minutes=15)
# Thứ Hai 16/11/2026: 00:00 UTC mở nến đầu; nến cuối đóng 10:05 UTC (phiên London).
DAY = datetime(2026, 11, 16, tzinfo=UTC)
SETTINGS = StrategySelection("settings-sig4", ("h18-v1", "h19-v1", "h20-v1"), published=True)


def _bar(
    timeframe: str,
    interval: timedelta,
    opened: datetime,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
    volume: float = 10.0,
    taker: float | None = 5.0,
) -> OHLCVBar:
    closed = opened + interval - timedelta(microseconds=1)
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe=timeframe,
        open_time=opened,
        close_time=closed,
        event_at=closed,
        received_at=closed,
        available_at=closed,
        open=Decimal(str(open_price)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal(str(volume)),
        is_closed=True,
        taker_buy_base_volume=None if taker is None else Decimal(str(taker)),
    )


def _flat(timeframe: str, interval: timedelta, start: datetime, count: int) -> list[OHLCVBar]:
    return [
        _bar(
            timeframe,
            interval,
            start + interval * index,
            open_price=100.0,
            high=100.2,
            low=99.8,
            close=100.0,
            taker=5.0 + (index % 3) * 0.2,
        )
        for index in range(count)
    ]


def _code(result: StrategyCandidate | NoTrade) -> str:
    assert isinstance(result, NoTrade), result
    return result.code


def _feature(name: str, timeframe: str, value: float, at: datetime) -> FeatureObservation:
    return FeatureObservation(f"{timeframe}-{name}", name, timeframe, value, value, at)


def _series(timeframe: str, interval: timedelta, bars: list[OHLCVBar]) -> MarketSeries:
    return MarketSeries(
        timeframe,
        tuple(bars),
        quality=SeriesQuality.READY,
        expected_interval=interval,
        max_event_age=interval * 2,
        backfill_complete=True,
    )


def _snapshot(
    bars: list[OHLCVBar],
    features: dict[str, float],
    *,
    context: list[OHLCVBar] | None = None,
    flat: bool | None = True,
    macro_blackout: bool | None = False,
) -> DecisionSnapshot:
    decision = bars[-1].close_time + timedelta(seconds=1)
    series = [_series("5m", FIVE, bars)]
    observations = [
        _feature(name, "5m", value, bars[-1].close_time) for name, value in features.items()
    ]
    if context is not None:
        series.append(_series("15m", FIFTEEN, context))
        observations.append(_feature("atr_14", "15m", 1.0, context[-1].close_time))
    return DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=decision,
        snapshot_id="snap-sig4",
        series=tuple(series),
        features=tuple(observations),
        position_is_flat=flat,
        macro_blackout=macro_blackout,
    )


# --- H18 ---------------------------------------------------------------------------------------


def _h18_bars(*, taker: float = 4.0, last_open: datetime | None = None) -> list[OHLCVBar]:
    """Phiên Á (00:00–09:00 UTC) có đỉnh 100,5; nến cuối quét lên 101 rồi đóng 100,1."""
    start = DAY if last_open is None else last_open - FIVE * 120
    bars = _flat("5m", FIVE, start, 120)
    bars[48] = replace(bars[48], high=Decimal("100.5"))
    bars.append(
        _bar(
            "5m",
            FIVE,
            start + FIVE * 120,
            open_price=100.0,
            high=101.0,
            low=99.9,
            close=100.1,
            taker=taker,
        )
    )
    return bars


def test_h18_short_after_sweep_and_reclaim_of_asia_high() -> None:
    result = H18SweepReclaimRule().evaluate(_snapshot(_h18_bars(), {"atr_14": 0.5}), SETTINGS)
    assert isinstance(result, StrategyCandidate), result
    assert result.direction == "short"
    assert result.strategy_release_id == "h18-v1"
    # stop = râu 101,0 − đóng 100,1 + 0,1 × ATR 0,5 = 0,95 → bội số 1,9 ATR.
    assert result.recipe.stop_atr_multiple == pytest.approx(1.9)
    assert result.recipe.max_hold_bars == 12
    assert result.release_content_hash == release_content_hash(H18SweepReclaimRule())


def test_h18_requires_reversed_delta_and_taker_data() -> None:
    rule = H18SweepReclaimRule()
    buying = rule.evaluate(_snapshot(_h18_bars(taker=7.0), {"atr_14": 0.5}), SETTINGS)
    assert _code(buying) == "H18_DELTA_NOT_REVERSED"
    missing = rule.evaluate(_snapshot(_h18_bars(taker=None), {"atr_14": 0.5}), SETTINGS)  # type: ignore[arg-type]
    assert _code(missing) == "H18_TAKER_UNAVAILABLE"


def test_h18_without_sweep_is_no_trade() -> None:
    bars = _h18_bars()
    bars[-1] = replace(bars[-1], high=Decimal("100.3"))
    result = H18SweepReclaimRule().evaluate(_snapshot(bars, {"atr_14": 0.5}), SETTINGS)
    assert _code(result) == "H18_NO_SWEEP"


# --- Bộ lọc chung --------------------------------------------------------------------------------


def test_common_gates_session_funding_position_macro_and_cost() -> None:
    rule = H18SweepReclaimRule()
    # Nến cuối mở 15:50 UTC, đóng 15:55 (phiên London) → trong ±10 phút quanh funding 16:00.
    near_funding = rule.evaluate(
        _snapshot(_h18_bars(last_open=DAY + timedelta(hours=15, minutes=50)), {"atr_14": 0.5}),
        SETTINGS,
    )
    assert _code(near_funding) == "H18_FUNDING_WINDOW"
    # 21:00 UTC: London và New York đều đã đóng.
    late = rule.evaluate(
        _snapshot(_h18_bars(last_open=DAY + timedelta(hours=21)), {"atr_14": 0.5}), SETTINGS
    )
    assert _code(late) == "H18_OUT_OF_SESSION"
    busy = rule.evaluate(_snapshot(_h18_bars(), {"atr_14": 0.5}, flat=False), SETTINGS)
    assert _code(busy) == "H18_POSITION_NOT_FLAT"
    macro = rule.evaluate(_snapshot(_h18_bars(), {"atr_14": 0.5}, macro_blackout=True), SETTINGS)
    assert _code(macro) == "H18_MACRO_BLACKOUT"
    bars = _h18_bars()
    bars[-1] = replace(bars[-1], high=Decimal("100.6"), close=Decimal("100.45"))
    tight = rule.evaluate(_snapshot(bars, {"atr_14": 0.5}), SETTINGS)
    # 0,15 + 0,05 = 0,2 < 3 × 0,1% × 100,45 ≈ 0,30.
    assert _code(tight) == "H18_STOP_TOO_TIGHT"
    unselected = rule.evaluate(
        _snapshot(_h18_bars(), {"atr_14": 0.5}),
        StrategySelection("settings-x", ("h19-v1",), published=True),
    )
    assert _code(unselected) == "H18_NOT_SELECTED"


def test_funding_window_boundaries() -> None:
    assert _near_funding(DAY + timedelta(minutes=10))
    assert not _near_funding(DAY + timedelta(minutes=10, seconds=1))
    assert _near_funding(DAY + timedelta(hours=15, minutes=50))
    assert _near_funding(DAY - timedelta(minutes=5))
    assert not _near_funding(DAY + timedelta(hours=12))


# --- H19 ---------------------------------------------------------------------------------------


def _h19_bars(close: float = 101.5) -> list[OHLCVBar]:
    bars = [
        _bar(
            "5m",
            FIVE,
            DAY + FIVE * index,
            open_price=100.0,
            high=100.2,
            low=99.8,
            close=99.9 + (index % 2) * 0.2,
        )
        for index in range(120)
    ]
    bars.append(
        _bar(
            "5m",
            FIVE,
            DAY + FIVE * 120,
            open_price=100.2,
            high=max(close + 0.1, 100.3),
            low=min(close - 0.5, 100.0),
            close=close,
        )
    )
    return bars


H19_FEATURES = {"atr_14": 0.5, "adx_14": 15.0, "efficiency_ratio_40": 0.2}


def test_h19_fades_two_sigma_stretch_from_day_vwap_in_range() -> None:
    result = H19VwapRangeReversionRule().evaluate(_snapshot(_h19_bars(), H19_FEATURES), SETTINGS)
    assert isinstance(result, StrategyCandidate), result
    assert result.direction == "short"
    assert result.recipe.stop_atr_multiple == 1.0
    long = H19VwapRangeReversionRule().evaluate(
        _snapshot(_h19_bars(close=98.5), H19_FEATURES), SETTINGS
    )
    assert isinstance(long, StrategyCandidate)
    assert long.direction == "long"


def test_h19_rejects_trend_and_small_deviation() -> None:
    rule = H19VwapRangeReversionRule()
    trending = rule.evaluate(_snapshot(_h19_bars(), {**H19_FEATURES, "adx_14": 25.0}), SETTINGS)
    assert _code(trending) == "H19_NOT_RANGE"
    calm = rule.evaluate(_snapshot(_h19_bars(close=100.05), H19_FEATURES), SETTINGS)
    assert _code(calm) == "H19_NO_EXTREME"


# --- H20 ---------------------------------------------------------------------------------------


def _impulse(
    timeframe: str,
    interval: timedelta,
    start: datetime,
    flat_count: int,
    *,
    break_taker: float = 9.5,
    retest_low: float = 101.05,
) -> list[OHLCVBar]:
    """Đi ngang → đỉnh swing 101 → đáy swing 100 → BOS đóng 101,3 → retest giữ trên 101."""
    bars = _flat(timeframe, interval, start, flat_count)
    path = [
        (100.2, 100.4, 100.1, 100.3),
        (100.3, 100.6, 100.2, 100.5),
        (100.5, 100.7, 100.4, 100.6),
        (100.6, 101.0, 100.5, 100.8),  # đỉnh swing 101,0
        (100.7, 100.75, 100.4, 100.5),
        (100.5, 100.5, 100.2, 100.3),
        (100.3, 100.3, 100.1, 100.2),
        (100.2, 100.2, 100.0, 100.1),  # đáy swing 100,0
        (100.1, 100.5, 100.1, 100.4),
        (100.4, 100.7, 100.3, 100.6),
        (100.6, 100.9, 100.5, 100.8),
        (100.8, 101.4, 100.8, 101.3),  # BOS: đóng trên 101,0
    ]
    for offset, (open_price, high, low, close) in enumerate(path):
        taker = break_taker if offset == len(path) - 1 else 5.0
        bars.append(
            _bar(
                timeframe,
                interval,
                start + interval * (flat_count + offset),
                open_price=open_price,
                high=high,
                low=low,
                close=close,
                taker=taker,
            )
        )
    bars.append(
        _bar(
            timeframe,
            interval,
            start + interval * (flat_count + len(path)),
            open_price=101.3,
            high=101.35,
            low=retest_low,
            close=101.2,
        )
    )
    return bars


def _h20_snapshot(**kwargs: float) -> DecisionSnapshot:
    bars = _impulse("5m", FIVE, DAY + timedelta(hours=5), 60, **kwargs)
    context_end = bars[-1].close_time + timedelta(microseconds=1)
    context_start = context_end - FIFTEEN * 73
    context = _impulse("15m", FIFTEEN, context_start, 60)
    return _snapshot(bars, {"atr_14": 0.5}, context=context)


def test_h20_long_on_bos_retest_with_strong_delta_and_m15_trend() -> None:
    result = H20BreakRetestRule().evaluate(_h20_snapshot(), SETTINGS)
    assert isinstance(result, StrategyCandidate), result
    assert result.direction == "long"
    # Stop = đáy swing 100,0 → khoảng 1,2 = 2,4 ATR.
    assert result.recipe.stop_atr_multiple == pytest.approx(2.4)
    assert len(result.evidence_bar_keys) == 2


def test_h20_rejects_weak_break_and_failed_retest() -> None:
    rule = H20BreakRetestRule()
    weak = rule.evaluate(_h20_snapshot(break_taker=5.2), SETTINGS)
    assert _code(weak) == "H20_WEAK_BREAK"
    deep = rule.evaluate(_h20_snapshot(retest_low=100.6), SETTINGS)
    assert _code(deep) == "H20_NO_RETEST"


def test_h20_requires_m15_context() -> None:
    bars = _impulse("5m", FIVE, DAY + timedelta(hours=5), 60)
    result = H20BreakRetestRule().evaluate(_snapshot(bars, {"atr_14": 0.5}), SETTINGS)
    assert _code(result) == "H20_INSUFFICIENT_BARS"


# --- Đăng ký release và thang chốt lời -----------------------------------------------------------


def test_scalping_releases_register_with_ladder_and_keep_built_in_hashes() -> None:
    from trading_bot.application.strategy_catalog import strategy_registry

    product = strategy_registry()
    for rule in SCALPING_RULES:
        release = product.get_release(rule.release_id)
        assert release.status == "EXPERIMENTAL_UNVALIDATED"
        assert release.content_hash == release_content_hash(rule)
        assert ladder_for_release(rule.release_id) is SCALPING_LADDER
    for built_in in StrategyRegistry().list_releases():
        assert product.get_release(built_in.release_id).content_hash == built_in.content_hash
        assert ladder_for_release(built_in.release_id) is None


def test_scalping_requirements_are_declared() -> None:
    from trading_bot.application.strategy_market_requirements import (
        ENTRY_TIMEFRAME_BY_RELEASE,
        FEATURES_BY_RELEASE,
    )

    assert ENTRY_TIMEFRAME_BY_RELEASE["h18-v1"] == "5m"
    assert set(FEATURES_BY_RELEASE["h20-v1"]) == {"5m", "15m"}
    assert FEATURES_BY_RELEASE["h19-v1"]["5m"] == ("atr_14", "adx_14", "efficiency_ratio_40")


COST = CostModel("vip0", Decimal("0.0005"), Decimal("0.0005"), Decimal("0"), Decimal("0"))


def _plan_h18(stop_high: str) -> TradePlan | PlanRejected:
    from test_paper_execution import _settings

    bars = _h18_bars()
    bars[-1] = replace(bars[-1], high=Decimal(stop_high))
    candidate = H18SweepReclaimRule().evaluate(_snapshot(bars, {"atr_14": 0.5}), SETTINGS)
    assert isinstance(candidate, StrategyCandidate), candidate
    return TradePlanner().plan(
        replace(candidate, settings_version_id="settings-v1"),
        replace(
            _settings(),
            active_release_ids=("h18-v1",),
            release_content_hashes=(("h18-v1", candidate.release_content_hash),),
        ),
        AccountSnapshot(
            "workspace-a",
            "paper-a",
            "settings-v1",
            candidate.signal_at,
            Decimal("10000"),
            Decimal("1000"),
            True,
        ),
        (),
        COST,
        now=candidate.eligible_after,
        ladder=ladder_for_release(candidate.strategy_release_id),
    )


def test_ladder_plan_after_fees_for_scalping_candidate() -> None:
    plan = _plan_h18("101.0")
    if isinstance(plan, PlanRejected):
        pytest.fail(f"{plan.reason_code}: {plan.detail_vi}")
    assert [target.r_multiple for target in plan.targets] == [Decimal("1"), Decimal("2")]
    assert plan.targets[0].stop_to_entry_after is True
    assert plan.target_price == plan.targets[-1].price
