from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import pytest
from tests.fixtures.runtime_replay import build_h01_dataset_runtime_replay
from tests.unit.test_backtest_dataset_replay_observations import bundle
from tests.unit.test_runtime_replay_evaluator import _setup

from trading_bot.application.backtest_dataset_replay_context import (
    BacktestDatasetReplayContextProvider,
    BacktestDatasetRuntimeEvaluatorFactory,
    BacktestReplayContextBuildError,
    run_backtest_dataset_runtime_replay,
)
from trading_bot.application.backtest_dataset_replay_observations import (
    build_backtest_dataset_replay_observations,
)
from trading_bot.application.backtest_sensitivity import SensitivityScenario
from trading_bot.domain.execution.paper import PaperExecutionGateway
from trading_bot.domain.strategy.contracts import FeatureQuality
from trading_bot.domain.strategy.registry import StrategyRegistry


def _observations_with_atr():
    source = bundle(timeframes=("1m", "1h", "4h"))
    observations = build_backtest_dataset_replay_observations(
        source,
        strategy_release_id="h01-v1",
    )
    prepared = []
    for index, observation in enumerate(observations):
        value = 2.0 if index == 0 else 99.0
        features = tuple(
            replace(
                feature,
                value=value,
                quality=FeatureQuality.READY,
                available_at=observation.snapshot.decision_time,
            )
            if feature.name == "atr_14" and feature.timeframe == "1h"
            else feature
            for feature in observation.snapshot.features
        )
        prepared.append(
            replace(observation, snapshot=replace(observation.snapshot, features=features))
        )
    return source, tuple(prepared)


def _seed_for_bundle(source, seed=None):
    if seed is None:
        _, seed, _, _, _ = _setup()
    workspace_id = source.workspace_id
    return replace(
        seed,
        published_settings=replace(seed.published_settings, workspace_id=workspace_id),
        planner_settings=replace(seed.planner_settings, workspace_id=str(workspace_id)),
        account=replace(seed.account, workspace_id=str(workspace_id)),
        risk_state=replace(seed.risk_state, workspace_id=str(workspace_id)),
    )


def test_context_provider_emits_only_new_closed_bars_available_by_decision_time() -> None:
    source, observations = _observations_with_atr()
    seed = _seed_for_bundle(source)
    provider = BacktestDatasetReplayContextProvider(
        source,
        observations,
        seed=seed,
        execution_interval="1m",
    )

    first = provider(observations[0].snapshot)
    second = provider(observations[1].snapshot)
    third = provider(observations[2].snapshot)

    assert first.execution_events == ()
    assert len(second.execution_events) == 60
    assert len(third.execution_events) == 60
    assert all(
        event.closed_at <= event.available_at <= observations[1].snapshot.decision_time
        for event in second.execution_events
    )
    assert {event.bar_key for event in second.execution_events}.isdisjoint(
        event.bar_key for event in third.execution_events
    )
    assert {atr for _, _, atr in second.atr_by_bar[:-1]} == {Decimal("2.0")}
    assert second.atr_by_bar[-1][2] == Decimal("99.0")
    assert {atr for _, _, atr in third.atr_by_bar} == {Decimal("99.0")}


def test_context_provider_passes_only_latest_available_opposing_signal() -> None:
    source, observations = _observations_with_atr()
    seed = _seed_for_bundle(source)
    provider = BacktestDatasetReplayContextProvider(
        source,
        observations,
        seed=seed,
        execution_interval="1m",
        opposing_signal_provider=lambda _bar, observation: (
            observation is not None and observation.event_id == observations[0].event_id
        ),
    )

    provider(observations[0].snapshot)
    second = provider(observations[1].snapshot)

    assert len(second.opposing_signal_by_bar) == 60
    assert {signal for _, _, signal in second.opposing_signal_by_bar[:-1]} == {True}
    assert second.opposing_signal_by_bar[-1][2] is False


def test_context_provider_emits_point_in_time_exit_evidence_for_v2_recipes() -> None:
    source, observations = _observations_with_atr()
    provider = BacktestDatasetReplayContextProvider(
        source,
        observations,
        seed=_seed_for_bundle(source),
        execution_interval="1m",
    )
    hourly = next(item for item in source.components if item.manifest.interval == "1h").bars

    provider(observations[0].snapshot)
    second = provider(observations[1].snapshot)

    # Bundle không có nến 15m nên chỉ recipe H03 v2 (khung 1h) nhận bằng chứng.
    assert {item.recipe_id for item in second.exit_evidence_by_bar} == {"h03-exit-v2"}
    assert len(second.exit_evidence_by_bar) == len(second.execution_events)
    events = {event.bar_key: event for event in second.execution_events}
    for evidence in second.exit_evidence_by_bar:
        event = events[evidence.bar_key]
        known = sorted(
            (bar for bar in hourly if bar.close_time <= event.opened_at),
            key=lambda bar: bar.close_time,
        )
        if len(known) < 20:
            assert evidence.channel_low is None
            assert evidence.channel_high is None
        else:
            # Kênh chỉ gồm 20 nến 1h đóng trước khi execution bar mở: không nhìn trước.
            assert evidence.channel_low == min(bar.low for bar in known[-20:])
            assert evidence.channel_high == max(bar.high for bar in known[-20:])
        assert evidence.strategy_bar_closed is (
            event.closed_at in {bar.close_time for bar in hourly}
        )
    # Trong 60 nến 1 phút giữa hai quyết định 1h chỉ có đúng một nến đóng cùng nến 1h.
    assert sum(item.strategy_bar_closed for item in second.exit_evidence_by_bar) == 1


def test_context_provider_rejects_nonflat_seed_and_out_of_order_observation() -> None:
    source, observations = _observations_with_atr()
    seed = _seed_for_bundle(source)

    with pytest.raises(BacktestReplayContextBuildError, match="seed phẳng"):
        BacktestDatasetReplayContextProvider(
            source,
            observations,
            seed=replace(
                seed,
                risk_state=replace(seed.risk_state, pending_reservation_count=1),
            ),
            execution_interval="1m",
        )

    provider = BacktestDatasetReplayContextProvider(
        source,
        observations,
        seed=seed,
        execution_interval="1m",
    )
    with pytest.raises(BacktestReplayContextBuildError, match="theo thứ tự"):
        provider(observations[1].snapshot)


def test_runtime_evaluator_factory_creates_isolated_gateway_and_provider_per_trial() -> None:
    source, observations = _observations_with_atr()
    _, seed, _, seed_gateway, manifest = _setup()
    seed = _seed_for_bundle(source, seed)
    manifest = replace(
        manifest,
        dataset_hash=source.dataset_hash,
        range_start=source.evaluation_start,
        range_end=source.range_end,
    )
    factory = BacktestDatasetRuntimeEvaluatorFactory(
        bundle=source,
        observations=observations,
        strategies=StrategyRegistry(),
        execution_interval="1m",
        context_seed_factory=lambda _scenario, _manifest: seed,
        gateway_factory=lambda _scenario, _manifest: PaperExecutionGateway(seed_gateway.policy),
    )
    scenario = SensitivityScenario("base", "BASE")

    first = factory(scenario, manifest)
    second = factory(scenario, manifest)

    assert first is not second
    assert first.gateway is not second.gateway
    assert first.context_provider is not second.context_provider


def test_backtest_dataset_service_runs_bundle_through_paper_runtime_and_runner() -> None:
    source, _ = _observations_with_atr()
    _, seed, _, seed_gateway, manifest = _setup()
    manifest = replace(
        manifest,
        dataset_hash=source.dataset_hash,
        range_start=source.evaluation_start,
        range_end=source.range_end,
    )
    seed = _seed_for_bundle(source, seed)

    comparison = run_backtest_dataset_runtime_replay(
        source,
        manifest,
        strategies=StrategyRegistry(),
        execution_interval="1m",
        context_seed_factory=lambda _scenario, _manifest: seed,
        gateway_factory=lambda _scenario, _manifest: PaperExecutionGateway(seed_gateway.policy),
    )

    assert comparison.is_complete, comparison.runs[0].sensitivity_run.result
    assert len(comparison.runs) == 1
    assert comparison.runs[0].sensitivity_run.result.observation_count > 0
    assert comparison.runs[0].trade_outcomes == ()


def test_multitimeframe_fixture_runs_and_persists_a_complete_dataset_comparison() -> None:
    source, comparison = build_h01_dataset_runtime_replay(
        UUID("11111111-1111-4111-8111-111111111111")
    )

    assert source.dataset_hash == comparison.runs[0].sensitivity_run.manifest.dataset_hash
    assert comparison.is_complete
    report = comparison.runs[0].sensitivity_run.result
    assert report.observation_count == 4
    assert report.usable_observation_count == 4
    assert report.trade_count == 0
    assert comparison.runs[0].trade_outcomes == ()
