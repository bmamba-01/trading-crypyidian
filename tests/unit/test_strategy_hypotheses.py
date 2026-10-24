from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    FeatureObservation,
    FeatureQuality,
    MarketSeries,
    NoTrade,
    SeriesQuality,
    StrategyCandidate,
)
from trading_bot.domain.strategy.registry import StrategyRegistry
from trading_bot.domain.strategy.rules import (
    H01EmaTrendRule,
    H03DonchianBreakoutRule,
    H06RangeMeanReversionRule,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def _bar(
    index: int,
    timeframe: str,
    close: float,
    *,
    high: float | None = None,
    low: float | None = None,
    is_closed: bool = True,
) -> OHLCVBar:
    duration = {"15m": timedelta(minutes=15), "1h": timedelta(hours=1), "4h": timedelta(hours=4)}[
        timeframe
    ]
    opened = START + index * duration
    closed = opened + duration - timedelta(microseconds=1)
    price = Decimal(str(close))
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe=timeframe,
        open_time=opened,
        close_time=closed,
        event_at=closed,
        received_at=closed,
        available_at=closed,
        open=price,
        high=Decimal(str(high if high is not None else close + 1)),
        low=Decimal(str(low if low is not None else close - 1)),
        close=price,
        volume=Decimal("10"),
        is_closed=is_closed,
    )


def _feature(
    name: str,
    timeframe: str,
    value: float | None,
    *,
    previous: float | None = None,
    available_at: datetime | None = None,
    quality: FeatureQuality = FeatureQuality.READY,
) -> FeatureObservation:
    return FeatureObservation(
        feature_id=f"{timeframe}:{name}:{value}:{previous}",
        name=name,
        timeframe=timeframe,
        value=value,
        previous_value=previous,
        available_at=available_at or START + timedelta(days=10),
        quality=quality,
    )


def _snapshot(
    *,
    series: tuple[MarketSeries, ...],
    features: tuple[FeatureObservation, ...],
    position_is_flat: bool | None = True,
    macro_blackout: bool | None = False,
) -> DecisionSnapshot:
    latest = max(bar.available_at for item in series for bar in item.bars)
    interval_by_timeframe = {
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
    }
    qualified_series = tuple(
        replace(
            item,
            quality=SeriesQuality.READY,
            expected_interval=interval_by_timeframe[item.timeframe],
            max_event_age=2 * interval_by_timeframe[item.timeframe],
            backfill_complete=True,
        )
        for item in series
    )
    return DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=latest,
        snapshot_id="snapshot-test-v1",
        series=qualified_series,
        features=features,
        position_is_flat=position_is_flat,
        macro_blackout=macro_blackout,
        macro_evidence_ids=("macro-calendar-test-v1",),
        macro_evidence_available_at=latest,
        macro_is_fresh=True,
    )


def _h01_snapshot(
    *,
    ema20: tuple[float, float] = (101.0, 99.0),
    ema80: tuple[float, float] = (100.5, 100.0),
    adx: float = 20.0,
    h4_close: float = 110.0,
    h4_ema200: float = 100.0,
    quality: FeatureQuality = FeatureQuality.READY,
    feature_available_at: datetime | None = None,
    position_is_flat: bool = True,
) -> DecisionSnapshot:
    bars = (
        _bar(0, "1h", 100.0),
        _bar(1, "1h", 101.0),
    )
    h4 = (_bar(-1, "4h", h4_close),)
    decision = bars[-1].available_at
    return _snapshot(
        series=(MarketSeries("1h", bars), MarketSeries("4h", h4)),
        features=(
            _feature(
                "ema_20",
                "1h",
                ema20[0],
                previous=ema20[1],
                available_at=feature_available_at or decision,
                quality=quality,
            ),
            _feature(
                "ema_80",
                "1h",
                ema80[0],
                previous=ema80[1],
                available_at=feature_available_at or decision,
                quality=quality,
            ),
            _feature(
                "ema_200",
                "4h",
                h4_ema200,
                available_at=feature_available_at or decision,
                quality=quality,
            ),
            _feature(
                "adx_14", "1h", adx, available_at=feature_available_at or decision, quality=quality
            ),
            _feature(
                "atr_14", "1h", 2.0, available_at=feature_available_at or decision, quality=quality
            ),
        ),
        position_is_flat=position_is_flat,
    )


def _h03_snapshot(
    *, current_close: float = 101.5, quote_volume: float = 6.0, short: bool = False
) -> DecisionSnapshot:
    bars: list[OHLCVBar] = []
    for index in range(55):
        bars.append(
            _bar(
                index,
                "1h",
                96.0,
                high=100.0 if not short else 105.0,
                low=95.0,
            )
        )
    if short:
        bars.append(_bar(55, "1h", current_close, high=101.0, low=94.0))
    else:
        bars.append(_bar(55, "1h", current_close, high=102.0, low=94.0))
    volumes = (5.0,) * 55 + (quote_volume,)
    return _snapshot(
        series=(MarketSeries("1h", tuple(bars), quote_volumes=volumes),),
        features=(_feature("atr_14", "1h", 2.0, available_at=bars[-1].available_at),),
    )


def _h06_snapshot(
    *,
    adx: float = 17.9,
    efficiency: float = 0.249,
    z_score: float = -2.01,
    rsi_value: float = 29.9,
    current_close: float = 90.0,
    previous_floor: float = 85.0,
    macro_blackout: bool | None = False,
    position_is_flat: bool | None = True,
) -> DecisionSnapshot:
    bars = tuple(
        _bar(
            index,
            "15m",
            96.0 if index % 2 == 0 else 104.0,
            high=115.0,
            low=previous_floor,
        )
        for index in range(120)
    ) + (_bar(120, "15m", current_close, high=current_close + 1, low=current_close - 1),)
    decision = bars[-1].available_at
    features = (
        _feature("adx_14", "15m", adx, available_at=decision),
        _feature("efficiency_ratio_40", "15m", efficiency, available_at=decision),
        _feature("zscore_20", "15m", z_score, available_at=decision),
        _feature("rsi_14", "15m", rsi_value, available_at=decision),
        _feature("atr_14", "15m", 2.0, available_at=decision),
    )
    return _snapshot(
        series=(MarketSeries("15m", bars),),
        features=features,
        macro_blackout=macro_blackout,
        position_is_flat=position_is_flat,
    )


def test_h01_exact_long_gate_and_exit_recipe() -> None:
    result = H01EmaTrendRule().evaluate(_h01_snapshot(), _settings("h01-v1"))

    assert isinstance(result, StrategyCandidate)
    assert result.direction == "long"
    assert result.reason_code == "H01_LONG_ENTRY"
    assert result.evidence_feature_ids == (
        "1h:ema_20:101.0:99.0",
        "1h:ema_80:100.5:100.0",
        "4h:ema_200:100.0:None",
        "1h:adx_14:20.0:None",
        "1h:atr_14:2.0:None",
    )
    assert result.recipe.stop_atr_multiple == 2.0
    assert result.recipe.trailing_atr_multiple == 3.0
    assert result.recipe.opposite_signal_exit
    assert result.recipe.max_hold_bars == 120
    assert result.eligible_after > result.signal_at
    assert H01EmaTrendRule().evaluate(_h01_snapshot(), _settings("h01-v1")) == result


def test_h01_short_cross_requires_lower_closed_four_hour_trend() -> None:
    result = H01EmaTrendRule().evaluate(
        _h01_snapshot(
            ema20=(99.0, 101.0),
            ema80=(100.0, 100.5),
            h4_close=90.0,
            h4_ema200=100.0,
        ),
        _settings("h01-v1"),
    )

    assert isinstance(result, StrategyCandidate)
    assert result.direction == "short"
    assert result.recipe.opposite_signal_exit
    assert len(result.evidence_bar_keys) == 3


@pytest.mark.parametrize(
    ("ema20", "ema80", "adx", "h4_close", "h4_ema200", "expected_code"),
    [
        ((101.0, 100.1), (100.5, 100.0), 25.0, 110.0, 100.0, "H01_NO_CROSS"),
        ((101.0, 99.0), (100.5, 100.0), 19.99, 110.0, 100.0, "H01_ADX_BELOW_20"),
        ((101.0, 99.0), (100.5, 100.0), 20.0, 99.0, 100.0, "H01_HTF_TREND_MISMATCH"),
    ],
)
def test_h01_rejects_when_any_exact_gate_fails(
    ema20: tuple[float, float],
    ema80: tuple[float, float],
    adx: float,
    h4_close: float,
    h4_ema200: float,
    expected_code: str,
) -> None:
    result = H01EmaTrendRule().evaluate(
        _h01_snapshot(
            ema20=ema20,
            ema80=ema80,
            adx=adx,
            h4_close=h4_close,
            h4_ema200=h4_ema200,
        ),
        _settings("h01-v1"),
    )

    assert isinstance(result, NoTrade)
    assert result.code == expected_code


def test_h01_rejects_unavailable_feature_and_open_higher_timeframe_bar() -> None:
    future = START + timedelta(days=20)
    result = H01EmaTrendRule().evaluate(
        _h01_snapshot(feature_available_at=future), _settings("h01-v1")
    )
    assert isinstance(result, NoTrade)
    assert result.code == "H01_INPUT_UNAVAILABLE"

    snapshot = _h01_snapshot()
    open_bar = _bar(3, "4h", 110.0, is_closed=False)
    updated = DecisionSnapshot(
        symbol=snapshot.symbol,
        decision_time=snapshot.decision_time,
        series=(snapshot.get_series("1h"), MarketSeries("4h", (open_bar,))),
        features=snapshot.features,
        snapshot_id=snapshot.snapshot_id,
        position_is_flat=snapshot.position_is_flat,
        macro_blackout=snapshot.macro_blackout,
        macro_evidence_ids=snapshot.macro_evidence_ids,
        macro_evidence_available_at=snapshot.macro_evidence_available_at,
        macro_is_fresh=snapshot.macro_is_fresh,
    )
    result = H01EmaTrendRule().evaluate(updated, _settings("h01-v1"))
    assert isinstance(result, NoTrade)
    assert result.code == "H01_BAR_NOT_CLOSED"


@pytest.mark.parametrize(
    ("short", "close", "direction"),
    [(False, 101.5, "long"), (True, 94.5, "short")],
)
def test_h03_uses_prior_55_bar_channel_prior_20_volume_and_exit_recipe(
    short: bool, close: float, direction: str
) -> None:
    result = H03DonchianBreakoutRule().evaluate(
        _h03_snapshot(short=short, current_close=close), _settings("h03-v1")
    )

    assert isinstance(result, StrategyCandidate)
    assert result.direction == direction
    assert result.recipe.stop_atr_multiple == 2.0
    assert result.recipe.trailing_atr_multiple == 3.0
    assert result.recipe.exit_channel_period == 20
    assert result.recipe.max_hold_bars == 120
    assert result.eligible_after > result.signal_at


def test_h03_excludes_current_bar_from_channel_and_requires_strict_volume_gate() -> None:
    not_breakout = _h03_snapshot(current_close=100.0)
    result = H03DonchianBreakoutRule().evaluate(not_breakout, _settings("h03-v1"))
    assert isinstance(result, NoTrade)
    assert result.code == "H03_NO_BREAKOUT"

    low_volume = _h03_snapshot(current_close=101.5, quote_volume=5.0)
    result = H03DonchianBreakoutRule().evaluate(low_volume, _settings("h03-v1"))
    assert isinstance(result, NoTrade)
    assert result.code == "H03_VOLUME_NOT_ABOVE_MEDIAN"


def test_h03_runtime_parameters_change_channel_and_volume_gates() -> None:
    from dataclasses import replace

    base = _h03_snapshot(current_close=101.5)
    series = base.get_series("1h")
    assert series is not None
    old_high = replace(series.bars[10], high=Decimal("110"))
    channel_snapshot = replace(
        base,
        series=(replace(series, bars=series.bars[:10] + (old_high,) + series.bars[11:]),),
    )
    default = H03DonchianBreakoutRule().evaluate(channel_snapshot, _settings("h03-v1"))
    shorter = H03DonchianBreakoutRule().evaluate(
        channel_snapshot, _settings("h03-v1", donchian_lookback=20)
    )
    assert isinstance(default, NoTrade)
    assert default.code == "H03_NO_BREAKOUT"
    assert isinstance(shorter, StrategyCandidate)

    volumes = (5.0,) * 40 + (10.0,) * 10 + (5.0,) * 5 + (6.0,)
    volume_snapshot = replace(
        base,
        series=(replace(series, quote_volumes=volumes),),
    )
    default_volume = H03DonchianBreakoutRule().evaluate(volume_snapshot, _settings("h03-v1"))
    shorter_volume = H03DonchianBreakoutRule().evaluate(
        volume_snapshot, _settings("h03-v1", volume_median_lookback=5)
    )
    assert isinstance(default_volume, NoTrade)
    assert default_volume.code == "H03_VOLUME_NOT_ABOVE_MEDIAN"
    assert isinstance(shorter_volume, StrategyCandidate)


@pytest.mark.parametrize(
    "parameters",
    [
        {"unknown": 1},
        {"donchian_lookback": 19},
        {"donchian_lookback": 201},
        {"volume_median_lookback": 4},
        {"volume_median_lookback": 101},
        {"donchian_lookback": True},
    ],
)
def test_h03_runtime_parameters_fail_closed(parameters: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="parameter"):
        _settings("h03-v1", **parameters)


@pytest.mark.parametrize(
    ("adx", "efficiency", "z_score", "rsi_value", "expected_code"),
    [
        (18.0, 0.1, -2.5, 20.0, "H06_NOT_RANGE"),
        (17.0, 0.25, -2.5, 20.0, "H06_NOT_RANGE"),
        (17.0, 0.2, -2.0, 20.0, "H06_NO_EXTREME"),
        (17.0, 0.2, -2.5, 30.0, "H06_NO_EXTREME"),
    ],
)
def test_h06_uses_strict_range_and_long_thresholds(
    adx: float,
    efficiency: float,
    z_score: float,
    rsi_value: float,
    expected_code: str,
) -> None:
    result = H06RangeMeanReversionRule().evaluate(
        _h06_snapshot(
            adx=adx,
            efficiency=efficiency,
            z_score=z_score,
            rsi_value=rsi_value,
        ),
        _settings("h06-v1"),
    )

    assert isinstance(result, NoTrade)
    assert result.code == expected_code


def test_h06_enforces_macro_adverse_break_and_no_scale_in_gates() -> None:
    rule = H06RangeMeanReversionRule()
    cases = (
        (_h06_snapshot(macro_blackout=True), "H06_MACRO_BLACKOUT"),
        (_h06_snapshot(current_close=80.0, previous_floor=90.0), "H06_ADVERSE_RANGE_BREAK"),
        (_h06_snapshot(position_is_flat=False), "H06_POSITION_NOT_FLAT"),
    )

    for snapshot, expected_code in cases:
        result = rule.evaluate(snapshot, _settings("h06-v1"))
        assert isinstance(result, NoTrade)
        assert result.code == expected_code


def test_h06_long_recipe_pins_mean_exit_stop_target_time_and_scale_in() -> None:
    result = H06RangeMeanReversionRule().evaluate(_h06_snapshot(), _settings("h06-v1"))

    assert isinstance(result, StrategyCandidate)
    assert result.direction == "long"
    assert result.recipe.stop_atr_multiple == 1.5
    assert result.recipe.take_profit_atr_multiple == 2.0
    assert result.recipe.max_hold_bars == 16
    assert result.recipe.allow_scale_in is False
    assert result.recipe.exit_feature_name == "zscore_20"
    assert result.recipe.exit_feature_operator == ">="
    assert result.recipe.exit_feature_threshold == -0.2


def test_h06_short_uses_positive_mean_reversion_threshold() -> None:
    result = H06RangeMeanReversionRule().evaluate(
        _h06_snapshot(z_score=2.01, rsi_value=70.1, current_close=114.5),
        _settings("h06-v1"),
    )

    assert isinstance(result, StrategyCandidate)
    assert result.direction == "short"
    assert result.recipe.exit_feature_operator == "<="
    assert result.recipe.exit_feature_threshold == 0.2


def test_rules_reject_bar_close_after_decision_time_even_if_available_timestamp_is_old() -> None:
    snapshot = _h01_snapshot()
    series = snapshot.get_series("1h")
    assert series is not None
    bars = list(series.bars)
    bars[-1] = replace(bars[-1], close_time=snapshot.decision_time + timedelta(hours=1))
    updated = replace(
        snapshot,
        series=(replace(series, bars=tuple(bars)), snapshot.get_series("4h")),
    )

    result = H01EmaTrendRule().evaluate(updated, _settings("h01-v1"))

    assert isinstance(result, NoTrade)
    assert result.code == "H01_INPUT_UNAVAILABLE"
    assert result.gate_details


def test_rules_fail_closed_for_unknown_series_quality_and_stale_or_gapped_series() -> None:
    snapshot = _h03_snapshot()
    series = snapshot.get_series("1h")
    assert series is not None

    unknown = replace(snapshot, series=(replace(series, quality=SeriesQuality.UNKNOWN),))
    unknown_result = H03DonchianBreakoutRule().evaluate(unknown, _settings("h03-v1"))
    assert isinstance(unknown_result, NoTrade)
    assert unknown_result.code == "H03_SERIES_QUALITY_UNKNOWN"

    unconfirmed = replace(
        snapshot,
        series=(replace(series, backfill_complete=False),),
    )
    incomplete_result = H03DonchianBreakoutRule().evaluate(unconfirmed, _settings("h03-v1"))
    assert isinstance(incomplete_result, NoTrade)
    assert incomplete_result.code == "H03_SERIES_INCOMPLETE"

    wrong_interval = replace(
        snapshot,
        series=(replace(series, expected_interval=timedelta(minutes=30)),),
    )
    wrong_interval_result = H03DonchianBreakoutRule().evaluate(wrong_interval, _settings("h03-v1"))
    assert isinstance(wrong_interval_result, NoTrade)
    assert wrong_interval_result.code == "H03_SERIES_INCOMPLETE"

    marked_stale = replace(snapshot, series=(replace(series, quality=SeriesQuality.STALE),))
    marked_stale_result = H03DonchianBreakoutRule().evaluate(marked_stale, _settings("h03-v1"))
    assert isinstance(marked_stale_result, NoTrade)
    assert marked_stale_result.code == "H03_SERIES_STALE"

    stale = replace(snapshot, decision_time=snapshot.decision_time + timedelta(days=2))
    stale_result = H03DonchianBreakoutRule().evaluate(stale, _settings("h03-v1"))
    assert isinstance(stale_result, NoTrade)
    assert stale_result.code == "H03_SERIES_STALE"

    bars = list(series.bars)
    for index in range(30, len(bars)):
        bars[index] = replace(
            bars[index],
            open_time=bars[index].open_time + timedelta(hours=1),
            close_time=bars[index].close_time + timedelta(hours=1),
            event_at=bars[index].event_at + timedelta(hours=1),
            received_at=bars[index].received_at + timedelta(hours=1),
            available_at=bars[index].available_at + timedelta(hours=1),
        )
    gapped = replace(
        snapshot,
        decision_time=bars[-1].available_at,
        series=(replace(series, bars=tuple(bars)),),
    )
    gap_result = H03DonchianBreakoutRule().evaluate(gapped, _settings("h03-v1"))
    assert isinstance(gap_result, NoTrade)
    assert gap_result.code == "H03_SERIES_INCOMPLETE"


def test_unknown_position_and_macro_state_never_become_favorable_defaults() -> None:
    h01 = _h01_snapshot()
    position_unknown = replace(h01, position_is_flat=None)
    position_result = H01EmaTrendRule().evaluate(position_unknown, _settings("h01-v1"))
    assert isinstance(position_result, NoTrade)
    assert position_result.code == "H01_POSITION_UNKNOWN"

    h06 = _h06_snapshot(macro_blackout=None)
    macro_result = H06RangeMeanReversionRule().evaluate(h06, _settings("h06-v1"))
    assert isinstance(macro_result, NoTrade)
    assert macro_result.code == "H06_MACRO_UNKNOWN"


def test_h06_accepts_feature_snapshot_names_and_pins_snapshot_settings_release_and_evidence() -> (
    None
):
    snapshot = _h06_snapshot()
    renamed = tuple(
        replace(
            item,
            name={"er_40": "efficiency_ratio_40", "z_20": "zscore_20"}.get(item.name, item.name),
        )
        for item in snapshot.features
    )
    snapshot = replace(snapshot, features=renamed)

    result = H06RangeMeanReversionRule().evaluate(snapshot, _settings("h06-v1"))

    assert isinstance(result, StrategyCandidate)
    assert result.settings_version_id == "settings-v1"
    assert result.snapshot_id == snapshot.snapshot_id
    assert len(result.release_content_hash) == 64
    assert result.evidence_bar_keys
    assert len(result.evidence_bar_keys) == 121
    assert result.evidence_bar_keys[-1].startswith("BTCUSDT|15m|")
    assert result.macro_evidence_ids == snapshot.macro_evidence_ids
    assert any("zscore_20" in feature_id for feature_id in result.evidence_feature_ids)
    assert result.release_content_hash == StrategyRegistry().get_release("h06-v1").content_hash


def test_gate_rejection_records_machine_readable_gate_details() -> None:
    result = H01EmaTrendRule().evaluate(_h01_snapshot(adx=19.0), _settings("h01-v1"))

    assert isinstance(result, NoTrade)
    assert result.code == "H01_ADX_BELOW_20"
    assert result.gate_details == ("adx_14=19.0; required >= 20",)


@pytest.mark.parametrize("future_field", ["event_at", "available_at"])
def test_rules_reject_future_event_and_availability_timestamps(future_field: str) -> None:
    snapshot = _h01_snapshot()
    series = snapshot.get_series("1h")
    assert series is not None
    bars = list(series.bars)
    future = snapshot.decision_time + timedelta(minutes=1)
    if future_field == "event_at":
        bars[-1] = replace(bars[-1], event_at=future, received_at=future, available_at=future)
    else:
        bars[-1] = replace(bars[-1], received_at=future, available_at=future)
    updated = replace(
        snapshot, series=(replace(series, bars=tuple(bars)), snapshot.get_series("4h"))
    )

    result = H01EmaTrendRule().evaluate(updated, _settings("h01-v1"))

    assert isinstance(result, NoTrade)
    assert result.code == "H01_INPUT_UNAVAILABLE"


def test_h06_requires_macro_calendar_evidence_even_when_state_says_clear() -> None:
    snapshot = replace(_h06_snapshot(), macro_evidence_ids=())

    result = H06RangeMeanReversionRule().evaluate(snapshot, _settings("h06-v1"))

    assert isinstance(result, NoTrade)
    assert result.code == "H06_MACRO_UNKNOWN"

    future = replace(
        _h06_snapshot(),
        macro_evidence_available_at=_h06_snapshot().decision_time + timedelta(minutes=1),
    )
    future_result = H06RangeMeanReversionRule().evaluate(future, _settings("h06-v1"))
    assert isinstance(future_result, NoTrade)
    assert future_result.code == "H06_MACRO_UNKNOWN"

    stale = replace(_h06_snapshot(), macro_is_fresh=False)
    stale_result = H06RangeMeanReversionRule().evaluate(stale, _settings("h06-v1"))
    assert isinstance(stale_result, NoTrade)
    assert stale_result.code == "H06_MACRO_STALE"


def test_stale_feature_quality_is_a_no_trade_with_feature_details() -> None:
    snapshot = _h01_snapshot(quality=FeatureQuality.STALE)

    result = H01EmaTrendRule().evaluate(snapshot, _settings("h01-v1"))

    assert isinstance(result, NoTrade)
    assert result.code == "H01_FEATURE_UNAVAILABLE"
    assert result.missing_features
    assert result.gate_details

    old_snapshot = _h01_snapshot(feature_available_at=START - timedelta(days=1))
    old_result = H01EmaTrendRule().evaluate(old_snapshot, _settings("h01-v1"))
    assert isinstance(old_result, NoTrade)
    assert old_result.code == "H01_FEATURE_STALE"


def _settings(release_id: str, **parameters: int):
    from trading_bot.domain.strategy.contracts import StrategySelection

    return StrategySelection(
        "settings-v1",
        (release_id,),
        published=True,
        parameters=tuple(sorted(parameters.items())),
    )
