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


def test_replay_evaluator_calls_published_runtime_decision_cycle_and_reserves() -> None:
    evaluator, _, observation, gateway, _ = _setup()
    reservations: list[str] = []
    context = evaluator.context_provider(observation.snapshot)
    evaluator.context_provider = lambda _: replace(
        context,
        reserve_risk=lambda decision, key: reservations.append(key) is None and decision.approved,
    )

    assert evaluator(observation.snapshot) is None
    assert reservations == [f"replay:{observation.snapshot.snapshot_id}:h01-v1"]
    assert gateway.order("paper:replay:snapshot-test-v1:h01-v1").status.value == "ACCEPTED"


def test_replay_evaluator_owns_account_and_exposure_after_first_observation() -> None:
    evaluator, context, first, gateway, _ = _setup()
    assert evaluator(first.snapshot) is None
    intent_id = f"paper:replay:{first.snapshot.snapshot_id}:h01-v1"
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


def test_replay_account_state_counts_two_simultaneous_positions_and_risk_rejects_cap() -> None:
    evaluator, context, observation, gateway, _ = _setup()
    context = replace(
        context,
        account=replace(context.account, available_margin=Decimal("100000")),
        risk_state=replace(context.risk_state, available_margin=Decimal("100000")),
    )
    evaluator.context_provider = lambda _: context
    evaluator(observation.snapshot)
    first_intent_id = f"paper:replay:{observation.snapshot.snapshot_id}:h01-v1"
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
    intent_id = f"paper:replay:{observation.snapshot.snapshot_id}:h01-v1"
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
    first_intent_id = f"paper:replay:{observation.snapshot.snapshot_id}:h01-v1"
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


def test_replay_evaluator_uses_shared_paper_fill_and_position_monitor() -> None:
    evaluator, context, observation, gateway, _ = _setup()
    assert evaluator(observation.snapshot) is None
    intent_id = f"paper:replay:{observation.snapshot.snapshot_id}:h01-v1"
    order = gateway.order(intent_id)
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
    intent_id = f"paper:replay:{first.snapshot.snapshot_id}:h01-v1"
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
        intent_id = f"paper:replay:{first.snapshot.snapshot_id}:h01-v1"

        def provider(snapshot):
            if snapshot.snapshot_id != "sensitivity-exit":
                return context
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
    intent_id = f"paper:replay:{first.snapshot.snapshot_id}:h01-v1"
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
