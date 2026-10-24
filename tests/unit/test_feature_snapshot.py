from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from trading_bot.domain.market.feature_snapshot import (
    FeatureCalculator,
    FeatureSettings,
    MarketRegime,
    feature_schema_hash,
)
from trading_bot.domain.market.models import MarketSnapshot, OHLCVBar

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)
INTERVAL = timedelta(days=1)


def make_bars(
    closes: list[float],
    *,
    start: datetime = BASE_TIME,
    spread: float = 1.0,
    interval: timedelta = INTERVAL,
) -> tuple[OHLCVBar, ...]:
    bars = []
    for index, close in enumerate(closes):
        open_time = start + index * interval
        close_time = open_time + interval
        price = Decimal(str(close))
        half_spread = Decimal(str(spread))
        bars.append(
            OHLCVBar(
                symbol="BTCUSDT",
                timeframe="1d" if interval == timedelta(days=1) else "1h",
                open_time=open_time,
                close_time=close_time,
                event_at=close_time,
                received_at=close_time + timedelta(minutes=1),
                available_at=close_time + timedelta(minutes=1),
                open=price,
                high=price + half_spread,
                low=price - half_spread,
                close=price,
                volume=Decimal("10"),
                is_closed=True,
                source="fixture",
                checksum=f"checksum-{index}",
            )
        )
    return tuple(bars)


def make_settings(**changes: object) -> FeatureSettings:
    values: dict[str, object] = {
        "code_hash": "a" * 64,
        "expected_interval": INTERVAL,
        "max_event_age": INTERVAL + timedelta(hours=1),
        "regime_history": timedelta(days=2),
        "sma_period": 2,
        "ema_periods": (2,),
        "atr_period": 2,
        "rsi_period": 2,
        "adx_period": 2,
        "bollinger_period": 2,
        "realized_volatility_period": 2,
        "efficiency_ratio_period": 2,
        "zscore_period": 2,
    }
    values.update(changes)
    return FeatureSettings(**values)  # type: ignore[arg-type]


def calculate(
    closes: list[float],
    *,
    settings: FeatureSettings | None = None,
    bars: tuple[OHLCVBar, ...] | None = None,
):
    source_bars = bars or make_bars(closes)
    snapshot = MarketSnapshot(as_of=source_bars[-1].available_at, bars=source_bars)
    return FeatureCalculator(settings or make_settings()).calculate(
        snapshot,
        workspace_id=WORKSPACE_ID,
        input_event_ids=tuple(f"event-{index}" for index in range(len(source_bars))),
        computed_at=snapshot.as_of + timedelta(seconds=1),
    )


def test_feature_snapshot_preserves_exact_input_lineage_and_is_immutable() -> None:
    source_bars = make_bars([100, 101, 102, 103, 104, 105, 106])
    snapshot = calculate([100], bars=source_bars)

    assert snapshot.workspace_id == WORKSPACE_ID
    assert snapshot.symbol == "BTCUSDT"
    assert snapshot.timeframe == "1d"
    assert snapshot.input_event_ids == tuple(f"event-{i}" for i in range(7))
    assert len(snapshot.input_bar_ids) == len(source_bars)
    assert snapshot.schema_hash == feature_schema_hash(snapshot.features)
    with pytest.raises(TypeError):
        snapshot.features["rsi_2"] = 0.0


def test_snapshot_hash_and_canonical_json_replay_without_computed_time_drift() -> None:
    bars = make_bars([100, 101, 99, 102, 103, 101, 104])
    market = MarketSnapshot(as_of=bars[-1].available_at, bars=bars)
    calculator = FeatureCalculator(make_settings())
    event_ids = tuple(f"event-{index}" for index in range(len(bars)))
    first = calculator.calculate(
        market, workspace_id=WORKSPACE_ID, input_event_ids=event_ids, computed_at=market.as_of
    )
    replay = calculator.calculate(
        market,
        workspace_id=WORKSPACE_ID,
        input_event_ids=event_ids,
        computed_at=market.as_of + timedelta(minutes=5),
    )

    assert first.computed_at != replay.computed_at
    assert first.snapshot_id == replay.snapshot_id
    assert first.content_hash == replay.content_hash
    assert first.canonical_json() == replay.canonical_json()


def test_formula_version_change_changes_hash_and_snapshot_identity() -> None:
    closes = [100, 101, 100, 102, 101, 103, 102]
    first = calculate(closes, settings=make_settings(formula_version="formula-v1"))
    second = calculate(closes, settings=make_settings(formula_version="formula-v2"))

    assert first.formula_hash != second.formula_hash
    assert first.snapshot_id != second.snapshot_id


def test_schema_version_and_code_hash_change_snapshot_identity() -> None:
    closes = [100, 101, 100, 102, 101, 103, 102]
    baseline = calculate(closes)
    changed_schema = calculate(closes, settings=make_settings(schema_version="feature-v2"))
    changed_code = calculate(closes, settings=make_settings(code_hash="b" * 64))

    assert baseline.schema_hash != changed_schema.schema_hash
    assert baseline.snapshot_id != changed_schema.snapshot_id
    assert baseline.snapshot_id != changed_code.snapshot_id


def test_formula_hash_includes_time_and_quality_thresholds() -> None:
    baseline = calculate([100, 101, 100, 102, 101, 103, 102])
    changed_interval = calculate(
        [100, 101, 100, 102, 101, 103, 102],
        settings=make_settings(expected_interval=timedelta(days=2)),
    )
    changed_regime_history = calculate(
        [100, 101, 100, 102, 101, 103, 102],
        settings=make_settings(regime_history=timedelta(days=3)),
    )

    assert baseline.formula_hash != changed_interval.formula_hash
    assert baseline.formula_hash != changed_regime_history.formula_hash


def test_warmup_and_missing_data_are_explicit_and_regime_is_unknown() -> None:
    snapshot = calculate([100, 101, 102])

    assert not snapshot.warmup_complete
    assert any(not ready for ready in snapshot.warmup.values())
    assert "WARMUP_INCOMPLETE" in snapshot.quality_flags
    assert not snapshot.is_usable
    assert snapshot.regime.label is MarketRegime.UNKNOWN
    assert snapshot.regime.confidence == 0.0


def test_stale_or_unclosed_bars_make_snapshot_unusable() -> None:
    bars = list(make_bars([100, 101, 102, 103, 104, 105, 106]))
    unclosed = OHLCVBar(
        **{
            field: getattr(bars[-1], field)
            for field in bars[-1].__dataclass_fields__
            if field != "is_closed"
        },
        is_closed=False,
    )
    bars[-1] = unclosed
    market = MarketSnapshot(as_of=unclosed.available_at, bars=tuple(bars))
    result = FeatureCalculator(make_settings()).calculate(
        market,
        workspace_id=WORKSPACE_ID,
        input_event_ids=tuple(f"event-{index}" for index in range(len(bars))),
        computed_at=market.as_of,
    )

    assert "UNCLOSED_BAR" in result.quality_flags
    assert not result.is_usable
    assert result.regime.label is MarketRegime.UNKNOWN


def test_stale_market_data_is_flagged_and_rejected() -> None:
    bars = make_bars([100, 101, 102, 103, 104, 105, 106])
    market = MarketSnapshot(as_of=bars[-1].available_at + timedelta(days=3), bars=bars)
    result = FeatureCalculator(make_settings()).calculate(
        market,
        workspace_id=WORKSPACE_ID,
        input_event_ids=tuple(f"event-{index}" for index in range(len(bars))),
        computed_at=market.as_of,
    )

    assert result.data_freshness.value == "STALE"
    assert "STALE_DATA" in result.quality_flags
    assert not result.is_usable


def test_missing_bar_reduces_coverage_and_invalidates_snapshot() -> None:
    all_bars = make_bars([100, 101, 102, 103, 104, 105, 106])
    bars = all_bars[:3] + all_bars[4:]
    market = MarketSnapshot(as_of=bars[-1].available_at, bars=bars)
    result = FeatureCalculator(make_settings()).calculate(
        market,
        workspace_id=WORKSPACE_ID,
        input_event_ids=tuple(f"event-{index}" for index in range(len(bars))),
        computed_at=market.as_of,
    )

    assert "BAR_GAP" in result.quality_flags
    assert "MISSING_BARS" in result.quality_flags
    assert result.data_coverage == pytest.approx(6 / 7)
    assert not result.is_usable


def test_vwap_resets_at_utc_midnight() -> None:
    start = datetime(2026, 1, 1, 23, 0, tzinfo=UTC)
    hourly = timedelta(hours=1)
    bars = make_bars([100, 102], start=start, interval=hourly)
    result = calculate(
        [1],
        bars=bars,
        settings=make_settings(
            expected_interval=hourly,
            max_event_age=hourly + timedelta(minutes=1),
            regime_history=timedelta(hours=2),
        ),
    )

    # VWAP dùng proxy nến (high + low + close) / 3; bar thứ hai mở ngày UTC mới.
    assert result.features["session_vwap"] == pytest.approx(102.0)


def test_zero_variance_zscore_is_undefined_without_mislabeling_warmup() -> None:
    snapshot = calculate([100.0] * 8)

    assert snapshot.warmup["zscore_2"]
    assert snapshot.features["zscore_2"] is None
    assert "FEATURE_UNDEFINED" in snapshot.quality_flags
    assert snapshot.is_usable


def test_regime_labels_trend_range_high_volatility_and_unknown() -> None:
    trend = calculate([100 + index for index in range(12)])
    range_prices = [100.1 if index % 2 == 0 else 99.9 for index in range(100)]
    ranging = calculate(
        range_prices,
        bars=make_bars(range_prices, spread=0.0),
        settings=make_settings(adx_period=14),
    )
    shock_prices = [100.0] * 12 + [130.0]
    volatile = calculate(shock_prices)
    unknown = calculate([100, 101, 102, 103])

    assert trend.regime.label is MarketRegime.TREND
    assert trend.regime.confidence > 0.0
    assert trend.regime.coverage == 1.0
    assert ranging.regime.label is MarketRegime.RANGE
    assert volatile.regime.label is MarketRegime.HIGH_VOLATILITY
    assert unknown.regime.label is MarketRegime.UNKNOWN
    assert unknown.regime.reason_code


def test_input_event_ids_must_match_bars_and_be_unique() -> None:
    bars = make_bars([100, 101, 102, 103, 104, 105, 106])
    market = MarketSnapshot(as_of=bars[-1].available_at, bars=bars)
    calculator = FeatureCalculator(make_settings())

    with pytest.raises(ValueError, match="số lượng nến"):
        calculator.calculate(market, workspace_id=WORKSPACE_ID, input_event_ids=("only-one",))
    with pytest.raises(ValueError, match="duy nhất"):
        calculator.calculate(
            market,
            workspace_id=WORKSPACE_ID,
            input_event_ids=("duplicate",) * len(bars),
        )


def test_empty_snapshot_keeps_explicit_market_identity() -> None:
    market = MarketSnapshot(as_of=BASE_TIME, bars=())
    result = FeatureCalculator(make_settings()).calculate(
        market,
        workspace_id=WORKSPACE_ID,
        input_event_ids=(),
        symbol="BTCUSDT",
        timeframe="1d",
    )

    assert result.symbol == "BTCUSDT"
    assert result.timeframe == "1d"
    assert not result.is_usable
    assert "EMPTY_INPUT" in result.quality_flags


def test_explicit_market_identity_must_match_nonempty_input() -> None:
    bars = make_bars([100, 101, 102, 103, 104, 105, 106])
    market = MarketSnapshot(as_of=bars[-1].available_at, bars=bars)

    with pytest.raises(ValueError, match="Symbol truyền vào"):
        FeatureCalculator(make_settings()).calculate(
            market,
            workspace_id=WORKSPACE_ID,
            input_event_ids=tuple(f"event-{index}" for index in range(len(bars))),
            symbol="ETHUSDT",
        )
