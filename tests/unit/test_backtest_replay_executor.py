from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from tests.unit.test_backtest_dataset_replay_observations import bundle

from trading_bot.application.backtest_dataset_replay_observations import (
    build_backtest_dataset_replay_observations,
)
from trading_bot.application.backtest_replay_executor import build_backtest_replay_plan
from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission
from trading_bot.application.backtest_sensitivity import SensitivityScenario
from trading_bot.application.runtime_replay_evaluator import RuntimePaperReplayComparison
from trading_bot.domain.execution.paper import PaperExecutionGateway

JOB_ID = UUID("84fdc281-3db5-41fb-b4aa-8f2de1807ab9")


def _submission(source, *, release: str = "h01-v1", interval: str = "1h"):
    return BacktestReplayJobSubmission(
        bundle_hash=source.dataset_hash,
        strategy_release_id=release,
        execution_interval=interval,
        starting_equity=Decimal("10000"),
        per_trade_risk_fraction=Decimal("0.01"),
        fee_rate=Decimal("0.001"),
        slippage_rate=Decimal("0.002"),
        seed=17,
    )


def test_plan_pins_deterministic_manifest_and_explicit_paper_profile() -> None:
    source = bundle()
    first = build_backtest_replay_plan(
        WORKSPACE := source.workspace_id, JOB_ID, source, _submission(source)
    )
    second = build_backtest_replay_plan(WORKSPACE, JOB_ID, source, _submission(source))

    assert first.manifest == second.manifest
    assert first.manifest.dataset_hash == source.dataset_hash
    assert first.manifest.range_start == source.evaluation_start
    assert first.manifest.range_end == source.range_end
    assert first.manifest.strategy_release_id == "h01-v1"
    assert first.manifest.strategy_content_hash
    assert first.manifest.cost_assumptions == (
        ("adverse_funding_per_unit", "0"),
        ("entry_fee_rate", "0.001"),
        ("entry_slippage_rate", "0.002"),
        ("exit_fee_rate", "0.001"),
        ("exit_slippage_rate", "0.002"),
        ("spread_rate", "0"),
    )
    assert first.manifest.seed == 17


def test_training_replay_intent_is_bound_to_manifest_before_execution() -> None:
    source = bundle()
    request = replace(_submission(source), learning_intent="TRAINING_BASE")
    plan = build_backtest_replay_plan(source.workspace_id, JOB_ID, source, request)
    research_plan = build_backtest_replay_plan(
        source.workspace_id, JOB_ID, source, _submission(source)
    )

    assert plan.manifest.learning_intent == "TRAINING_BASE"
    assert plan.manifest.learning_job_id == str(JOB_ID)
    assert plan.manifest.content_hash != research_plan.manifest.content_hash


def test_replay_job_payload_persists_intent_and_reads_legacy_as_research() -> None:
    source = bundle()
    request = replace(_submission(source), learning_intent="TRAINING_BASE")

    restored = type(request).from_payload(request.to_payload())
    legacy_payload = request.to_payload()
    legacy_payload.pop("learning_intent")
    legacy = type(request).from_payload(legacy_payload)

    assert restored == request
    assert legacy.learning_intent == "RESEARCH"


def test_executor_uses_only_one_base_scenario_and_matching_paper_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = bundle()
    request = _submission(source)
    plan = build_backtest_replay_plan(source.workspace_id, JOB_ID, source, request)
    comparison = RuntimePaperReplayComparison.__new__(RuntimePaperReplayComparison)
    captured: dict[str, object] = {}

    def fake_run(
        replay_bundle,
        manifest,
        *,
        strategies,
        execution_interval,
        context_seed_factory,
        gateway_factory,
        scenarios,
        progress_callback,
        cancel_requested,
    ):
        captured.update(
            replay_bundle=replay_bundle,
            manifest=manifest,
            strategies=strategies,
            execution_interval=execution_interval,
            context_seed_factory=context_seed_factory,
            gateway_factory=gateway_factory,
            scenarios=scenarios,
            progress_callback=progress_callback,
            cancel_requested=cancel_requested,
        )
        return comparison

    monkeypatch.setattr(
        "trading_bot.application.backtest_replay_executor.run_backtest_dataset_runtime_replay",
        fake_run,
    )

    assert plan.executor(source, plan.manifest) is comparison
    assert captured["replay_bundle"] is source
    assert captured["manifest"] == plan.manifest
    assert captured["execution_interval"] == "1h"
    assert captured["scenarios"] == (SensitivityScenario("base", "BASE"),)
    assert captured["progress_callback"] is None
    assert captured["cancel_requested"] is None

    def progress_callback(completed: int, total: int) -> None:
        captured["progress"] = (completed, total)

    def cancel_requested() -> bool:
        return False

    assert (
        plan.executor(
            source,
            plan.manifest,
            progress_callback=progress_callback,
            cancel_requested=cancel_requested,
        )
        is comparison
    )
    assert captured["progress_callback"] is progress_callback
    assert captured["cancel_requested"] is cancel_requested

    renewed_bundle = replace(
        source,
        components=tuple(
            replace(
                component,
                current_rights=replace(
                    component.current_rights,
                    checked_at=component.current_rights.checked_at + timedelta(seconds=1),
                    valid_until=component.current_rights.valid_until + timedelta(seconds=1),
                ),
            )
            for component in source.components
        ),
    )
    assert plan.executor(renewed_bundle, plan.manifest) is comparison
    assert captured["replay_bundle"] is renewed_bundle

    scenario = captured["scenarios"][0]
    seed = captured["context_seed_factory"](scenario, plan.manifest)
    assert seed.published_settings.settings.runtime.mode == "PAPER"
    assert seed.published_settings.settings.paper_execution.starting_equity == Decimal("10000")
    assert seed.published_settings.settings.strategy.default_release == "h01-v1"
    assert seed.account.is_known is True
    assert seed.account.equity == Decimal("10000")
    assert seed.risk_state.is_known is True
    assert seed.risk_state.open_exposures == ()
    assert seed.risk_state.pending_reservation_count == 0
    assert seed.planner_settings.limits.per_trade_risk_fraction == Decimal("0.01")
    assert seed.cost_model.entry_fee_rate == seed.cost_model.exit_fee_rate == Decimal("0.001")
    assert (
        seed.cost_model.entry_slippage_rate
        == seed.cost_model.exit_slippage_rate
        == Decimal("0.002")
    )

    gateway = captured["gateway_factory"](scenario, plan.manifest)
    assert isinstance(gateway, PaperExecutionGateway)
    assert gateway.policy.fee_rate == Decimal("0.001")
    assert gateway.policy.slippage_rate == Decimal("0.002")
    assert gateway.policy.spread_rate == Decimal("0")


def test_plan_rejects_workspace_hash_and_required_timeframe_mismatch() -> None:
    source = bundle()
    request = _submission(source)
    with pytest.raises(ValueError, match="workspace"):
        build_backtest_replay_plan(UUID(int=0), JOB_ID, source, request)

    wrong_hash = replace(request, bundle_hash="a" * 64)
    with pytest.raises(ValueError, match="hash"):
        build_backtest_replay_plan(source.workspace_id, JOB_ID, source, wrong_hash)

    incomplete = bundle(timeframes=("1h",))
    with pytest.raises(ValueError, match="4h"):
        build_backtest_replay_plan(
            incomplete.workspace_id,
            JOB_ID,
            incomplete,
            _submission(incomplete),
        )


def test_plan_rejects_execution_interval_not_in_bundle() -> None:
    source = bundle()
    with pytest.raises(ValueError, match="execution interval"):
        build_backtest_replay_plan(
            source.workspace_id,
            JOB_ID,
            source,
            _submission(source, interval="15m"),
        )


def test_plan_accepts_supported_lower_timeframe_execution_interval() -> None:
    source = bundle(timeframes=("1m", "1h", "4h"))
    request = _submission(source, interval="1m")

    plan = build_backtest_replay_plan(source.workspace_id, JOB_ID, source, request)

    assert plan.manifest.dataset_hash == source.dataset_hash
    assert plan.manifest.range_start == source.evaluation_start


def test_h03_missing_quote_volume_does_not_create_a_trade() -> None:
    source = bundle(timeframes=("1h",), missing_quote_volume=True)
    request = _submission(source, release="h03-v1")
    plan = build_backtest_replay_plan(
        source.workspace_id,
        JOB_ID,
        source,
        request,
    )
    observations = build_backtest_dataset_replay_observations(source, strategy_release_id="h03-v1")

    assert observations
    assert all(item.snapshot.get_series("1h").quote_volumes is None for item in observations)
    comparison = plan.executor(source, plan.manifest)
    assert comparison.is_complete
    assert comparison.runs[0].sensitivity_run.result.trade_count == 0
