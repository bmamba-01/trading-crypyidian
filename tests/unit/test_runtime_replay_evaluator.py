from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from tests.unit.test_paper_decision_cycle import _inputs
from tests.unit.test_workspace_settings import valid_payload

from trading_bot.adapters.database.repositories.backtest_outcomes import (
    BacktestOutcomeRepository,
)
from trading_bot.application.backtest import ReplayManifest, ReplayObservation
from trading_bot.application.backtest_sensitivity import SensitivityScenario
from trading_bot.application.paper_decision_cycle import PaperDecisionCycle
from trading_bot.application.runtime_replay_evaluator import (
    ReplayEvaluationContext,
    ReplayEvaluationContextError,
    RuntimeReplayEvaluator,
    run_runtime_paper_replay,
)
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings
from trading_bot.domain.execution.paper import (
    OrderStatus,
    PaperBar,
    PaperExecutionGateway,
    PaperExecutionPolicy,
)
from trading_bot.domain.risk.engine import RiskEngine
from trading_bot.domain.risk.planner import (
    OpenExposure,
    TradePlanner,
    calculate_plan_hash,
)
from trading_bot.domain.strategy.contracts import StrategyCandidate
from trading_bot.domain.strategy.registry import StrategyRegistry

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SETTINGS_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
START = datetime(2026, 1, 1, tzinfo=UTC)


def _setup():
    snapshot, _, planner, account, state, costs = _inputs()
    costs = replace(costs, exit_slippage_rate=costs.entry_slippage_rate)
    registry = StrategyRegistry()
    release = registry.get_release("h01-v1")
    risk_profile_id = uuid4()
    settings_hash = "f" * 64
    planner = replace(
        planner,
        settings_version_id=str(SETTINGS_ID),
        risk_profile_version_id=str(risk_profile_id),
        settings_hash=settings_hash,
        active_release_ids=("h01-v1",),
        release_content_hashes=(("h01-v1", release.content_hash),),
    )
    account = replace(account, settings_version_id=str(SETTINGS_ID))
    state = replace(
        state,
        settings_version_id=str(SETTINGS_ID),
        risk_profile_version_id=str(risk_profile_id),
        evaluated_at=snapshot.decision_time + timedelta(seconds=1),
        market_data_as_of=snapshot.decision_time,
    )
    payload = valid_payload()
    risk = payload["risk"]
    assert isinstance(risk, dict)
    risk["profile_version_id"] = str(risk_profile_id)
    published = PublishedWorkspaceSettings(
        workspace_id=WORKSPACE_ID,
        settings_version_id=SETTINGS_ID,
        settings_hash=settings_hash,
        settings=WorkspaceSettings.model_validate(payload),
    )
    fill_policy = PaperExecutionPolicy(
        version_id="paper-fill-v1",
        latency=timedelta(0),
        spread_rate=Decimal("0"),
        slippage_rate=costs.entry_slippage_rate,
        fee_rate=Decimal("0.001"),
    )
    gateway = PaperExecutionGateway(fill_policy)
    cycle = PaperDecisionCycle(registry, TradePlanner(), RiskEngine(), gateway)
    manifest = ReplayManifest(
        dataset_hash="d" * 64,
        range_start=START,
        range_end=START + timedelta(days=30),
        settings_version_id=str(SETTINGS_ID),
        risk_profile_version_id=str(risk_profile_id),
        strategy_release_id="h01-v1",
        strategy_content_hash=release.content_hash,
        feature_schema_hash="c" * 64,
        fill_model_version=fill_policy.version_id,
        pipeline_content_hash="e" * 64,
        cost_assumptions=(
            ("entry_fee_rate", str(costs.entry_fee_rate)),
            ("exit_fee_rate", str(costs.exit_fee_rate)),
            ("adverse_funding_per_unit", str(costs.adverse_funding_per_unit)),
            ("entry_slippage_rate", str(costs.entry_slippage_rate)),
            ("exit_slippage_rate", str(costs.exit_slippage_rate)),
            ("spread_rate", "0"),
        ),
        parameter_family_hash="a" * 64,
        trial_count=1,
        trial_index=0,
        seed=1,
    )
    context = ReplayEvaluationContext(
        published_settings=published,
        evaluated_at=snapshot.decision_time + timedelta(seconds=1),
        planner_settings=planner,
        account=account,
        risk_state=state,
        open_exposures=(),
        cost_model=costs,
        feature_schema_hash="c" * 64,
        pipeline_content_hash="e" * 64,
        execution_events=(),
        reserve_risk=lambda decision, key: decision.approved and bool(key),
        opposing_signal=False,
        atr_by_symbol=(("BTCUSDT", Decimal("1")),),
    )
    evaluator = RuntimeReplayEvaluator(
        cycle=cycle,
        strategies=registry,
        gateway=gateway,
        manifest=manifest,
        context_provider=lambda _: context,
    )
    observation = ReplayObservation(
        event_id="event-1",
        event_at=snapshot.decision_time,
        available_at=snapshot.decision_time,
        snapshot=snapshot,
    )
    return evaluator, context, observation, gateway, manifest


def _first_intent_id(evaluator: RuntimeReplayEvaluator) -> str:
    return next(iter(evaluator._intent_plans))


def test_replay_evaluator_calls_published_runtime_decision_cycle_and_reserves() -> None:
    evaluator, _, observation, gateway, _ = _setup()
    reservations: list[str] = []
    context = evaluator.context_provider(observation.snapshot)
    evaluator.context_provider = lambda _: replace(
        context,
        reserve_risk=lambda decision, key: reservations.append(key) is None and decision.approved,
    )

    assert evaluator(observation.snapshot) is None
    assert len(reservations) == 1
    assert reservations[0].startswith(f"replay:{observation.snapshot.snapshot_id}:runtime:")
    assert reservations[0].endswith(":h01-v1")
    assert gateway.order(_first_intent_id(evaluator)).status.value == "ACCEPTED"


def test_replay_evaluator_owns_account_and_exposure_after_first_observation() -> None:
    evaluator, context, first, gateway, _ = _setup()
    assert evaluator(first.snapshot) is None
    intent_id = _first_intent_id(evaluator)
    pending_at = first.snapshot.decision_time + timedelta(seconds=5)
    opened = first.snapshot.decision_time + timedelta(seconds=10)
    decision_at = opened + timedelta(seconds=1)
    event = PaperBar(
        bar_key="progression-entry-bar",
        symbol="BTCUSDT",
        opened_at=opened,
        closed_at=decision_at,
        available_at=decision_at,
        open=Decimal("100"),
        high=Decimal("100.5"),
        low=Decimal("99.5"),
        close=Decimal("100"),
    )
    pending_snapshot = replace(
        first.snapshot, snapshot_id="snapshot-pending", decision_time=pending_at
    )
    second_snapshot = replace(
        first.snapshot, snapshot_id="snapshot-progressed", decision_time=decision_at
    )
    forged_exposure = OpenExposure(
        workspace_id=str(WORKSPACE_ID),
        account_id="paper-a",
        symbol="ETHUSDT",
        notional=Decimal("900000"),
        risk_amount=Decimal("900000"),
    )
    forged_account = replace(
        context.account,
        equity=Decimal("999999"),
        available_margin=Decimal("999999"),
        captured_at=first.snapshot.decision_time,
    )
    forged_state = replace(
        context.risk_state,
        evaluated_at=decision_at + timedelta(seconds=1),
        account_captured_at=forged_account.captured_at,
        market_data_as_of=decision_at,
        equity=forged_account.equity,
        available_margin=forged_account.available_margin,
        open_position_count=99,
        pending_reservation_count=0,
    )
    observed: dict[str, object] = {}

    def capture(**kwargs):
        observed.update(kwargs)
        from trading_bot.application.paper_decision_cycle import (
            DecisionCycleOutcome,
            DecisionCycleResult,
        )
        from trading_bot.domain.strategy.contracts import NoTrade

        return DecisionCycleResult(
            DecisionCycleOutcome.NO_TRADE,
            NoTrade("h01-v1", "TEST_NO_TRADE", "Kiểm thử"),
        )

    evaluator._cycle.prepare_published = capture
    evaluator.context_provider = lambda snapshot: replace(
        context,
        evaluated_at=(
            pending_at + timedelta(seconds=1)
            if snapshot.snapshot_id == "snapshot-pending"
            else decision_at + timedelta(seconds=1)
        ),
        risk_state=(
            replace(
                context.risk_state,
                evaluated_at=pending_at + timedelta(seconds=1),
                market_data_as_of=pending_at,
            )
            if snapshot.snapshot_id == "snapshot-pending"
            else forged_state
        ),
        account=context.account if snapshot.snapshot_id == "snapshot-pending" else forged_account,
        open_exposures=() if snapshot.snapshot_id == "snapshot-pending" else (forged_exposure,),
        execution_events=() if snapshot.snapshot_id == "snapshot-pending" else (event,),
    )
    evaluator(pending_snapshot)
    pending_account = observed["account"]
    pending_state = observed["risk_state"]
    assert pending_account.equity == context.account.equity
    assert pending_state.pending_reservation_count == 1
    assert pending_state.open_position_count == 0
    assert pending_state.pending_reservation_notional == evaluator._intent_plans[intent_id].notional

    evaluator(second_snapshot)

    projected_account = observed["account"]
    projected_state = observed["risk_state"]
    projected_exposures = observed["open_exposures"]
    assert projected_account.equity == context.account.equity
    assert projected_account.available_margin < context.account.available_margin
    assert projected_account.equity != forged_account.equity
    assert projected_state.open_position_count == 1
    assert projected_state.pending_reservation_count == 0
    assert projected_state.pending_reservation_initial_margin == Decimal("0")
    assert len(projected_exposures) == 1
    assert projected_exposures[0].notional == evaluator._intent_plans[intent_id].notional
    assert projected_exposures[0].symbol == "BTCUSDT"


def test_replay_evaluator_derives_position_flat_from_pending_runtime_order() -> None:
    evaluator, context, first, _, _ = _setup()
    evaluator(first.snapshot)
    decision_time = first.snapshot.decision_time + timedelta(minutes=1)
    evaluated_at = decision_time + timedelta(seconds=1)
    evaluator.context_provider = lambda _: replace(
        context,
        evaluated_at=evaluated_at,
        risk_state=replace(
            context.risk_state,
            evaluated_at=evaluated_at,
            market_data_as_of=decision_time,
        ),
        execution_events=(),
    )
    prepared_snapshots = []
    prepare = evaluator._cycle.prepare_published

    def capture(**kwargs):
        prepared_snapshots.append(kwargs["snapshot"])
        return prepare(**kwargs)

    evaluator._cycle.prepare_published = capture
    second = replace(
        first.snapshot,
        snapshot_id="position-state-check",
        decision_time=decision_time,
    )

    evaluator(second)

    assert len(prepared_snapshots) == 1
    assert prepared_snapshots[0].position_is_flat is False
    assert prepared_snapshots[0].snapshot_id.startswith("position-state-check:runtime:")


@pytest.mark.parametrize("missing_context", ["atr", "opposing_signal"])
def test_replay_evaluator_fails_closed_when_exit_recipe_context_is_missing(
    missing_context: str,
) -> None:
    evaluator, context, observation, gateway, _ = _setup()
    evaluator(observation.snapshot)
    intent_id = _first_intent_id(evaluator)
    order = gateway.order(intent_id)
    entry_time = order.eligible_at + timedelta(seconds=1)
    gateway.apply_market_event(
        PaperBar(
            bar_key=f"context-entry-{missing_context}",
            symbol="BTCUSDT",
            opened_at=entry_time,
            closed_at=entry_time + timedelta(minutes=1),
            available_at=entry_time + timedelta(minutes=1),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
        )
    )
    position = gateway.position(intent_id)
    assert position is not None
    recipe = replace(
        position.recipe,
        trailing_atr_multiple=(Decimal("2") if missing_context == "atr" else None),
        opposite_signal_exit=missing_context == "opposing_signal",
    )
    gateway._positions[intent_id] = replace(position, recipe=recipe)
    event_time = entry_time + timedelta(minutes=2)
    monitor_bar = PaperBar(
        bar_key=f"context-monitor-{missing_context}",
        symbol="BTCUSDT",
        opened_at=event_time,
        closed_at=event_time + timedelta(minutes=1),
        available_at=event_time + timedelta(minutes=1),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
    )
    current = replace(
        context,
        evaluated_at=monitor_bar.available_at + timedelta(seconds=1),
        risk_state=replace(
            context.risk_state,
            evaluated_at=monitor_bar.available_at + timedelta(seconds=1),
            market_data_as_of=monitor_bar.available_at,
        ),
        execution_events=(monitor_bar,),
        opposing_signal=None,
        atr_by_symbol=(),
    )

    with pytest.raises(ReplayEvaluationContextError, match="point-in-time"):
        evaluator._advance_execution(
            current,
            replace(observation.snapshot, decision_time=monitor_bar.available_at),
        )


def test_replay_evaluator_closes_hard_stop_without_trailing_or_opposing_context() -> None:
    evaluator, context, observation, gateway, _ = _setup()
    evaluator(observation.snapshot)
    intent_id = _first_intent_id(evaluator)
    order = gateway.order(intent_id)
    entry_time = order.eligible_at + timedelta(seconds=1)
    gateway.apply_market_event(
        PaperBar(
            bar_key="hard-stop-entry",
            symbol="BTCUSDT",
            opened_at=entry_time,
            closed_at=entry_time + timedelta(minutes=1),
            available_at=entry_time + timedelta(minutes=1),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
        )
    )
    position = gateway.position(intent_id)
    assert position is not None
    gateway._positions[intent_id] = replace(
        position,
        recipe=replace(
            position.recipe,
            trailing_atr_multiple=Decimal("2"),
            opposite_signal_exit=True,
        ),
    )
    close_time = entry_time + timedelta(minutes=2)
    stop_bar = PaperBar(
        bar_key="hard-stop-close",
        symbol="BTCUSDT",
        opened_at=close_time,
        closed_at=close_time + timedelta(minutes=1),
        available_at=close_time + timedelta(minutes=1),
        open=position.stop_price,
        high=position.stop_price + Decimal("1"),
        low=position.stop_price - Decimal("1"),
        close=position.stop_price,
    )
    current = replace(
        context,
        evaluated_at=stop_bar.available_at + timedelta(seconds=1),
        risk_state=replace(
            context.risk_state,
            evaluated_at=stop_bar.available_at + timedelta(seconds=1),
            market_data_as_of=stop_bar.available_at,
        ),
        execution_events=(stop_bar,),
        opposing_signal=None,
        atr_by_symbol=(),
    )

    trade = evaluator._advance_execution(
        current,
        replace(observation.snapshot, decision_time=stop_bar.available_at),
    )

    assert trade is not None
    assert gateway.position(intent_id).exit_reason == "STOP_LOSS"


def test_replay_account_state_counts_two_simultaneous_positions_and_risk_rejects_cap() -> None:
    evaluator, context, observation, gateway, _ = _setup()
    context = replace(
        context,
        account=replace(context.account, available_margin=Decimal("100000")),
        risk_state=replace(context.risk_state, available_margin=Decimal("100000")),
    )
    evaluator.context_provider = lambda _: context
    evaluator(observation.snapshot)
    first_intent_id = _first_intent_id(evaluator)
    first_reservation = evaluator._reservations[first_intent_id]
    first_plan = first_reservation.plan
    second_plan = replace(
        first_plan,
        snapshot_id="snapshot-second-position",
        candidate_id="candidate-second-position",
        plan_hash="",
    )
    second_plan = replace(second_plan, plan_hash=calculate_plan_hash(second_plan))
    second_key = "replay:second-position:h01-v1"
    second_order = gateway.submit(
        second_plan,
        second_key,
        submitted_at=observation.snapshot.decision_time + timedelta(seconds=2),
        eligible_after=observation.snapshot.decision_time + timedelta(seconds=2),
    )
    evaluator._intent_plans[second_order.intent_id] = second_plan
    evaluator._reservations[second_order.intent_id] = type(first_reservation)(
        second_plan, first_reservation.decision
    )
    entry_at = observation.snapshot.decision_time + timedelta(seconds=10)
    entry_bar = PaperBar(
        bar_key="two-position-entry",
        symbol="BTCUSDT",
        opened_at=entry_at,
        closed_at=entry_at + timedelta(seconds=1),
        available_at=entry_at + timedelta(seconds=1),
        open=Decimal("100"),
        high=Decimal("100.5"),
        low=Decimal("99.5"),
        close=Decimal("100"),
    )
    gateway.apply_market_event(entry_bar)

    account, state, exposures = evaluator._account_state.project(
        context,
        gateway,
        evaluator._reservations,
        candidate_symbol="BTCUSDT",
    )

    assert account.available_margin < context.account.available_margin
    assert state.open_position_count == 2
    assert state.pending_reservation_count == 0
    assert len(exposures) == 2
    assert sum((item.notional for item in exposures), Decimal("0")) == (
        first_plan.notional + second_plan.notional
    )
    capped_settings = replace(
        context.planner_settings,
        limits=replace(
            context.planner_settings.limits,
            max_positions=1,
            max_aggregate_open_risk=Decimal("100000"),
        ),
    )
    from trading_bot.domain.risk.engine import RiskEngine

    rejected = RiskEngine().evaluate(second_plan, state, capped_settings)
    assert not rejected.approved
    assert rejected.reason_code == "MAX_POSITIONS_REACHED"


@pytest.mark.parametrize("terminal_status", ["cancel", "expire"])
def test_replay_account_state_releases_terminal_unfilled_reservation(terminal_status: str) -> None:
    evaluator, context, observation, gateway, _ = _setup()
    evaluator(observation.snapshot)
    intent_id = _first_intent_id(evaluator)
    if terminal_status == "cancel":
        gateway.cancel_unfilled(intent_id)
    else:
        # Một event bắt đầu sau thời điểm hiệu lực nhưng sau ngày hết hạn sẽ đóng order.
        order = gateway.order(intent_id)
        expiration_bar = PaperBar(
            bar_key="reservation-expiration",
            symbol="BTCUSDT",
            opened_at=order.expires_at,
            closed_at=order.expires_at + timedelta(minutes=1),
            available_at=order.expires_at + timedelta(minutes=1),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
        )
        gateway.apply_market_event(expiration_bar)

    _, state, exposures = evaluator._account_state.project(
        context,
        gateway,
        evaluator._reservations,
        candidate_symbol="BTCUSDT",
    )

    assert not exposures
    assert state.pending_reservation_count == 0
    assert state.pending_reservation_risk == Decimal("0")
    assert state.pending_reservation_notional == Decimal("0")
    assert state.pending_reservation_initial_margin == Decimal("0")


def test_sequential_paper_trades_update_equity_and_next_risk_budget() -> None:
    evaluator, context, observation, gateway, _ = _setup()
    candidates: list[StrategyCandidate] = []
    prepare = evaluator._cycle.prepare_published

    def capture_candidate(**kwargs):
        result = prepare(**kwargs)
        if isinstance(result.strategy_result, StrategyCandidate):
            candidates.append(result.strategy_result)
        return result

    evaluator._cycle.prepare_published = capture_candidate
    evaluator(observation.snapshot)
    first_intent_id = _first_intent_id(evaluator)
    first_plan = evaluator._intent_plans[first_intent_id]
    first_opened = observation.snapshot.decision_time + timedelta(seconds=10)
    first_closed = first_opened + timedelta(hours=1)
    first_order = gateway.order(first_intent_id)
    if first_order.direction == "long":
        first_low = min(first_order.stop_price - Decimal("1"), Decimal("99"))
        first_high = Decimal("101")
    else:
        first_high = max(first_order.stop_price + Decimal("1"), Decimal("101"))
        first_low = Decimal("99")
    first_close_context = replace(
        context,
        evaluated_at=first_closed + timedelta(seconds=1),
        risk_state=replace(
            context.risk_state,
            evaluated_at=first_closed + timedelta(seconds=1),
            market_data_as_of=first_closed,
        ),
        execution_events=(
            PaperBar(
                bar_key="sequential-first-stop",
                symbol="BTCUSDT",
                opened_at=first_opened,
                closed_at=first_closed,
                available_at=first_closed,
                open=Decimal("100"),
                high=max(first_high, Decimal("100")),
                low=min(first_low, Decimal("100")),
                close=Decimal("100"),
            ),
        ),
    )
    first_trade = evaluator._advance_execution(
        first_close_context,
        replace(
            observation.snapshot, snapshot_id="sequential-first-close", decision_time=first_closed
        ),
    )
    assert first_trade is not None
    assert first_trade.net_pnl < 0

    next_time = first_closed + timedelta(minutes=1)
    next_context = replace(
        context,
        account=replace(context.account, captured_at=next_time),
        evaluated_at=next_time + timedelta(seconds=1),
        risk_state=replace(
            context.risk_state,
            account_captured_at=next_time,
            evaluated_at=next_time + timedelta(seconds=1),
            market_data_as_of=next_time,
        ),
        execution_events=(),
    )
    account_after_first, state_after_first, exposures_after_first = (
        evaluator._account_state.project(
            next_context,
            gateway,
            evaluator._reservations,
            candidate_symbol="BTCUSDT",
        )
    )
    assert account_after_first.equity == context.account.equity + first_trade.net_pnl
    assert not exposures_after_first
    assert state_after_first.open_position_count == 0

    second_candidate = replace(
        candidates[0],
        snapshot_id="sequential-second-snapshot",
        signal_at=next_time,
        eligible_after=next_time + timedelta(microseconds=1),
        expires_at=next_time + timedelta(hours=1),
    )
    second_plan = TradePlanner().plan(
        second_candidate,
        context.planner_settings,
        account_after_first,
        exposures_after_first,
        context.cost_model,
        now=next_time + timedelta(seconds=1),
    )
    assert isinstance(second_plan, type(first_plan))
    assert second_plan.risk_budget < first_plan.risk_budget
    second_decision = RiskEngine().evaluate(
        second_plan, state_after_first, context.planner_settings
    )
    assert second_decision.approved

    second_key = "replay:sequential-second:h01-v1"
    second_order = gateway.submit(
        second_plan,
        second_key,
        submitted_at=next_time + timedelta(seconds=1),
        eligible_after=next_time,
    )
    evaluator._intent_plans[second_order.intent_id] = second_plan
    evaluator._reservations[second_order.intent_id] = type(
        evaluator._reservations[first_intent_id]
    )(second_plan, second_decision)
    second_opened = next_time + timedelta(seconds=10)
    second_entry_bar = PaperBar(
        bar_key="sequential-second-entry",
        symbol="BTCUSDT",
        opened_at=second_opened,
        closed_at=second_opened + timedelta(seconds=1),
        available_at=second_opened + timedelta(seconds=1),
        open=Decimal("100"),
        high=Decimal("100.5"),
        low=Decimal("99.5"),
        close=Decimal("100"),
    )
    evaluator._advance_execution(
        replace(
            next_context,
            evaluated_at=second_opened + timedelta(seconds=2),
            risk_state=replace(
                next_context.risk_state,
                evaluated_at=second_opened + timedelta(seconds=2),
                market_data_as_of=second_opened + timedelta(seconds=1),
            ),
            execution_events=(second_entry_bar,),
        ),
        replace(
            observation.snapshot,
            snapshot_id="sequential-second-entry-snapshot",
            decision_time=second_opened + timedelta(seconds=1),
        ),
    )
    second_order = gateway.order(second_order.intent_id)
    if second_order.direction == "long":
        second_low = min(second_order.stop_price - Decimal("1"), Decimal("99"))
        second_high = Decimal("101")
    else:
        second_high = max(second_order.stop_price + Decimal("1"), Decimal("101"))
        second_low = Decimal("99")
    second_exit_opened = second_opened + timedelta(minutes=1)
    second_closed = second_opened + timedelta(hours=1)
    second_trade = evaluator._advance_execution(
        replace(
            next_context,
            evaluated_at=second_closed + timedelta(seconds=1),
            risk_state=replace(
                next_context.risk_state,
                evaluated_at=second_closed + timedelta(seconds=1),
                market_data_as_of=second_closed,
            ),
            execution_events=(
                PaperBar(
                    bar_key="sequential-second-stop",
                    symbol="BTCUSDT",
                    opened_at=second_exit_opened,
                    closed_at=second_closed,
                    available_at=second_closed,
                    open=Decimal("100"),
                    high=max(second_high, Decimal("100")),
                    low=min(second_low, Decimal("100")),
                    close=Decimal("100"),
                ),
            ),
        ),
        replace(
            observation.snapshot,
            snapshot_id="sequential-second-close-snapshot",
            decision_time=second_closed,
        ),
    )
    assert second_trade is not None
    final_context = replace(
        next_context,
        evaluated_at=second_closed + timedelta(minutes=1),
        risk_state=replace(
            next_context.risk_state,
            evaluated_at=second_closed + timedelta(minutes=1),
            market_data_as_of=second_closed,
        ),
    )
    final_account, final_state, final_exposures = evaluator._account_state.project(
        final_context,
        gateway,
        evaluator._reservations,
        candidate_symbol="BTCUSDT",
    )
    assert final_account.equity == (
        context.account.equity + first_trade.net_pnl + second_trade.net_pnl
    )
    assert final_state.open_position_count == 0
    assert not final_exposures


@pytest.mark.parametrize(
    "mutate",
    [
        lambda context: replace(context, feature_schema_hash="0" * 64),
        lambda context: replace(context, pipeline_content_hash="0" * 64),
        lambda context: replace(context, account=replace(context.account, is_known=False)),
        lambda context: replace(context, risk_state=replace(context.risk_state, is_known=False)),
        lambda context: replace(
            context,
            risk_state=replace(context.risk_state, account_id="other-account"),
        ),
    ],
)
def test_replay_evaluator_rejects_missing_or_mismatched_runtime_state(mutate) -> None:
    evaluator, context, observation, _, _ = _setup()
    evaluator.context_provider = lambda _: mutate(context)

    with pytest.raises(ReplayEvaluationContextError):
        evaluator(observation.snapshot)


def test_replay_evaluator_rejects_future_execution_event() -> None:
    evaluator, context, observation, _, _ = _setup()
    event = PaperBar(
        bar_key="future-bar",
        symbol="BTCUSDT",
        opened_at=observation.snapshot.decision_time,
        closed_at=observation.snapshot.decision_time + timedelta(hours=1),
        available_at=observation.snapshot.decision_time + timedelta(hours=1),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
    )
    evaluator.context_provider = lambda _: replace(context, execution_events=(event,))

    with pytest.raises(ReplayEvaluationContextError, match="chưa available"):
        evaluator(observation.snapshot)


@pytest.mark.parametrize(("ready", "collision"), [(True, False), (False, False), (False, True)])
def test_replay_evaluator_uses_shared_paper_fill_and_position_monitor(ready, collision) -> None:
    from trading_bot.domain.strategy.contracts import FeatureObservation, FeatureQuality

    evaluator, context, observation, gateway, manifest = _setup()
    extra = (
        FeatureObservation(
            "extra-ready", "extra_ready", "1h", 42.0, None, observation.snapshot.decision_time
        ),
        FeatureObservation(
            "extra-stale",
            "extra_stale",
            "1h",
            5.0,
            None,
            observation.snapshot.decision_time,
            FeatureQuality.STALE,
        ),
        FeatureObservation(
            "extra-none", "extra_none", "1h", None, None, observation.snapshot.decision_time
        ),
    )
    if collision:
        # Hai source key hợp lệ riêng nhưng trùng nhau sau khi trim key canonical.
        extra += (replace(extra[0], feature_id="extra-collision", name="extra_ready "),)
    observation = replace(
        observation,
        snapshot=replace(
            observation.snapshot,
            features=observation.snapshot.features + extra,
        ),
    )
    assert evaluator(observation.snapshot) is None
    intent_id = _first_intent_id(evaluator)
    order = gateway.order(intent_id)
    assert order.status is OrderStatus.ACCEPTED
    assert evaluator._reservations[intent_id].plan == evaluator._intent_plans[intent_id]
    if collision:
        assert evaluator._intent_entry_provenance[intent_id] is None
    if not ready and not collision:
        # Intent không được evaluator ghi bằng chứng vẫn phải đóng như legacy.
        evaluator._intent_entry_provenance.clear()
    assert order.stop_price > 0

    opened = observation.snapshot.decision_time + timedelta(seconds=10)
    closed = opened + timedelta(hours=1)
    if order.direction == "long":
        low = min(order.stop_price - Decimal("1"), Decimal("99"))
        high = Decimal("101")
    else:
        high = max(order.stop_price + Decimal("1"), Decimal("101"))
        low = Decimal("99")
    event = PaperBar(
        bar_key="exit-stop-bar",
        symbol="BTCUSDT",
        opened_at=opened,
        closed_at=closed,
        available_at=closed,
        open=Decimal("100"),
        high=max(high, Decimal("100")),
        low=min(low, Decimal("100")),
        close=Decimal("100"),
    )
    later_snapshot = replace(
        observation.snapshot,
        snapshot_id="snapshot-exit",
        decision_time=closed,
    )
    later_state = replace(
        context.risk_state,
        evaluated_at=closed + timedelta(seconds=1),
        market_data_as_of=closed,
    )
    evaluator.context_provider = lambda _: replace(
        context,
        evaluated_at=closed + timedelta(seconds=1),
        risk_state=later_state,
        execution_events=(event,),
    )

    trade = evaluator(later_snapshot)

    assert trade is not None
    assert trade.trade_id == gateway.position(intent_id).position_id
    assert trade.opened_at == opened
    assert trade.closed_at == closed
    assert trade.fees > 0
    assert trade.slippage == 0
    assert gateway.position(intent_id).is_closed
    assert trade.has_entry_provenance is ready
    if ready:
        assert trade.entry_decision_snapshot_id.startswith(
            f"{observation.snapshot.snapshot_id}:runtime:"
        )
        assert "snapshot-exit" not in trade.entry_decision_snapshot_id
        assert trade.entry_decision_at == observation.snapshot.decision_time
        assert trade.entry_feature_schema_hash == manifest.feature_schema_hash
        assert trade.entry_symbol == "BTCUSDT"
        assert ("1h:extra_ready", 42.0) in trade.entry_features
        assert not {"1h:extra_stale", "1h:extra_none"} & dict(trade.entry_features).keys()
    else:
        assert trade.entry_features == ()
        assert trade.entry_features_hash is None
        assert trade.entry_symbol is None

    next_time = closed + timedelta(minutes=1)
    next_snapshot = replace(
        later_snapshot,
        snapshot_id="snapshot-after-close",
        decision_time=next_time,
    )
    projected: dict[str, object] = {}

    def capture(**kwargs):
        projected.update(kwargs)
        from trading_bot.application.paper_decision_cycle import (
            DecisionCycleOutcome,
            DecisionCycleResult,
        )
        from trading_bot.domain.strategy.contracts import NoTrade

        return DecisionCycleResult(
            DecisionCycleOutcome.NO_TRADE,
            NoTrade("h01-v1", "TEST_NO_TRADE", "Kiểm thử"),
        )

    evaluator._cycle.prepare_published = capture
    evaluator.context_provider = lambda _: replace(
        context,
        evaluated_at=next_time + timedelta(seconds=1),
        risk_state=replace(
            context.risk_state,
            evaluated_at=next_time + timedelta(seconds=1),
            market_data_as_of=next_time,
        ),
        execution_events=(),
    )
    evaluator(next_snapshot)

    assert projected["account"].equity == context.account.equity + trade.net_pnl
    assert projected["risk_state"].open_position_count == 0
    assert projected["risk_state"].pending_reservation_count == 0


class _PersistenceSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.statements: list[str] = []

    async def scalar(self, statement):
        from sqlalchemy.dialects import postgresql

        from trading_bot.adapters.database.models.backtest_entities import (
            BacktestTrialRecord,
        )

        compiled = str(statement.compile(dialect=postgresql.dialect()))
        self.statements.append(compiled)
        if "FROM backtest_trial" in compiled:
            return next(
                (row for row in self.added if isinstance(row, BacktestTrialRecord)),
                None,
            )
        return None

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        return None

    @asynccontextmanager
    async def begin_nested(self):
        yield


@pytest.mark.asyncio
async def test_runner_composes_runtime_evaluator_into_persistable_report_and_trade() -> None:
    evaluator, context, first, gateway, manifest = _setup()
    opened = first.snapshot.decision_time + timedelta(seconds=10)
    closed = opened + timedelta(hours=1)
    later_snapshot = replace(
        first.snapshot,
        snapshot_id="snapshot-exit",
        decision_time=closed,
    )

    def provider(snapshot):
        if snapshot.snapshot_id != "snapshot-exit":
            return context
        intent_id = _first_intent_id(evaluator)
        order = gateway.order(intent_id)
        if order.direction == "long":
            low = min(order.stop_price - Decimal("1"), Decimal("99"))
            high = Decimal("101")
        else:
            high = max(order.stop_price + Decimal("1"), Decimal("101"))
            low = Decimal("99")
        event = PaperBar(
            bar_key="runner-close-stop",
            symbol="BTCUSDT",
            opened_at=opened,
            closed_at=closed,
            available_at=closed,
            open=Decimal("100"),
            high=max(high, Decimal("100")),
            low=min(low, Decimal("100")),
            close=Decimal("100"),
        )
        return replace(
            context,
            evaluated_at=closed + timedelta(seconds=1),
            risk_state=replace(
                context.risk_state,
                evaluated_at=closed + timedelta(seconds=1),
                market_data_as_of=closed,
            ),
            execution_events=(event,),
        )

    evaluator.context_provider = provider
    observations = (
        first,
        ReplayObservation("event-close", closed, closed, later_snapshot),
    )
    replay = run_runtime_paper_replay(
        observations,
        manifest=manifest,
        runtime_evaluator_factory=lambda scenario, _: evaluator,
    )

    replay_run = replay.runs[0]
    assert replay_run.sensitivity_run.scenario == SensitivityScenario("base", "BASE")
    assert replay_run.sensitivity_run.result.trade_count == 1
    assert len(replay_run.trade_outcomes) == 1
    intent_id = _first_intent_id(evaluator)
    assert replay_run.trade_outcomes[0].trade_id == gateway.position(intent_id).position_id
    assert replay_run.sensitivity_run.result.net_pnl == replay_run.trade_outcomes[0].net_pnl

    session = _PersistenceSession()
    persisted = await BacktestOutcomeRepository(session).append_sensitivity_run(
        workspace_id=WORKSPACE_ID,
        sensitivity_run=replay_run.sensitivity_run,
        trade_outcomes=replay_run.trade_outcomes,
    )

    assert persisted.trade_count == 1
    assert len(persisted.outcomes_hash) == 64
    assert len(session.added) == 5
    run, trial, report, scenario, outcome = session.added
    assert run.manifest_hash == replay_run.sensitivity_run.manifest.content_hash
    assert trial.trial_id == replay_run.sensitivity_run.trial_id
    assert report.report_hash == replay_run.sensitivity_run.result.report_hash
    assert scenario.scenario_id == "base"
    assert outcome.trade_id == replay_run.trade_outcomes[0].trade_id
    assert all("workspace_id =" in statement for statement in session.statements)


def test_runtime_sensitivity_builds_fresh_cost_context_and_fill_gateway_per_trial() -> None:
    _, base_context, first, initial_gateway, manifest = _setup()
    base_context = replace(
        base_context,
        cost_model=replace(base_context.cost_model, adverse_funding_per_unit=Decimal("0")),
    )
    manifest = replace(
        manifest,
        cost_assumptions=tuple(
            (key, "0" if key == "adverse_funding_per_unit" else value)
            for key, value in manifest.cost_assumptions
        ),
    )
    opened = first.snapshot.decision_time + timedelta(seconds=10)
    closed = opened + timedelta(hours=1)
    later_snapshot = replace(first.snapshot, snapshot_id="sensitivity-exit", decision_time=closed)
    observations = (
        first,
        ReplayObservation("sensitivity-close", closed, closed, later_snapshot),
    )
    scenarios = (
        SensitivityScenario("base", "BASE"),
        SensitivityScenario(
            "higher-fee",
            "COST",
            cost_overrides=(("entry_fee_rate", "0.002"), ("exit_fee_rate", "0.002")),
        ),
        SensitivityScenario(
            "higher-slippage",
            "COST",
            cost_overrides=(
                ("entry_slippage_rate", "0.01"),
                ("exit_slippage_rate", "0.01"),
            ),
        ),
    )
    built: list[RuntimeReplayEvaluator] = []

    def factory(scenario, scenario_manifest):
        assumptions = dict(scenario_manifest.cost_assumptions)
        entry_fee = Decimal(assumptions["entry_fee_rate"])
        exit_fee = Decimal(assumptions["exit_fee_rate"])
        entry_slippage = Decimal(assumptions["entry_slippage_rate"])
        exit_slippage = Decimal(assumptions["exit_slippage_rate"])
        costs = replace(
            base_context.cost_model,
            entry_fee_rate=entry_fee,
            exit_fee_rate=exit_fee,
            entry_slippage_rate=entry_slippage,
            exit_slippage_rate=exit_slippage,
        )
        context = replace(base_context, cost_model=costs)
        gateway = PaperExecutionGateway(
            replace(
                initial_gateway.policy,
                fee_rate=entry_fee,
                slippage_rate=entry_slippage,
            )
        )
        registry = StrategyRegistry()
        cycle = PaperDecisionCycle(registry, TradePlanner(), RiskEngine(), gateway)

        def provider(snapshot):
            if snapshot.snapshot_id != "sensitivity-exit":
                return context
            intent_id = _first_intent_id(evaluator)
            order = gateway.order(intent_id)
            stop_low = order.stop_price - Decimal("1")
            stop_high = order.stop_price + Decimal("1")
            event = PaperBar(
                bar_key="sensitivity-stop-" + scenario.scenario_id,
                symbol="BTCUSDT",
                opened_at=opened,
                closed_at=closed,
                available_at=closed,
                open=Decimal("100"),
                high=max(stop_high, Decimal("101")),
                low=min(stop_low, Decimal("99")),
                close=Decimal("100"),
            )
            return replace(
                context,
                evaluated_at=closed + timedelta(seconds=1),
                risk_state=replace(
                    context.risk_state,
                    evaluated_at=closed + timedelta(seconds=1),
                    market_data_as_of=closed,
                ),
                execution_events=(event,),
            )

        evaluator = RuntimeReplayEvaluator(
            cycle=cycle,
            strategies=registry,
            gateway=gateway,
            manifest=scenario_manifest,
            context_provider=provider,
            scenario=scenario,
        )
        built.append(evaluator)
        return evaluator

    comparison = run_runtime_paper_replay(
        observations,
        manifest=manifest,
        scenarios=scenarios,
        runtime_evaluator_factory=factory,
    )

    assert len(comparison.runs) == 3
    assert comparison.is_complete
    assert built[0] is not built[1]
    assert built[0].gateway is not built[1].gateway
    assert built[0].context_provider is not built[1].context_provider
    base, fee_stressed, slippage_stressed = comparison.runs
    assert base.sensitivity_run.result.trade_count == 1
    assert fee_stressed.sensitivity_run.result.trade_count == 1
    assert slippage_stressed.sensitivity_run.result.trade_count == 1
    assert base.trade_outcomes[0].fees < fee_stressed.trade_outcomes[0].fees
    assert base.sensitivity_run.result.net_pnl > fee_stressed.sensitivity_run.result.net_pnl
    assert base.trade_outcomes[0].slippage < slippage_stressed.trade_outcomes[0].slippage
    assert slippage_stressed.sensitivity_run.result.slippage == (
        slippage_stressed.trade_outcomes[0].slippage
    )
    assert base.sensitivity_run.result.net_pnl > slippage_stressed.sensitivity_run.result.net_pnl
    intent_id = _first_intent_id(built[2])
    slippage_fills = built[2].gateway.order(intent_id).fills
    slippage_position = built[2].gateway.position(intent_id)
    assert len(slippage_fills) == 1
    assert slippage_fills[0].slippage_cost > Decimal("0")
    assert slippage_position.slippage_cost > slippage_fills[0].slippage_cost


def test_runtime_sensitivity_rejects_unsupported_cost_override_before_factory() -> None:
    evaluator, _, observation, _, manifest = _setup()
    factory_calls: list[str] = []
    scenarios = (
        SensitivityScenario("base", "BASE"),
        SensitivityScenario(
            "slippage-stress",
            "COST",
            cost_overrides=(("entry_slippage_rate", "0.01"),),
        ),
    )

    def factory(scenario, current_manifest):
        factory_calls.append(scenario.scenario_id)
        return RuntimeReplayEvaluator(
            cycle=evaluator._cycle,
            strategies=StrategyRegistry(),
            gateway=evaluator.gateway,
            manifest=current_manifest,
            context_provider=evaluator.context_provider,
            scenario=scenario,
        )

    with pytest.raises(ReplayEvaluationContextError, match="chỉ hỗ trợ cặp"):
        run_runtime_paper_replay(
            (observation,),
            manifest=manifest,
            scenarios=scenarios,
            runtime_evaluator_factory=factory,
        )

    assert factory_calls == ["base"]


def test_runtime_sensitivity_rejects_reused_gateway_and_context_provider() -> None:
    evaluator, _, observation, _, manifest = _setup()
    scenarios = (
        SensitivityScenario("base", "BASE"),
        SensitivityScenario(
            "fee-stress",
            "COST",
            cost_overrides=(("entry_fee_rate", "0.002"), ("exit_fee_rate", "0.002")),
        ),
    )

    def factory(scenario, scenario_manifest):
        return RuntimeReplayEvaluator(
            cycle=evaluator._cycle,
            strategies=StrategyRegistry(),
            gateway=evaluator.gateway,
            manifest=scenario_manifest,
            context_provider=evaluator.context_provider,
            scenario=scenario,
        )

    with pytest.raises(ReplayEvaluationContextError, match="gateway và context provider mới"):
        run_runtime_paper_replay(
            (observation,),
            manifest=manifest,
            scenarios=scenarios,
            runtime_evaluator_factory=factory,
        )


def test_empty_ready_features_cannot_gain_entry_provenance():
    evaluator, _, observation, _, _ = _setup()
    empty = replace(observation.snapshot, features=())
    assert evaluator._capture_entry_provenance(empty) is None


@pytest.mark.parametrize("recorded_none", [False, True])
def test_retry_keeps_recorded_entry_provenance_without_recapturing(monkeypatch, recorded_none):
    evaluator, _, observation, gateway, _ = _setup()
    prepared = []
    runtime_snapshots = []
    prepare = evaluator._cycle.prepare_published

    def record_prepared(**kwargs):
        result = prepare(**kwargs)
        prepared.append(result)
        runtime_snapshots.append(kwargs["snapshot"])
        return result

    monkeypatch.setattr(evaluator._cycle, "prepare_published", record_prepared)
    assert evaluator(observation.snapshot) is None
    intent_id = _first_intent_id(evaluator)
    if recorded_none:
        # Mô phỏng kết quả capture vắng mặt đã được ghi ở lần submit đầu.
        evaluator._intent_entry_provenance[intent_id] = None
    first_provenance = evaluator._intent_entry_provenance[intent_id]
    first_order = gateway.order(intent_id)
    first_plan = evaluator._intent_plans[intent_id]

    def reject_recapture(_):
        pytest.fail("Retry không được đánh giá lại optional entry provenance.")

    # Phát lại đúng quyết định đã duyệt và khóa submit cũ qua gateway thật.
    monkeypatch.setattr(evaluator, "_runtime_snapshot", lambda *args: runtime_snapshots[0])
    monkeypatch.setattr(evaluator._cycle, "prepare_published", lambda **kwargs: prepared[0])
    monkeypatch.setattr(evaluator, "_capture_entry_provenance", reject_recapture)

    assert evaluator(observation.snapshot) is None
    assert gateway.order(intent_id) == first_order
    assert len(gateway.open_orders()) == 1
    assert evaluator._intent_entry_provenance[intent_id] is first_provenance
    assert tuple(evaluator._intent_plans) == (intent_id,)
    assert evaluator._intent_plans[intent_id] == first_plan
    assert evaluator._reservations[intent_id].plan == first_plan


def test_pinned_replay_captures_exact_cycle_vector_and_original_entry_event(monkeypatch):
    from tests.unit.test_backtest_decision_opportunities import _pinned_setup

    from trading_bot.application.learning_feature_vector import build_learning_feature_vector
    from trading_bot.domain.strategy.rules import H01EmaTrendRule

    source, observations, seed, manifest, factory = _pinned_setup(cross=True)
    seen = []
    original = H01EmaTrendRule.evaluate

    def capture(self, snapshot, selection):
        result = original(self, snapshot, selection)
        seen.append((snapshot, result))
        return result

    monkeypatch.setattr(H01EmaTrendRule, "evaluate", capture)
    comparison = run_runtime_paper_replay(
        observations, manifest=manifest, runtime_evaluator_factory=factory
    )
    run = comparison.runs[0]
    assert comparison.is_complete
    assert len(run.opportunities) == len(observations)
    assert len(seen) == len(observations)
    first = run.opportunities[0]
    assert first.strategy_state == "CANDIDATE"
    assert first.candidate_id == seen[0][1].candidate_id
    assert first.decision_outcome == "PAPER_INTENT_CREATED"
    for opportunity, (snapshot, _) in zip(run.opportunities, seen, strict=True):
        vector = build_learning_feature_vector(
            snapshot,
            strategy_release_id=manifest.strategy_release_id,
            strategy_release_content_hash=manifest.strategy_content_hash,
        )
        assert opportunity.runtime_snapshot_id == snapshot.snapshot_id == vector.snapshot_id
        assert opportunity.vector_hash == vector.vector_hash
        assert opportunity.feature_schema_hash == vector.feature_schema_hash
        from trading_bot.application.backtest_decision_opportunities import metadata_hash

        expected_refs = [
            {"timeframe": part.timeframe, "source_event_digest": part.source_event_digest}
            for part in vector.timeframe_lineage
        ]
        assert opportunity.source_refs_digest == metadata_hash(expected_refs)
        assert ":runtime:" not in snapshot.snapshot_id
    assert run.trade_outcomes
    trade = run.trade_outcomes[0]
    assert trade.entry_opportunity_event_id == observations[0].event_id
    assert trade.entry_candidate_id == first.candidate_id
    assert trade.entry_decision_snapshot_id == first.runtime_snapshot_id
    assert trade.entry_feature_schema_hash == first.feature_schema_hash
    assert trade.closed_at > first.decision_at


def test_pinned_risk_rejection_retains_candidate_identity(monkeypatch):
    from tests.unit.test_backtest_decision_opportunities import _pinned_setup

    from trading_bot.application.backtest import BacktestRunner, EvaluationReport

    source, observations, seed, manifest, factory = _pinned_setup(cross=True)
    from trading_bot.domain.strategy.rules import H01EmaTrendRule

    candidates = []
    original = H01EmaTrendRule.evaluate

    def capture(self, snapshot, selection):
        result = original(self, snapshot, selection)
        if isinstance(result, StrategyCandidate):
            candidates.append(result)
        return result

    monkeypatch.setattr(H01EmaTrendRule, "evaluate", capture)
    rejected_seed = replace(
        seed, risk_state=replace(seed.risk_state, realized_loss_today=Decimal("1000"))
    )
    factory = replace(factory, context_seed_factory=lambda _scenario, _manifest: rejected_seed)
    evaluator = factory(SensitivityScenario("base", "BASE"), manifest)
    result = BacktestRunner().run(observations, manifest=manifest, evaluator=evaluator)
    assert isinstance(result, EvaluationReport)
    opportunity = evaluator.opportunities[0]
    assert opportunity.strategy_state == "CANDIDATE"
    assert opportunity.candidate_id == candidates[0].candidate_id
    assert opportunity.decision_outcome == "RISK_REJECTED"
    assert not evaluator.gateway.open_orders()


def test_pre_strategy_config_stop_is_not_run_not_no_candidate():
    from tests.unit.test_backtest_decision_opportunities import _pinned_setup

    from trading_bot.application.backtest import BacktestRunner, EvaluationReport

    _, observations, seed, manifest, factory = _pinned_setup()
    blocked = replace(
        seed, published_settings=replace(seed.published_settings, effective_at=manifest.range_end)
    )
    factory = replace(factory, context_seed_factory=lambda _scenario, _manifest: blocked)
    evaluator = factory(SensitivityScenario("base", "BASE"), manifest)
    result = BacktestRunner().run(observations, manifest=manifest, evaluator=evaluator)
    assert isinstance(result, EvaluationReport)
    assert all(item.strategy_state == "NOT_RUN" for item in evaluator.opportunities)
    assert all(item.input_state == "EVALUATED" for item in evaluator.opportunities)
    assert all(item.reason_code == "SETTINGS_NOT_EFFECTIVE" for item in evaluator.opportunities)


def test_legacy_replay_does_not_fabricate_opportunity_proof_metadata():
    from tests.fixtures.runtime_replay import build_h01_dataset_runtime_replay

    _, comparison = build_h01_dataset_runtime_replay(WORKSPACE_ID)
    assert comparison.is_complete
    assert comparison.runs[0].sensitivity_run.result.trade_count == 0
    assert comparison.runs[0].trade_outcomes == ()
    assert comparison.runs[0].opportunities == ()


def test_pinned_nonflat_cycle_snapshot_keeps_canonical_vector_identity(monkeypatch):
    from tests.unit.test_backtest_decision_opportunities import _pinned_setup

    from trading_bot.application.learning_feature_vector import build_learning_feature_vector
    from trading_bot.domain.strategy.rules import H01EmaTrendRule

    _, observations, _, manifest, factory = _pinned_setup(cross=True, close_on_next=False)
    seen = []
    original = H01EmaTrendRule.evaluate

    def capture(self, snapshot, selection):
        seen.append(snapshot)
        return original(self, snapshot, selection)

    monkeypatch.setattr(H01EmaTrendRule, "evaluate", capture)
    comparison = run_runtime_paper_replay(
        observations, manifest=manifest, runtime_evaluator_factory=factory
    )
    assert comparison.is_complete
    assert any(snapshot.position_is_flat is False for snapshot in seen)
    for snapshot, opportunity in zip(seen, comparison.runs[0].opportunities, strict=True):
        vector = build_learning_feature_vector(
            snapshot,
            strategy_release_id=manifest.strategy_release_id,
            strategy_release_content_hash=manifest.strategy_content_hash,
        )
        assert vector.snapshot_id == opportunity.runtime_snapshot_id
        assert vector.vector_hash == opportunity.vector_hash
        if snapshot.position_is_flat is False:
            assert opportunity.reason_code == "H01_POSITION_NOT_FLAT"
            assert opportunity.strategy_state == "NO_CANDIDATE"


def test_pinned_tampered_features_are_excluded_before_canonical_rebuild(monkeypatch):
    from tests.unit.test_backtest_decision_opportunities import _pinned_setup

    from trading_bot.application.backtest import BacktestRunner, EvaluationReport
    from trading_bot.domain.strategy.rules import H01EmaTrendRule

    _, observations, _, manifest, factory = _pinned_setup()
    original_first = observations[0]
    changed = replace(
        original_first,
        snapshot=replace(
            original_first.snapshot,
            features=(
                replace(
                    original_first.snapshot.features[0],
                    value=original_first.snapshot.features[0].value + 0.25,
                ),
                *original_first.snapshot.features[1:],
            ),
        ),
    )
    changed_observations = (changed, *observations[1:])
    factory = replace(factory, observations=changed_observations)
    evaluator = factory(SensitivityScenario("base", "BASE"), manifest)
    seen = []
    original = H01EmaTrendRule.evaluate

    def capture(self, snapshot, selection):
        seen.append(snapshot)
        return original(self, snapshot, selection)

    monkeypatch.setattr(H01EmaTrendRule, "evaluate", capture)
    result = BacktestRunner().run(changed_observations, manifest=manifest, evaluator=evaluator)
    assert isinstance(result, EvaluationReport)
    assert len(seen) == len(observations) - 1
    assert evaluator.opportunities[0].input_state == "INPUT_EXCLUDED"
    assert evaluator.opportunities[0].reason_code == "FEATURE_VECTOR_SNAPSHOT_CONTENT_MISMATCH"
    assert evaluator.opportunities[0].vector_hash is None


def test_pinned_research_input_tampering_fails_before_runtime_rebuild():
    from tests.unit.test_backtest_decision_opportunities import _pinned_setup

    from trading_bot.application.backtest import BacktestRunner, ReplayRejected

    _, observations, _, manifest, factory = _pinned_setup(intent="RESEARCH")
    first = observations[0]
    altered = replace(
        first,
        snapshot=replace(
            first.snapshot,
            features=(
                replace(first.snapshot.features[0], value=first.snapshot.features[0].value + 0.25),
                *first.snapshot.features[1:],
            ),
        ),
    )
    altered_observations = (altered, *observations[1:])
    evaluator = replace(factory, observations=altered_observations)(
        SensitivityScenario("base", "BASE"), manifest
    )
    result = BacktestRunner().run(altered_observations, manifest=manifest, evaluator=evaluator)
    assert isinstance(result, ReplayRejected)
    assert result.reason_code == "EVALUATOR_FAILED"
    assert result.trial_record.error_type == "ReplayEvaluationContextError"
    assert evaluator.opportunities == ()
    assert not evaluator.gateway.open_orders()


def test_replay_account_state_mark_to_market_unrealized_pnl() -> None:
    """Kiểm tra ReplayAccountState tính đúng Mark-to-Market UPNL cho vị thế mở."""
    evaluator, context, first, gateway, _ = _setup()
    assert evaluator(first.snapshot) is None
    intent_id = _first_intent_id(evaluator)
    opened = first.snapshot.decision_time + timedelta(seconds=10)
    decision_at = opened + timedelta(seconds=1)
    event = PaperBar(
        bar_key="mtm-fill-bar",
        symbol="BTCUSDT",
        opened_at=opened,
        closed_at=decision_at,
        available_at=decision_at,
        open=Decimal("100"),
        high=Decimal("100.5"),
        low=Decimal("99.5"),
        close=Decimal("100"),
    )
    gateway.apply_market_event(event)
    position = gateway.position(intent_id)
    assert position is not None
    assert not position.is_closed

    state = evaluator._account_state
    assert state is not None

    # Khi không truyền mark_prices, UPNL mặc định bằng 0, equity giữ nguyên
    account_no_mtm, _, _ = state.project(
        context, gateway, evaluator._reservations, candidate_symbol="BTCUSDT"
    )
    assert state.unrealized_pnl == Decimal("0")
    assert account_no_mtm.equity == context.account.equity

    # Khi giá tăng lên 105: lãi thả nổi (105 - entry_price) * quantity
    diff_gain = Decimal("105") - position.entry_execution_price
    expected_gain = diff_gain * position.quantity * position.contract_multiplier
    account_gain, _, _ = state.project(
        context,
        gateway,
        evaluator._reservations,
        candidate_symbol="BTCUSDT",
        mark_prices={"BTCUSDT": Decimal("105")},
    )
    assert state.unrealized_pnl == expected_gain
    assert account_gain.equity == context.account.equity + expected_gain

    # Khi giá giảm xuống 95: lỗ thả nổi (95 - entry_price) * quantity
    diff_loss = Decimal("95") - position.entry_execution_price
    expected_loss = diff_loss * position.quantity * position.contract_multiplier
    account_loss, _, _ = state.project(
        context,
        gateway,
        evaluator._reservations,
        candidate_symbol="BTCUSDT",
        mark_prices={"BTCUSDT": Decimal("95")},
    )
    assert state.unrealized_pnl == expected_loss
    assert account_loss.equity == context.account.equity + expected_loss


def test_replay_evaluator_mark_to_market_mode() -> None:
    """Kiểm tra RuntimeReplayEvaluator khi bật mark_to_market=True tự động nạp mark prices."""
    evaluator, context, first, gateway, manifest = _setup()
    mtm_evaluator = RuntimeReplayEvaluator(
        cycle=evaluator._cycle,
        strategies=evaluator._strategies,
        gateway=gateway,
        manifest=manifest,
        context_provider=evaluator.context_provider,
        mark_to_market=True,
    )
    assert mtm_evaluator(first.snapshot) is None
    opened = first.snapshot.decision_time + timedelta(seconds=10)
    decision_at = opened + timedelta(seconds=1)
    event = PaperBar(
        bar_key="mtm-evaluator-bar",
        symbol="BTCUSDT",
        opened_at=opened,
        closed_at=decision_at,
        available_at=decision_at,
        open=Decimal("100"),
        high=Decimal("100.5"),
        low=Decimal("99.5"),
        close=Decimal("100"),
    )
    second_snapshot = replace(
        first.snapshot,
        snapshot_id="snapshot-progressed-mtm",
        decision_time=decision_at,
    )
    observed_account = []

    def capture(**kwargs):
        observed_account.append(kwargs["account"])
        from trading_bot.application.paper_decision_cycle import (
            DecisionCycleOutcome,
            DecisionCycleResult,
        )
        from trading_bot.domain.strategy.contracts import NoTrade

        return DecisionCycleResult(
            DecisionCycleOutcome.NO_TRADE,
            NoTrade("h01-v1", "TEST_NO_TRADE", "Kiểm thử"),
        )

    mtm_evaluator._cycle.prepare_published = capture
    mtm_evaluator.context_provider = lambda snapshot: replace(
        context,
        evaluated_at=decision_at + timedelta(seconds=1),
        risk_state=replace(
            context.risk_state,
            evaluated_at=decision_at + timedelta(seconds=1),
            market_data_as_of=decision_at,
        ),
        execution_events=(event,),
    )
    mtm_evaluator(second_snapshot)
    assert len(observed_account) == 1
    # first.snapshot.series[-1] bar close là 102, entry là 100 -> gain = (102 - 100) * 99 = 198
    assert observed_account[0].equity == Decimal("10198.000")
