from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from tests.unit.test_risk_engine import _settings as _planner_settings
from tests.unit.test_strategy_hypotheses import _h01_snapshot, _h03_snapshot
from tests.unit.test_workspace_settings import valid_payload

from trading_bot.application.paper_decision_cycle import (
    DecisionCycleOutcome,
    PaperDecisionCycle,
    strategy_selection_from_published_settings,
)
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings
from trading_bot.domain.execution.paper import (
    OrderStatus,
    PaperExecutionGateway,
    PaperExecutionPolicy,
)
from trading_bot.domain.risk.engine import RiskDecision, RiskEngine, RiskState
from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    CostModel,
    PlanRejected,
    TradePlan,
    TradePlanner,
)
from trading_bot.domain.strategy.contracts import NoTrade, StrategyCandidate, StrategySelection
from trading_bot.domain.strategy.registry import StrategyRegistry

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def test_published_root_settings_selects_the_configured_release_only() -> None:
    payload = valid_payload()
    strategy = payload["strategy"]
    assert isinstance(strategy, dict)
    strategy["default_release"] = "h03-v1"
    settings = WorkspaceSettings.model_validate(payload)

    selection = strategy_selection_from_published_settings(
        settings,
        settings_version_id="settings-v9",
        is_published=True,
    )

    assert selection.active_release_ids == ("h03-v1",)
    assert selection.settings_version_id == "settings-v9"
    assert selection.published is True


def test_published_h03_settings_apply_supported_strategy_parameters() -> None:
    payload = valid_payload()
    strategy = payload["strategy"]
    assert isinstance(strategy, dict)
    strategy["default_release"] = "h03-v1"
    strategy["parameters"] = {"donchian_lookback": 63, "volume_median_lookback": 25}
    settings = WorkspaceSettings.model_validate(payload)

    selection = strategy_selection_from_published_settings(
        settings,
        settings_version_id="settings-v10",
        is_published=True,
    )

    assert selection.parameters == (
        ("donchian_lookback", 63),
        ("volume_median_lookback", 25),
    )


def test_root_strategy_parameters_change_runtime_h03_signal() -> None:
    from dataclasses import replace

    base = _h03_snapshot()
    series = base.get_series("1h")
    assert series is not None
    old_high = replace(series.bars[10], high=Decimal("110"))
    snapshot = replace(
        base,
        series=(replace(series, bars=series.bars[:10] + (old_high,) + series.bars[11:]),),
    )
    payload = valid_payload()
    strategy = payload["strategy"]
    assert isinstance(strategy, dict)
    strategy["default_release"] = "h03-v1"
    strategy["parameters"] = {"donchian_lookback": 20}
    settings = WorkspaceSettings.model_validate(payload)
    selection = strategy_selection_from_published_settings(
        settings,
        settings_version_id="settings-h03-short-channel",
        is_published=True,
    )

    result = StrategyRegistry().resolve("h03-v1", selection).evaluate(snapshot, selection)

    assert isinstance(result, StrategyCandidate)
    assert result.reason_code == "H03_LONG_ENTRY"


@pytest.mark.parametrize(
    "parameters",
    [
        {"unknown_parameter": 50},
        {"donchian_lookback": True},
        {"donchian_lookback": 19},
        {"donchian_lookback": 54.0},
    ],
)
def test_published_h03_settings_reject_unsupported_parameters(parameters) -> None:
    payload = valid_payload()
    strategy = payload["strategy"]
    assert isinstance(strategy, dict)
    strategy["default_release"] = "h03-v1"
    strategy["parameters"] = parameters
    settings = WorkspaceSettings.model_validate(payload)

    with pytest.raises(ValueError, match="."):
        strategy_selection_from_published_settings(
            settings,
            settings_version_id="settings-v11",
            is_published=True,
        )


def test_strategy_without_parameter_contract_rejects_parameterized_settings() -> None:
    payload = valid_payload()
    strategy = payload["strategy"]
    assert isinstance(strategy, dict)
    strategy["default_release"] = "h01-v1"
    strategy["parameters"] = {"donchian_lookback": 60}
    settings = WorkspaceSettings.model_validate(payload)

    with pytest.raises(ValueError, match="chưa hỗ trợ tham số"):
        strategy_selection_from_published_settings(
            settings,
            settings_version_id="settings-v12",
            is_published=True,
        )


def test_bot_strategy_override_can_select_release_different_from_root() -> None:
    snapshot, now, planner_settings, account, state, costs = _inputs()
    root = WorkspaceSettings.model_validate(valid_payload())
    version_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    settings_hash = "f" * 64
    planner_settings = replace(
        planner_settings,
        settings_version_id=str(version_id),
        settings_hash=settings_hash,
        active_release_ids=("h01-v1",),
        release_content_hashes=(
            ("h01-v1", StrategyRegistry().get_release("h01-v1").content_hash),
            ("h03-v1", StrategyRegistry().get_release("h03-v1").content_hash),
        ),
    )
    account = replace(account, settings_version_id=str(version_id))
    state = replace(state, settings_version_id=str(version_id))
    published = PublishedWorkspaceSettings(
        workspace_id=WORKSPACE_ID,
        settings_version_id=version_id,
        settings_hash=settings_hash,
        settings=root,
    )
    selection = StrategySelection(
        settings_version_id=str(version_id),
        active_release_ids=("h03-v1",),
        published=True,
    )
    cycle = PaperDecisionCycle(
        StrategyRegistry(),
        TradePlanner(),
        RiskEngine(),
        PaperExecutionGateway(
            PaperExecutionPolicy(
                version_id="paper-v1",
                latency=timedelta(0),
                spread_rate=Decimal("0"),
                slippage_rate=Decimal("0"),
                fee_rate=Decimal("0"),
            )
        ),
    )

    result = cycle.prepare_published(
        published_settings=published,
        snapshot=snapshot,
        evaluated_at=now,
        planner_settings=planner_settings,
        account=account,
        risk_state=state,
        open_exposures=(),
        cost_model=costs,
        selection_override=selection,
    )

    assert getattr(result.strategy_result, "strategy_id", None) == "H03"


def test_draft_root_settings_cannot_create_runtime_strategy_selection() -> None:
    settings = WorkspaceSettings.model_validate(valid_payload())

    with pytest.raises(ValueError, match="Bản nháp"):
        strategy_selection_from_published_settings(
            settings,
            settings_version_id="draft-v1",
            is_published=False,
        )


def _inputs():
    snapshot = _h01_snapshot()
    now = snapshot.decision_time
    planner_settings = replace(
        _planner_settings(),
        workspace_id=str(WORKSPACE_ID),
        settings_version_id="settings-v1",
        active_release_ids=("h01-v1",),
        release_content_hashes=(("h01-v1", StrategyRegistry().get_release("h01-v1").content_hash),),
    )
    account = AccountSnapshot(
        workspace_id=str(WORKSPACE_ID),
        account_id="paper-a",
        settings_version_id="settings-v1",
        captured_at=now - timedelta(seconds=1),
        equity=Decimal("10000"),
        available_margin=Decimal("1000"),
        is_known=True,
    )
    state = RiskState(
        workspace_id=str(WORKSPACE_ID),
        account_id="paper-a",
        settings_version_id="settings-v1",
        risk_profile_version_id="risk-v1",
        evaluated_at=now,
        account_captured_at=account.captured_at,
        market_data_as_of=now - timedelta(minutes=1),
        equity=account.equity,
        available_margin=account.available_margin,
        open_exposures=(),
        realized_loss_today=Decimal("0"),
        realized_loss_rolling=Decimal("0"),
        open_position_count=0,
        is_known=True,
        pending_reservation_initial_margin=Decimal("0"),
    )
    costs = CostModel(
        version_id="cost-v1",
        entry_fee_rate=Decimal("0.001"),
        exit_fee_rate=Decimal("0.001"),
        adverse_funding_per_unit=Decimal("0.05"),
        entry_slippage_rate=Decimal("0"),
    )
    return snapshot, now, planner_settings, account, state, costs


def test_cycle_uses_selected_strategy_plan_risk_and_creates_paper_intent() -> None:
    snapshot, now, planner_settings, account, state, costs = _inputs()
    evaluated_at = now + timedelta(seconds=1)
    version_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    settings_hash = "f" * 64
    planner_settings = replace(
        planner_settings,
        settings_version_id=str(version_id),
        settings_hash=settings_hash,
    )
    account = replace(account, settings_version_id=str(version_id))
    state = replace(state, settings_version_id=str(version_id))
    published_settings = PublishedWorkspaceSettings(
        workspace_id=WORKSPACE_ID,
        settings_version_id=version_id,
        settings_hash=settings_hash,
        settings=WorkspaceSettings.model_validate(valid_payload()),
    )
    gateway = PaperExecutionGateway(
        PaperExecutionPolicy(
            version_id="paper-fill-v1",
            latency=timedelta(0),
            spread_rate=Decimal("0"),
            slippage_rate=Decimal("0"),
            fee_rate=Decimal("0.001"),
        )
    )

    cycle = PaperDecisionCycle(StrategyRegistry(), TradePlanner(), RiskEngine(), gateway)
    prepared = cycle.prepare_published(
        published_settings=published_settings,
        snapshot=snapshot,
        evaluated_at=evaluated_at,
        planner_settings=planner_settings,
        account=account,
        risk_state=state,
        open_exposures=(),
        cost_model=costs,
    )
    assert prepared.outcome is DecisionCycleOutcome.RISK_APPROVED
    assert prepared.order is None

    result = cycle.submit_approved(
        prepared,
        evaluated_at=evaluated_at,
        idempotency_key="cycle:settings-v1:snapshot-test-v1:h01-v1",
    )

    assert result.outcome is DecisionCycleOutcome.PAPER_INTENT_CREATED
    assert result.strategy_result is not None
    assert result.strategy_result.strategy_release_id == "h01-v1"
    assert isinstance(result.plan, TradePlan)
    assert isinstance(result.risk_decision, RiskDecision)
    assert result.risk_decision.approved
    assert result.order is not None
    assert result.order.status is OrderStatus.ACCEPTED
    assert result.order.plan_hash == result.plan.plan_hash
    assert result.order.eligible_at >= evaluated_at
    assert isinstance(result.strategy_result, StrategyCandidate)
    assert result.strategy_result.candidate_id == result.plan.candidate_id
    assert replace(result.strategy_result).candidate_id == result.strategy_result.candidate_id
    assert (
        replace(
            result.strategy_result,
            reference_price=result.strategy_result.reference_price + Decimal("1"),
        ).candidate_id
        != result.strategy_result.candidate_id
    )
    assert (
        replace(result.strategy_result, direction="short").candidate_id
        != result.strategy_result.candidate_id
    )
    assert (
        replace(
            result.strategy_result,
            evidence_bar_keys=tuple(reversed(result.strategy_result.evidence_bar_keys)),
        ).candidate_id
        == result.strategy_result.candidate_id
    )


def test_published_cycle_rejects_snapshot_outside_configured_market_scope() -> None:
    snapshot, now, planner_settings, account, state, costs = _inputs()
    published_settings = PublishedWorkspaceSettings(
        workspace_id=WORKSPACE_ID,
        settings_version_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        settings_hash="f" * 64,
        settings=WorkspaceSettings.model_validate(valid_payload()),
    )
    planner_settings = replace(
        planner_settings,
        settings_version_id=str(published_settings.settings_version_id),
        settings_hash=published_settings.settings_hash,
    )
    account = replace(account, settings_version_id=planner_settings.settings_version_id)
    state = replace(state, settings_version_id=planner_settings.settings_version_id)
    invalid_snapshot = replace(
        snapshot,
        symbol="XRPUSDT",
        series=tuple(
            replace(
                series,
                bars=tuple(replace(bar, symbol="XRPUSDT") for bar in series.bars),
            )
            for series in snapshot.series
        ),
    )
    cycle = PaperDecisionCycle(
        StrategyRegistry(),
        TradePlanner(),
        RiskEngine(),
        PaperExecutionGateway(
            PaperExecutionPolicy("paper-v1", timedelta(0), Decimal(0), Decimal(0), Decimal(0))
        ),
    )

    result = cycle.prepare_published(
        published_settings=published_settings,
        snapshot=invalid_snapshot,
        evaluated_at=now + timedelta(seconds=1),
        planner_settings=planner_settings,
        account=account,
        risk_state=state,
        open_exposures=(),
        cost_model=costs,
    )

    assert result.outcome is DecisionCycleOutcome.NO_TRADE
    assert isinstance(result.strategy_result, NoTrade)
    assert result.strategy_result.code == "MARKET_SCOPE_MISMATCH"


def test_published_cycle_rejects_settings_hash_mismatch() -> None:
    snapshot, now, planner_settings, account, state, costs = _inputs()
    published_settings = PublishedWorkspaceSettings(
        workspace_id=WORKSPACE_ID,
        settings_version_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        settings_hash="f" * 64,
        settings=WorkspaceSettings.model_validate(valid_payload()),
    )
    planner_settings = replace(
        planner_settings,
        settings_version_id=str(published_settings.settings_version_id),
        settings_hash="a" * 64,
    )
    account = replace(account, settings_version_id=planner_settings.settings_version_id)
    state = replace(state, settings_version_id=planner_settings.settings_version_id)
    gateway = PaperExecutionGateway(
        PaperExecutionPolicy("paper-v1", timedelta(0), Decimal(0), Decimal(0), Decimal(0))
    )

    result = PaperDecisionCycle(
        StrategyRegistry(), TradePlanner(), RiskEngine(), gateway
    ).prepare_published(
        published_settings=published_settings,
        snapshot=snapshot,
        evaluated_at=now + timedelta(seconds=1),
        planner_settings=planner_settings,
        account=account,
        risk_state=state,
        open_exposures=(),
        cost_model=costs,
    )

    assert result.outcome is DecisionCycleOutcome.NO_TRADE
    assert isinstance(result.strategy_result, NoTrade)
    assert result.strategy_result.code == "PUBLISHED_SETTINGS_SCOPE_MISMATCH"


def test_published_cycle_rejects_settings_before_effective_time() -> None:
    snapshot, now, planner_settings, account, state, costs = _inputs()
    effective_at = now + timedelta(minutes=1)
    version_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    published_settings = PublishedWorkspaceSettings(
        workspace_id=WORKSPACE_ID,
        settings_version_id=version_id,
        settings_hash="f" * 64,
        settings=WorkspaceSettings.model_validate(valid_payload()),
        effective_at=effective_at,
    )
    planner_settings = replace(
        planner_settings,
        settings_version_id=str(version_id),
        settings_hash=published_settings.settings_hash,
    )
    account = replace(account, settings_version_id=planner_settings.settings_version_id)
    state = replace(state, settings_version_id=planner_settings.settings_version_id)
    cycle = PaperDecisionCycle(
        StrategyRegistry(),
        TradePlanner(),
        RiskEngine(),
        PaperExecutionGateway(
            PaperExecutionPolicy("paper-v1", timedelta(0), Decimal(0), Decimal(0), Decimal(0))
        ),
    )

    result = cycle.prepare_published(
        published_settings=published_settings,
        snapshot=snapshot,
        evaluated_at=now + timedelta(seconds=1),
        planner_settings=planner_settings,
        account=account,
        risk_state=state,
        open_exposures=(),
        cost_model=costs,
    )

    assert result.outcome is DecisionCycleOutcome.NO_TRADE
    assert isinstance(result.strategy_result, NoTrade)
    assert result.strategy_result.code == "SETTINGS_NOT_EFFECTIVE"


def test_loaded_published_settings_remain_pinned_when_a_new_release_exists() -> None:
    snapshot, _, planner_settings, account, state, costs = _inputs()
    first_payload = valid_payload()
    first_strategy = first_payload["strategy"]
    assert isinstance(first_strategy, dict)
    first_strategy["default_release"] = "h01-v1"
    first = PublishedWorkspaceSettings(
        workspace_id=WORKSPACE_ID,
        settings_version_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        settings_hash="f" * 64,
        settings=WorkspaceSettings.model_validate(first_payload),
        effective_at=snapshot.decision_time - timedelta(seconds=1),
    )
    second_payload = valid_payload()
    second_strategy = second_payload["strategy"]
    assert isinstance(second_strategy, dict)
    second_strategy["default_release"] = "h03-v1"
    second = PublishedWorkspaceSettings(
        workspace_id=WORKSPACE_ID,
        settings_version_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        settings_hash="e" * 64,
        settings=WorkspaceSettings.model_validate(second_payload),
        effective_at=snapshot.decision_time + timedelta(seconds=1),
    )

    assert first.settings.strategy.default_release == "h01-v1"
    assert second.settings.strategy.default_release == "h03-v1"
    assert first.settings_version_id != second.settings_version_id

    planner_settings = replace(
        planner_settings,
        settings_version_id=str(first.settings_version_id),
        settings_hash=first.settings_hash,
        active_release_ids=(first.settings.strategy.default_release,),
    )
    account = replace(account, settings_version_id=planner_settings.settings_version_id)
    state = replace(state, settings_version_id=planner_settings.settings_version_id)
    assert second.effective_at is not None
    later_evaluation = second.effective_at + timedelta(seconds=1)
    prepared = PaperDecisionCycle(
        StrategyRegistry(),
        TradePlanner(),
        RiskEngine(),
        PaperExecutionGateway(
            PaperExecutionPolicy("paper-v1", timedelta(0), Decimal(0), Decimal(0), Decimal(0))
        ),
    ).prepare_published(
        published_settings=first,
        snapshot=snapshot,
        evaluated_at=later_evaluation,
        planner_settings=planner_settings,
        account=account,
        risk_state=state,
        open_exposures=(),
        cost_model=costs,
    )

    assert isinstance(prepared.plan, TradePlan)
    assert prepared.plan.settings_version_id == str(first.settings_version_id)
    assert prepared.plan.settings_hash == first.settings_hash
    assert prepared.plan.strategy_release_id == "h01-v1"


def test_cycle_never_submits_when_strategy_returns_no_trade() -> None:
    snapshot, evaluated_at, planner_settings, account, state, costs = _inputs()
    evaluated_at += timedelta(seconds=1)
    stale = replace(snapshot, position_is_flat=False)
    gateway = PaperExecutionGateway(
        PaperExecutionPolicy("paper-fill-v1", timedelta(0), Decimal(0), Decimal(0), Decimal(0))
    )

    result = PaperDecisionCycle(StrategyRegistry(), TradePlanner(), RiskEngine(), gateway).evaluate(
        snapshot=stale,
        evaluated_at=evaluated_at,
        release_id="h01-v1",
        selection=StrategySelection("settings-v1", ("h01-v1",), published=True),
        planner_settings=planner_settings,
        account=account,
        risk_state=state,
        open_exposures=(),
        cost_model=costs,
        idempotency_key="cycle:no-trade",
    )

    assert result.outcome is DecisionCycleOutcome.NO_TRADE
    assert isinstance(result.strategy_result, NoTrade)
    assert result.plan is None
    assert result.order is None


def test_cycle_never_submits_when_risk_rejects_plan() -> None:
    snapshot, now, planner_settings, account, state, costs = _inputs()
    evaluated_at = now + timedelta(seconds=1)
    state = replace(state, evaluated_at=evaluated_at)
    blocked_state = replace(state, is_known=False)
    gateway = PaperExecutionGateway(
        PaperExecutionPolicy("paper-fill-v1", timedelta(0), Decimal(0), Decimal(0), Decimal(0))
    )

    result = PaperDecisionCycle(StrategyRegistry(), TradePlanner(), RiskEngine(), gateway).evaluate(
        snapshot=snapshot,
        evaluated_at=evaluated_at,
        release_id="h01-v1",
        selection=StrategySelection("settings-v1", ("h01-v1",), published=True),
        planner_settings=planner_settings,
        account=account,
        risk_state=blocked_state,
        open_exposures=(),
        cost_model=costs,
        idempotency_key="cycle:risk-rejected",
    )

    assert result.outcome is DecisionCycleOutcome.RISK_REJECTED
    assert result.risk_decision is not None
    assert result.risk_decision.reason_code == "RISK_STATE_UNKNOWN"
    assert result.order is None


def test_cycle_does_not_allow_plan_reject_to_fall_through_to_execution() -> None:
    snapshot, evaluated_at, planner_settings, account, state, costs = _inputs()
    evaluated_at += timedelta(seconds=1)
    bad_account = replace(account, settings_version_id="settings-old")
    gateway = PaperExecutionGateway(
        PaperExecutionPolicy("paper-fill-v1", timedelta(0), Decimal(0), Decimal(0), Decimal(0))
    )

    result = PaperDecisionCycle(StrategyRegistry(), TradePlanner(), RiskEngine(), gateway).evaluate(
        snapshot=snapshot,
        evaluated_at=evaluated_at,
        release_id="h01-v1",
        selection=StrategySelection("settings-v1", ("h01-v1",), published=True),
        planner_settings=planner_settings,
        account=bad_account,
        risk_state=state,
        open_exposures=(),
        cost_model=costs,
        idempotency_key="cycle:plan-rejected",
    )

    assert result.outcome is DecisionCycleOutcome.PLAN_REJECTED
    assert isinstance(result.plan, PlanRejected)
    assert result.order is None
