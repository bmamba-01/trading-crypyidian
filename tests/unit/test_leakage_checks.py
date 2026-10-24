from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.application.backtest import (
    BacktestRunner,
    ReplayManifest,
    ReplayObservation,
    ReplayRejected,
    validate_replay_observation,
)
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.strategy.contracts import DecisionSnapshot, MarketSeries, SeriesQuality


def test_feature_availability_must_not_exceed_decision_time() -> None:
    from trading_bot.domain.strategy.contracts import FeatureObservation

    decision_time = datetime(2026, 1, 1, tzinfo=UTC)
    feature = FeatureObservation(
        "feature-1", "atr_14", "1h", 1.0, None, decision_time + timedelta(seconds=1)
    )
    snapshot = DecisionSnapshot("BTCUSDT", decision_time, "snapshot-1", (), (feature,))

    with pytest.raises(ValueError, match="LOOKAHEAD"):
        validate_replay_observation(
            ReplayObservation("event-1", decision_time, decision_time, snapshot)
        )


def test_market_bar_must_be_closed_before_decision_time() -> None:
    decision_time = datetime(2026, 1, 1, tzinfo=UTC)
    bar = OHLCVBar(
        "BTCUSDT",
        "1h",
        decision_time - timedelta(hours=1),
        decision_time + timedelta(seconds=1),
        decision_time,
        decision_time,
        decision_time,
        Decimal("100"),
        Decimal("101"),
        Decimal("99"),
        Decimal("100"),
        Decimal("1"),
        True,
    )
    snapshot = DecisionSnapshot(
        "BTCUSDT",
        decision_time,
        "snapshot-1",
        (
            MarketSeries(
                "1h",
                (bar,),
                quality=SeriesQuality.READY,
                expected_interval=timedelta(hours=1),
                max_event_age=timedelta(hours=2),
                backfill_complete=True,
            ),
        ),
        (),
    )

    with pytest.raises(ValueError, match="LOOKAHEAD"):
        validate_replay_observation(
            ReplayObservation("event-1", decision_time, decision_time, snapshot)
        )


def test_unclosed_bar_is_rejected_even_if_all_timestamps_are_past() -> None:
    decision_time = datetime(2026, 1, 1, tzinfo=UTC)
    bar = OHLCVBar(
        "BTCUSDT",
        "1h",
        decision_time - timedelta(hours=1),
        decision_time,
        decision_time,
        decision_time,
        decision_time,
        Decimal("100"),
        Decimal("101"),
        Decimal("99"),
        Decimal("100"),
        Decimal("1"),
        False,
    )
    snapshot = DecisionSnapshot(
        "BTCUSDT",
        decision_time,
        "snapshot-unclosed",
        (
            MarketSeries(
                "1h",
                (bar,),
                quality=SeriesQuality.READY,
                expected_interval=timedelta(hours=1),
                max_event_age=timedelta(hours=2),
                backfill_complete=True,
            ),
        ),
        (),
    )

    with pytest.raises(ValueError, match="LOOKAHEAD"):
        validate_replay_observation(
            ReplayObservation("event-1", decision_time, decision_time, snapshot)
        )


def test_manifest_range_is_half_open_and_future_event_is_rejected() -> None:
    from trading_bot.domain.strategy.contracts import FeatureObservation

    decision_time = datetime(2026, 1, 1, 1, tzinfo=UTC)
    snapshot = DecisionSnapshot(
        "BTCUSDT",
        decision_time,
        "snapshot-1",
        (),
        (FeatureObservation("feature-1", "atr_14", "1h", 1.0, None, decision_time),),
    )
    observation = ReplayObservation("event-1", decision_time, decision_time, snapshot)
    manifest = ReplayManifest(
        dataset_hash="d" * 64,
        range_start=datetime(2026, 1, 1, tzinfo=UTC),
        range_end=decision_time,
        settings_version_id="settings-v1",
        risk_profile_version_id="risk-v1",
        strategy_release_id="h01-v1",
        strategy_content_hash="b" * 64,
        feature_schema_hash="c" * 64,
        fill_model_version="paper-v1",
        pipeline_content_hash="e" * 64,
        cost_assumptions=(),
        parameter_family_hash="a" * 64,
        trial_count=1,
        trial_index=0,
        seed=1,
    )
    result = BacktestRunner().run((observation,), manifest=manifest, evaluator=lambda _: None)

    assert isinstance(result, ReplayRejected)
    assert result.reason_code == "EVENT_OUTSIDE_RANGE"
