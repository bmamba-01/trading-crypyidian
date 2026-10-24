from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    FeatureObservation,
    FeatureQuality,
    MarketSeries,
    SeriesQuality,
    StrategyCandidate,
    StrategySelection,
)
from trading_bot.domain.strategy.recipes import ExitRecipe


def _bar(index: int, *, timeframe: str = "1h", available_at: datetime | None = None) -> OHLCVBar:
    opened = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index)
    closed = opened + timedelta(hours=1) - timedelta(microseconds=1)
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe=timeframe,
        open_time=opened,
        close_time=closed,
        event_at=closed,
        received_at=available_at or closed,
        available_at=available_at or closed,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("10"),
        is_closed=True,
    )


def test_feature_observation_preserves_identity_timeframe_and_quality() -> None:
    available_at = datetime(2026, 1, 1, tzinfo=UTC)
    feature = FeatureObservation(
        feature_id="feature-123",
        name="ema_20",
        timeframe="1h",
        value=100.0,
        previous_value=99.0,
        available_at=available_at,
    )

    assert feature.feature_id == "feature-123"
    assert feature.quality is FeatureQuality.READY


def test_decision_snapshot_rejects_duplicate_feature_keys() -> None:
    available_at = datetime(2026, 1, 1, tzinfo=UTC)
    features = (
        FeatureObservation("f1", "ema_20", "1h", 100.0, 99.0, available_at),
        FeatureObservation("f2", "ema_20", "1h", 101.0, 100.0, available_at),
    )

    with pytest.raises(ValueError, match="feature.*lặp"):
        DecisionSnapshot(
            symbol="BTCUSDT",
            decision_time=available_at,
            snapshot_id="snapshot-duplicate-features",
            series=(MarketSeries("1h", (_bar(0),)),),
            features=features,
        )


def test_market_series_requires_aligned_quote_volume_and_timeframe() -> None:
    with pytest.raises(ValueError, match="quote volume.*độ dài"):
        MarketSeries("1h", (_bar(0), _bar(1)), quote_volumes=(10.0,))

    with pytest.raises(ValueError, match="timeframe"):
        MarketSeries("4h", (_bar(0, timeframe="1h"),))


def test_candidate_is_immutable_and_carries_versioned_evidence_and_window() -> None:
    signal_at = datetime(2026, 1, 1, tzinfo=UTC)
    candidate = StrategyCandidate(
        strategy_id="H01",
        strategy_release_id="h01-v1",
        release_content_hash="a" * 64,
        settings_version_id="settings-v4",
        snapshot_id="snapshot-123",
        symbol="BTCUSDT",
        direction="long",
        reference_price=Decimal("100"),
        atr_value=2.0,
        signal_at=signal_at,
        eligible_after=signal_at + timedelta(microseconds=1),
        expires_at=signal_at + timedelta(hours=1),
        recipe=ExitRecipe("h01-exit-v1", 2.0, trailing_atr_multiple=3.0),
        evidence_feature_ids=("ema20", "ema80", "adx14", "atr14"),
        evidence_bar_keys=("BTCUSDT|1h|2026-01-01T00:00:00+00:00",),
        macro_evidence_ids=(),
        geometry_version=None,
        reason_code="H01_LONG_CROSS",
        detail_vi="EMA20 cắt lên EMA80 và các bộ lọc xác nhận xu hướng.",
    )

    with pytest.raises(FrozenInstanceError):
        candidate.direction = "short"  # type: ignore[misc]
    assert candidate.eligible_after < candidate.expires_at
    assert candidate.settings_version_id == "settings-v4"


def test_market_series_is_unknown_until_quality_and_freshness_policy_are_supplied() -> None:
    series = MarketSeries("1h", (_bar(0),))

    assert series.quality is SeriesQuality.UNKNOWN
    assert series.expected_interval is None
    assert series.backfill_complete is None


def test_strategy_selection_is_an_immutable_published_version_reference() -> None:
    selection = StrategySelection(
        settings_version_id="settings-v4",
        active_release_ids=("h01-v1", "h03-v1"),
        published=True,
    )

    assert selection.published
    assert selection.active_release_ids == ("h01-v1", "h03-v1")
