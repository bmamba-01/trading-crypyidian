from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.application.backtest import (
    BacktestRunner,
    EvaluationReport,
    ReplayManifest,
    ReplayObservation,
    ReplayRejected,
)
from trading_bot.domain.learning.evaluation import EvaluationTrade
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    FeatureObservation,
    FeatureQuality,
    MarketSeries,
    SeriesQuality,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def _bar(index: int, *, available_offset: timedelta = timedelta(0)) -> OHLCVBar:
    opened = START + timedelta(hours=index)
    closed = opened + timedelta(hours=1)
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe="1h",
        open_time=opened,
        close_time=closed,
        event_at=closed,
        received_at=closed,
        available_at=closed + available_offset,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1"),
        is_closed=True,
    )


def _observation(index: int, *, future_feature: bool = False) -> ReplayObservation:
    bar = _bar(index)
    decision_time = bar.available_at
    feature = FeatureObservation(
        feature_id=f"feature-{index}",
        name="atr_14",
        timeframe="1h",
        value=1.0,
        previous_value=None,
        available_at=decision_time + (timedelta(seconds=1) if future_feature else timedelta(0)),
        quality=FeatureQuality.READY,
    )
    snapshot = DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=decision_time,
        snapshot_id=f"snapshot-{index}",
        series=(
            MarketSeries(
                timeframe="1h",
                bars=(bar,),
                quality=SeriesQuality.READY,
                expected_interval=timedelta(hours=1),
                max_event_age=timedelta(hours=2),
                backfill_complete=True,
            ),
        ),
        features=(feature,),
    )
    return ReplayObservation(f"event-{index}", decision_time, decision_time, snapshot)


def _manifest(**changes: object) -> ReplayManifest:
    values: dict[str, object] = {
        "dataset_hash": "d" * 64,
        "range_start": START,
        "range_end": START + timedelta(days=1),
        "settings_version_id": "settings-v1",
        "risk_profile_version_id": "risk-v1",
        "strategy_release_id": "h01-v1",
        "strategy_content_hash": "b" * 64,
        "feature_schema_hash": "c" * 64,
        "fill_model_version": "paper-v1",
        "pipeline_content_hash": "e" * 64,
        "cost_assumptions": (("fee_rate", "0.001"), ("funding_rate", "0.0001")),
        "parameter_family_hash": "a" * 64,
        "trial_count": 1,
        "trial_index": 0,
        "seed": 7,
    }
    values.update(changes)
    return ReplayManifest(**values)  # type: ignore[arg-type]


def test_runner_sorts_events_deterministically_and_builds_stable_manifest() -> None:
    seen: list[str] = []

    def evaluate(snapshot: DecisionSnapshot) -> EvaluationTrade:
        seen.append(snapshot.snapshot_id)
        return EvaluationTrade(
            trade_id=f"trade-{snapshot.snapshot_id}",
            opened_at=snapshot.decision_time,
            closed_at=snapshot.decision_time + timedelta(minutes=1),
            gross_pnl=Decimal("10"),
            fees=Decimal("1"),
            funding=Decimal("0.5"),
            slippage=Decimal("0.5"),
            exposure=Decimal("100"),
        )

    runner = BacktestRunner()
    first = runner.run((_observation(1), _observation(0)), manifest=_manifest(), evaluator=evaluate)
    first_order = tuple(seen)
    seen.clear()
    second = runner.run(
        (_observation(0), _observation(1)), manifest=_manifest(), evaluator=evaluate
    )

    assert first_order == ("snapshot-0", "snapshot-1")
    assert seen == list(first_order)
    assert first.manifest_hash == second.manifest_hash
    assert first.report_hash == second.report_hash
    assert first.net_pnl == Decimal("16")
    assert first.trade_count == 2
    assert first.trial_record.status == "COMPLETED"


def test_runner_accepts_trade_emitted_at_close_after_prior_observation_open() -> None:
    first = _observation(0)
    second = _observation(1)
    closed_trade = EvaluationTrade(
        trade_id="trade-opened-earlier",
        opened_at=first.snapshot.decision_time,
        closed_at=second.snapshot.decision_time,
        gross_pnl=Decimal("5"),
        fees=Decimal("1"),
        funding=Decimal("0"),
        slippage=Decimal("0.5"),
        exposure=Decimal("50"),
    )

    class CloseEmittingEvaluator:
        emits_trade_on_close = True

        def __call__(self, snapshot: DecisionSnapshot) -> EvaluationTrade | None:
            return closed_trade if snapshot.snapshot_id == "snapshot-1" else None

    result = BacktestRunner().run(
        (first, second),
        manifest=_manifest(),
        evaluator=CloseEmittingEvaluator(),
    )

    assert isinstance(result, EvaluationReport)
    assert result.trade_count == 1
    assert result.net_pnl == Decimal("3.5")


@pytest.mark.parametrize(
    ("opened_at", "closed_at", "expected_reason"),
    [
        (START - timedelta(seconds=1), START + timedelta(minutes=1), "TRADE_OUTSIDE_RANGE"),
        (START + timedelta(hours=1), START + timedelta(hours=3), "TRADE_AFTER_OBSERVATION"),
    ],
)
def test_runner_rejects_trade_outside_observation_or_replay_window(
    opened_at: datetime, closed_at: datetime, expected_reason: str
) -> None:
    observation = _observation(0)
    trade = EvaluationTrade(
        trade_id="invalid-trade",
        opened_at=opened_at,
        closed_at=closed_at,
        gross_pnl=Decimal("1"),
        fees=Decimal("0"),
        funding=Decimal("0"),
        slippage=Decimal("0"),
        exposure=Decimal("1"),
    )

    class CloseEmittingEvaluator:
        emits_trade_on_close = True

        def __call__(self, snapshot: DecisionSnapshot) -> EvaluationTrade:
            return trade

    result = BacktestRunner().run(
        (observation,), manifest=_manifest(), evaluator=CloseEmittingEvaluator()
    )

    assert isinstance(result, ReplayRejected)
    assert result.reason_code == expected_reason


def test_runner_rejects_feature_available_after_decision_time_and_records_trial() -> None:
    calls = 0

    def evaluate(snapshot: DecisionSnapshot) -> None:
        nonlocal calls
        calls += 1
        return None

    result = BacktestRunner().run(
        (_observation(0, future_feature=True),), manifest=_manifest(), evaluator=evaluate
    )

    assert calls == 0
    assert isinstance(result, ReplayRejected)
    assert result.trial_record.status == "REJECTED"
    assert result.reason_code == "LOOKAHEAD_DETECTED"


def test_runner_coverage_counts_usable_observations_not_trade_frequency() -> None:
    result = BacktestRunner().run(
        (_observation(0), _observation(1)), manifest=_manifest(), evaluator=lambda _: None
    )

    assert isinstance(result, EvaluationReport)
    assert result.coverage == 1.0
    assert result.trade_frequency == 0.0


def test_runner_excludes_stale_observation_and_reports_actual_usable_coverage() -> None:
    fresh = _observation(0)
    stale = _observation(1)
    old_bar = _bar(1)
    stale_time = old_bar.available_at + timedelta(hours=3)
    stale_snapshot = DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=stale_time,
        snapshot_id=stale.snapshot.snapshot_id,
        series=(
            MarketSeries(
                timeframe="1h",
                bars=(old_bar,),
                quality=SeriesQuality.READY,
                expected_interval=timedelta(hours=1),
                max_event_age=timedelta(hours=2),
                backfill_complete=True,
            ),
        ),
        features=stale.snapshot.features,
    )
    stale_observation = ReplayObservation("stale-event", stale_time, stale_time, stale_snapshot)
    evaluated: list[str] = []

    result = BacktestRunner().run(
        (fresh, stale_observation),
        manifest=_manifest(),
        evaluator=lambda snapshot: evaluated.append(snapshot.snapshot_id),
    )

    assert isinstance(result, EvaluationReport)
    assert evaluated == [fresh.snapshot.snapshot_id]
    assert result.observation_count == 2
    assert result.usable_observation_count == 1
    assert result.coverage == 0.5


def test_runner_excludes_observation_with_bar_gap_from_usable_coverage() -> None:
    first = _observation(0)
    later_bar = _bar(2)
    decision_time = later_bar.available_at
    gap_snapshot = DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=decision_time,
        snapshot_id="snapshot-gap",
        series=(
            MarketSeries(
                timeframe="1h",
                bars=(_bar(0), later_bar),
                quality=SeriesQuality.READY,
                expected_interval=timedelta(hours=1),
                max_event_age=timedelta(hours=4),
                backfill_complete=True,
            ),
        ),
        features=(),
    )
    gap = ReplayObservation("event-gap", decision_time, decision_time, gap_snapshot)

    result = BacktestRunner().run((first, gap), manifest=_manifest(), evaluator=lambda _: None)

    assert isinstance(result, EvaluationReport)
    assert result.observation_count == 2
    assert result.usable_observation_count == 1
    assert result.coverage == 0.5


def test_runner_rejects_unclosed_or_future_market_bars() -> None:
    observation = _observation(0)
    original = observation.snapshot.series[0].bars[0]
    future_bar = OHLCVBar(
        symbol=original.symbol,
        timeframe=original.timeframe,
        open_time=original.open_time,
        close_time=original.close_time,
        event_at=original.event_at,
        received_at=original.received_at,
        available_at=observation.snapshot.decision_time + timedelta(seconds=1),
        open=original.open,
        high=original.high,
        low=original.low,
        close=original.close,
        volume=original.volume,
        is_closed=True,
    )
    changed_snapshot = DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=observation.snapshot.decision_time,
        snapshot_id=observation.snapshot.snapshot_id,
        series=(
            MarketSeries(
                "1h",
                (future_bar,),
                quality=SeriesQuality.READY,
                expected_interval=timedelta(hours=1),
                max_event_age=timedelta(hours=2),
                backfill_complete=True,
            ),
        ),
        features=(),
    )
    changed = ReplayObservation(
        observation.event_id, observation.event_at, observation.available_at, changed_snapshot
    )
    result = BacktestRunner().run((changed,), manifest=_manifest(), evaluator=lambda _: None)

    assert isinstance(result, ReplayRejected)
    assert result.reason_code == "LOOKAHEAD_DETECTED"


def test_failed_evaluator_creates_failed_trial_record() -> None:
    def evaluate(snapshot: DecisionSnapshot) -> None:
        raise RuntimeError("evaluator failed")

    result = BacktestRunner().run((_observation(0),), manifest=_manifest(), evaluator=evaluate)

    assert isinstance(result, ReplayRejected)
    assert result.trial_record.status == "FAILED"
    assert result.trial_record.error_type == "RuntimeError"
    assert "evaluator failed" not in result.trial_record.error_detail


def test_invalid_evaluation_output_still_creates_failed_trial_record() -> None:
    def evaluate(snapshot: DecisionSnapshot) -> EvaluationTrade:
        return EvaluationTrade(
            trade_id="duplicate-trade",
            opened_at=snapshot.decision_time,
            closed_at=snapshot.decision_time + timedelta(minutes=1),
            gross_pnl=Decimal("1"),
            fees=Decimal("0"),
            funding=Decimal("0"),
            slippage=Decimal("0"),
            exposure=Decimal("10"),
        )

    result = BacktestRunner().run(
        (_observation(0), _observation(1)), manifest=_manifest(), evaluator=evaluate
    )

    assert isinstance(result, ReplayRejected)
    assert result.reason_code == "EVALUATION_OUTPUT_INVALID"
    assert result.trial_record.status == "FAILED"
    assert result.trial_record.error_type == "ValueError"
    assert result.trial_record.completed_observation_count == 2


def test_replay_manifest_binds_fill_costs_trial_count_and_seed() -> None:
    base = _manifest()
    changed = _manifest(fill_model_version="paper-v2", trial_count=4, seed=8)

    assert base.content_hash != changed.content_hash


def test_manifest_hash_changes_for_risk_pipeline_and_parameter_trial() -> None:
    base = _manifest()
    changed = _manifest(
        risk_profile_version_id="risk-v2",
        pipeline_content_hash="9" * 64,
        parameter_family_hash="8" * 64,
        trial_index=1,
        trial_count=2,
    )

    assert base.content_hash != changed.content_hash
