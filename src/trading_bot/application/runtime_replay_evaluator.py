"""Bộ chuyển context replay sang các thành phần quyết định và PAPER runtime dùng chung."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from trading_bot.application.backtest import (
    BacktestRunner,
    EvaluationReport,
    ReplayEvaluator,
    ReplayManifest,
    ReplayObservation,
    ReplayRejected,
)
from trading_bot.application.backtest_sensitivity import (
    SensitivityRun,
    SensitivityScenario,
    run_sensitivity,
)
from trading_bot.application.paper_decision_cycle import (
    DecisionCycleOutcome,
    PaperDecisionCycle,
)
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings
from trading_bot.domain.execution.paper import (
    OrderStatus,
    PaperBar,
    PaperExecutionGateway,
)
from trading_bot.domain.learning.evaluation import EvaluationTrade
from trading_bot.domain.risk.engine import RiskDecision, RiskState
from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    CostModel,
    OpenExposure,
    PlannerSettings,
    TradePlan,
)
from trading_bot.domain.strategy.contracts import DecisionSnapshot
from trading_bot.domain.strategy.registry import StrategyRegistry

ZERO = Decimal("0")


class ReplayEvaluationContextError(ValueError):
    """Từ chối replay khi thiếu state hoặc lineage đã ghim."""


@dataclass(frozen=True, slots=True)
class ReplayEvaluationContext:
    """Trạng thái point-in-time và sự kiện PAPER mới mà composition root cung cấp."""

    published_settings: PublishedWorkspaceSettings
    evaluated_at: datetime
    planner_settings: PlannerSettings
    account: AccountSnapshot
    risk_state: RiskState
    open_exposures: tuple[OpenExposure, ...]
    cost_model: CostModel
    feature_schema_hash: str
    pipeline_content_hash: str
    execution_events: tuple[PaperBar, ...]
    reserve_risk: Callable[[RiskDecision, str], bool]
    opposing_signal: bool = False
    atr_by_symbol: tuple[tuple[str, Decimal], ...] = ()


ContextProvider = Callable[[DecisionSnapshot], ReplayEvaluationContext]


@dataclass(frozen=True, slots=True)
class _ReplayReservation:
    """Ngân sách risk của intent PAPER, được reducer đối soát từ gateway."""

    plan: TradePlan
    decision: RiskDecision


class ReplayAccountState:
    """Reducer vốn, exposure và reservation từ các sự kiện gateway PAPER."""

    def __init__(self, context: ReplayEvaluationContext) -> None:
        self._initial_equity = context.account.equity
        self._initial_margin = context.account.available_margin
        self._initial_exposures = context.open_exposures
        self._initial_position_count = context.risk_state.open_position_count
        self._initial_pending_risk = context.risk_state.pending_reservation_risk
        self._initial_pending_notional = context.risk_state.pending_reservation_notional
        self._initial_pending_symbol_notional = (
            context.risk_state.pending_symbol_reservation_notional
        )
        self._initial_pending_margin = context.risk_state.pending_reservation_initial_margin
        self._initial_pending_count = context.risk_state.pending_reservation_count
        self._initial_realized_loss_today = context.risk_state.realized_loss_today
        self._initial_realized_loss_rolling = context.risk_state.realized_loss_rolling
        self._closed_pnl: dict[str, Decimal] = {}

    def project(
        self,
        context: ReplayEvaluationContext,
        gateway: PaperExecutionGateway,
        reservations: dict[str, _ReplayReservation],
        *,
        candidate_symbol: str,
    ) -> tuple[AccountSnapshot, RiskState, tuple[OpenExposure, ...]]:
        """Tạo snapshot point-in-time, bỏ qua số dư/exposure provider trả lại."""
        workspace_id = context.account.workspace_id
        account_id = context.account.account_id
        open_exposures = list(self._initial_exposures)
        open_count = self._initial_position_count
        pending_risk = self._initial_pending_risk
        pending_notional = self._initial_pending_notional
        pending_symbol_notional = self._initial_pending_symbol_notional
        pending_margin = self._initial_pending_margin
        pending_count = self._initial_pending_count
        allocated_margin = ZERO

        for intent_id, reservation in reservations.items():
            plan = reservation.plan
            order = gateway.order(intent_id)
            position = gateway.position(intent_id)
            requested = plan.quantity
            filled_fraction = order.filled_quantity / requested
            remaining_fraction = order.remaining_quantity / requested
            if position is not None and not position.is_closed and position.quantity > ZERO:
                filled_fraction = min(Decimal("1"), position.quantity / requested)
                open_exposures.append(
                    OpenExposure(
                        workspace_id=workspace_id,
                        account_id=account_id,
                        symbol=plan.symbol,
                        notional=plan.notional * filled_fraction,
                        risk_amount=plan.estimated_loss * filled_fraction,
                    )
                )
                open_count += 1
                allocated_margin += plan.initial_margin * filled_fraction
            elif position is not None and position.is_closed:
                self._closed_pnl[position.position_id] = position.net_realized_pnl

            if (
                order.status
                in {
                    OrderStatus.ACCEPTED,
                    OrderStatus.PARTIALLY_FILLED,
                    OrderStatus.UNKNOWN,
                }
                and order.remaining_quantity > ZERO
            ):
                if position is None or position.is_closed:
                    pending_count += 1
                pending_risk += reservation.decision.requested_risk * remaining_fraction
                pending_notional += plan.notional * remaining_fraction
                if plan.symbol == candidate_symbol:
                    pending_symbol_notional += plan.notional * remaining_fraction
                pending_margin += plan.initial_margin * remaining_fraction

        realized_pnl = sum(self._closed_pnl.values(), ZERO)
        realized_loss = sum(
            (-amount for amount in self._closed_pnl.values() if amount < ZERO), ZERO
        )
        account = replace(
            context.account,
            equity=self._initial_equity + realized_pnl,
            available_margin=self._initial_margin - allocated_margin,
        )
        risk_state = replace(
            context.risk_state,
            equity=account.equity,
            available_margin=account.available_margin,
            open_exposures=tuple(open_exposures),
            realized_loss_today=self._initial_realized_loss_today + realized_loss,
            realized_loss_rolling=self._initial_realized_loss_rolling + realized_loss,
            open_position_count=open_count,
            pending_reservation_initial_margin=pending_margin,
            pending_reservation_risk=pending_risk,
            pending_reservation_notional=pending_notional,
            pending_symbol_reservation_notional=pending_symbol_notional,
            pending_reservation_count=pending_count,
        )
        return account, risk_state, tuple(open_exposures)


@dataclass(frozen=True, slots=True)
class RuntimePaperReplayRun:
    """Kết quả BASE và các trade đóng theo hợp đồng repository hiện có."""

    sensitivity_run: SensitivityRun
    trade_outcomes: tuple[EvaluationTrade, ...]
    partial_trade_count: int = 0

    def __post_init__(self) -> None:
        result = self.sensitivity_run.result
        if isinstance(result, EvaluationReport):
            if len(self.trade_outcomes) != result.trade_count or self.partial_trade_count:
                raise ValueError("Completed replay phải có đúng số outcome như report.")
        elif self.trade_outcomes:
            raise ValueError("Replay chưa hoàn tất không được trả outcome để lưu cùng report.")
        elif self.partial_trade_count < 0:
            raise ValueError("partial_trade_count không được âm.")


class RuntimeReplayEvaluator:
    """Evaluator đồng bộ strategy/planner/risk/fill/monitor với PAPER runtime.

    Context/state progression và việc ghi reservation vẫn thuộc composition root;
    adapter này tự fail-closed nếu context không ghim đúng run manifest.
    """

    emits_trade_on_close: ClassVar[bool] = True

    def __init__(
        self,
        *,
        cycle: PaperDecisionCycle,
        strategies: StrategyRegistry,
        gateway: PaperExecutionGateway,
        manifest: ReplayManifest,
        context_provider: ContextProvider,
        scenario: SensitivityScenario | None = None,
    ) -> None:
        if cycle.paper_gateway is not gateway:
            raise ReplayEvaluationContextError(
                "PaperDecisionCycle và RuntimeReplayEvaluator phải dùng cùng gateway riêng."
            )
        self._cycle = cycle
        self._strategies = strategies
        self._gateway = gateway
        self._manifest = manifest
        self.scenario = scenario or SensitivityScenario("base", "BASE")
        self.strategy_parameters = _runtime_parameters(self.scenario)
        self.context_provider = context_provider
        self._intent_plans: dict[str, TradePlan] = {}
        self._reservations: dict[str, _ReplayReservation] = {}
        self._account_state: ReplayAccountState | None = None
        self._seen_events: set[tuple[str, str]] = set()
        self._emitted_trades: set[str] = set()

    @property
    def manifest_hash(self) -> str:
        """Hash manifest mà evaluator đã ghim khi tạo."""
        return self._manifest.content_hash

    @property
    def gateway(self) -> PaperExecutionGateway:
        """Gateway PAPER riêng của evaluator để điều phối kiểm tra cách ly scenario."""
        return self._gateway

    def __call__(self, snapshot: DecisionSnapshot) -> EvaluationTrade | None:
        """Đánh giá một observation rồi áp dụng event đã available theo thứ tự ổn định."""
        context = self.context_provider(snapshot)
        self._validate_context(context, snapshot)
        if self._account_state is None:
            self._account_state = ReplayAccountState(context)
        closed_trade = self._advance_execution(context, snapshot)
        assert self._account_state is not None
        account, risk_state, open_exposures = self._account_state.project(
            context,
            self._gateway,
            self._reservations,
            candidate_symbol=snapshot.symbol,
        )
        result = self._cycle.prepare_published(
            published_settings=context.published_settings,
            snapshot=snapshot,
            evaluated_at=context.evaluated_at,
            planner_settings=context.planner_settings,
            account=account,
            risk_state=risk_state,
            open_exposures=open_exposures,
            cost_model=context.cost_model,
            strategy_parameters=self.strategy_parameters,
        )

        if result.outcome is DecisionCycleOutcome.RISK_APPROVED:
            assert result.risk_decision is not None
            idempotency_key = f"replay:{snapshot.snapshot_id}:{self._manifest.strategy_release_id}"
            if not context.reserve_risk(result.risk_decision, idempotency_key):
                raise ReplayEvaluationContextError(
                    "Không ghi nhận được reservation trước khi tạo PAPER intent."
                )
            submitted = self._cycle.submit_approved(
                result,
                evaluated_at=context.evaluated_at,
                idempotency_key=idempotency_key,
            )
            if submitted.order is None or not isinstance(submitted.plan, TradePlan):
                raise ReplayEvaluationContextError("PAPER submit thiếu order hoặc TradePlan.")
            self._intent_plans[submitted.order.intent_id] = submitted.plan
            self._reservations[submitted.order.intent_id] = _ReplayReservation(
                submitted.plan, result.risk_decision
            )

        return closed_trade

    def _validate_context(
        self, context: ReplayEvaluationContext, snapshot: DecisionSnapshot
    ) -> None:
        """So khớp settings, release, risk state, costs và dữ liệu theo manifest."""
        manifest = self._manifest
        published = context.published_settings
        planner = context.planner_settings
        account = context.account
        state = context.risk_state
        release_id = manifest.strategy_release_id
        try:
            release = self._strategies.get_release(release_id)
        except LookupError as error:
            raise ReplayEvaluationContextError(
                "Strategy release không có trong registry."
            ) from error

        if (
            str(published.settings_version_id) != manifest.settings_version_id
            or planner.settings_version_id != manifest.settings_version_id
            or account.settings_version_id != manifest.settings_version_id
            or state.settings_version_id != manifest.settings_version_id
            or published.settings_hash != planner.settings_hash
            or str(published.workspace_id) != planner.workspace_id
            or account.workspace_id != planner.workspace_id
            or state.workspace_id != planner.workspace_id
            or account.account_id != state.account_id
            or state.risk_profile_version_id != manifest.risk_profile_version_id
            or planner.risk_profile_version_id != manifest.risk_profile_version_id
        ):
            raise ReplayEvaluationContextError(
                "Workspace/account/settings/risk lineage không khớp."
            )
        if not planner.published or published.settings.runtime.mode != "PAPER":
            raise ReplayEvaluationContextError("Replay chỉ nhận settings đã publish ở PAPER mode.")
        effective_strategy_hash = release.content_hash
        if self.scenario.kind == "PARAMETER":
            effective_strategy_hash = _hash_payload(
                {
                    "base_strategy_content_hash": release.content_hash,
                    "parameter_overrides": [
                        [key, _decimal_parameter(value)]
                        for key, value in sorted(self.scenario.parameter_overrides)
                    ],
                }
            )
        if (
            published.settings.strategy.default_release != release_id
            or planner.active_release_ids != (release_id,)
            or effective_strategy_hash != manifest.strategy_content_hash
            or dict(planner.release_content_hashes).get(release_id) != release.content_hash
        ):
            raise ReplayEvaluationContextError("Strategy release/hash không khớp manifest.")
        if (
            context.feature_schema_hash != manifest.feature_schema_hash
            or context.pipeline_content_hash != manifest.pipeline_content_hash
        ):
            raise ReplayEvaluationContextError("Feature/pipeline hash không khớp manifest.")
        if not account.is_known or not state.is_known:
            raise ReplayEvaluationContextError("Account hoặc RiskState chưa được xác nhận.")
        if (
            context.evaluated_at < snapshot.decision_time
            or state.evaluated_at != context.evaluated_at
            or account.captured_at > snapshot.decision_time
            or state.account_captured_at != account.captured_at
            or state.market_data_as_of > snapshot.decision_time
        ):
            raise ReplayEvaluationContextError("Runtime state không cùng mốc point-in-time.")
        if self._gateway.policy.version_id != manifest.fill_model_version:
            raise ReplayEvaluationContextError("Fill policy version không khớp manifest.")
        if context.cost_model.version_id == "":
            raise ReplayEvaluationContextError("Cost model thiếu version.")
        cost_values = {
            "entry_fee_rate": str(context.cost_model.entry_fee_rate),
            "exit_fee_rate": str(context.cost_model.exit_fee_rate),
            "adverse_funding_per_unit": str(context.cost_model.adverse_funding_per_unit),
            "entry_slippage_rate": str(context.cost_model.entry_slippage_rate),
            "exit_slippage_rate": str(context.cost_model.exit_slippage_rate),
        }
        manifest_costs = dict(manifest.cost_assumptions)
        if (
            set(manifest_costs) != set(cost_values) | {"spread_rate"}
            or {key: manifest_costs.get(key) for key in cost_values} != cost_values
            or manifest_costs.get("spread_rate") != str(self._gateway.policy.spread_rate)
        ):
            raise ReplayEvaluationContextError("Cost assumptions không khớp cost model runtime.")
        if context.cost_model.entry_fee_rate != context.cost_model.exit_fee_rate:
            raise ReplayEvaluationContextError(
                "Replay PAPER hiện chỉ hỗ trợ fee entry/exit bằng nhau."
            )
        if self._gateway.policy.fee_rate != context.cost_model.entry_fee_rate:
            raise ReplayEvaluationContextError(
                "Fee của PAPER fill policy không khớp Planner CostModel."
            )
        if (
            self._gateway.policy.spread_rate != Decimal("0")
            or context.cost_model.entry_slippage_rate != context.cost_model.exit_slippage_rate
            or self._gateway.policy.slippage_rate != context.cost_model.entry_slippage_rate
        ):
            raise ReplayEvaluationContextError(
                "Replay PAPER yêu cầu spread bằng 0 và slippage Planner/fill đối xứng, khớp nhau."
            )
        if any(event.available_at > snapshot.decision_time for event in context.execution_events):
            raise ReplayEvaluationContextError("Execution event chưa available tại decision time.")
        if any(event.symbol != snapshot.symbol for event in context.execution_events):
            raise ReplayEvaluationContextError("Execution event khác symbol của snapshot.")
        if len({(event.symbol, event.bar_key) for event in context.execution_events}) != len(
            context.execution_events
        ):
            raise ReplayEvaluationContextError("Execution event bị trùng trong một context.")

    def _advance_execution(
        self, context: ReplayEvaluationContext, snapshot: DecisionSnapshot
    ) -> EvaluationTrade | None:
        """Chạy fill/monitor PAPER trên event mới; chỉ trả trade khi vị thế đã đóng."""
        closed_trades: list[EvaluationTrade] = []
        atr_by_symbol = dict(context.atr_by_symbol)
        events = sorted(
            context.execution_events,
            key=lambda item: (item.available_at, item.opened_at, item.bar_key),
        )
        for event in events:
            event_key = (event.symbol, event.bar_key)
            if event_key in self._seen_events:
                continue
            self._seen_events.add(event_key)
            self._gateway.apply_market_event(event)
            for intent_id in self._intent_plans:
                position = self._gateway.position(intent_id)
                if position is None or position.is_closed or position.quantity <= 0:
                    continue
                if position.symbol != event.symbol:
                    continue
                result = self._gateway.monitor_position(
                    position,
                    event,
                    opposing_signal=context.opposing_signal,
                    atr=atr_by_symbol.get(event.symbol),
                )
                if (
                    not result.position.is_closed
                    or result.position.position_id in self._emitted_trades
                ):
                    continue
                intent = self._gateway.order(intent_id)
                first_fill = next((fill for fill in intent.fills if fill.quantity > 0), None)
                if first_fill is None:
                    raise ReplayEvaluationContextError("Vị thế đóng nhưng thiếu entry fill.")
                plan = self._intent_plans[intent_id]
                position = result.position
                closed_trades.append(
                    EvaluationTrade(
                        trade_id=position.position_id,
                        opened_at=first_fill.filled_at,
                        closed_at=event.closed_at,
                        gross_pnl=position.gross_realized_pnl,
                        fees=position.fee_cost,
                        funding=position.funding_cost,
                        slippage=position.slippage_cost,
                        exposure=plan.notional,
                    )
                )
                self._emitted_trades.add(position.position_id)
        if len(closed_trades) > 1:
            raise ReplayEvaluationContextError(
                "Một observation đóng nhiều vị thế; EvaluationTrade hiện chỉ biểu diễn một trade."
            )
        return closed_trades[0] if closed_trades else None


RuntimeEvaluatorFactory = Callable[[SensitivityScenario, ReplayManifest], RuntimeReplayEvaluator]


@dataclass(slots=True)
class _CapturingRuntimeEvaluator:
    """Giữ outcome trong lúc giữ nguyên marker close-emitting cho runner."""

    evaluator: RuntimeReplayEvaluator
    captured: list[EvaluationTrade]
    emits_trade_on_close: ClassVar[bool] = True

    def __call__(self, snapshot: DecisionSnapshot) -> EvaluationTrade | None:
        trade = self.evaluator(snapshot)
        if trade is not None:
            self.captured.append(trade)
        return trade


@dataclass(frozen=True, slots=True)
class RuntimePaperReplayComparison:
    """Kết quả BASE và các scenario, mỗi scenario có outcome riêng để persist."""

    runs: tuple[RuntimePaperReplayRun, ...]

    def __post_init__(self) -> None:
        base_count = sum(run.sensitivity_run.scenario.kind == "BASE" for run in self.runs)
        if not self.runs or base_count != 1:
            raise ValueError("Replay comparison cần đúng một run BASE.")

    @property
    def is_complete(self) -> bool:
        return all(isinstance(run.sensitivity_run.result, EvaluationReport) for run in self.runs)


def run_runtime_paper_replay(
    observations: tuple[ReplayObservation, ...],
    *,
    manifest: ReplayManifest,
    runtime_evaluator_factory: RuntimeEvaluatorFactory,
    scenarios: tuple[SensitivityScenario, ...] | None = None,
    runner: BacktestRunner | None = None,
) -> RuntimePaperReplayComparison:
    """Chạy scenario bằng evaluator PAPER cụ thể và giữ outcome theo từng trial."""
    active_scenarios = (
        scenarios if scenarios is not None else (SensitivityScenario("base", "BASE"),)
    )
    captured_by_manifest: dict[str, list[EvaluationTrade]] = {}
    evaluators: list[RuntimeReplayEvaluator] = []

    def evaluator_factory(
        scenario: SensitivityScenario, scenario_manifest: ReplayManifest
    ) -> ReplayEvaluator:
        _validate_runtime_scenario(scenario, scenario_manifest)
        evaluator = runtime_evaluator_factory(scenario, scenario_manifest)
        if not isinstance(evaluator, RuntimeReplayEvaluator):
            raise ReplayEvaluationContextError(
                "Scenario factory phải trả RuntimeReplayEvaluator cụ thể."
            )
        if evaluator.manifest_hash != scenario_manifest.content_hash:
            raise ReplayEvaluationContextError(
                "Runtime evaluator không ghim manifest do sensitivity runner cấp."
            )
        if evaluator.scenario != scenario or evaluator.strategy_parameters != _runtime_parameters(
            scenario
        ):
            raise ReplayEvaluationContextError(
                "Evaluator runtime không áp dụng đúng cấu hình scenario đã khai báo."
            )
        if any(
            evaluator.gateway is prior.gateway
            or evaluator.context_provider is prior.context_provider
            for prior in evaluators
        ):
            raise ReplayEvaluationContextError(
                "Mỗi trial phải tạo gateway và context provider mới."
            )
        evaluators.append(evaluator)
        captured: list[EvaluationTrade] = []
        captured_by_manifest[scenario_manifest.content_hash] = captured

        return _CapturingRuntimeEvaluator(evaluator, captured)

    comparison = run_sensitivity(
        observations,
        manifest=manifest,
        scenarios=active_scenarios,
        evaluator_factory=evaluator_factory,
        runner=runner,
    )
    results: list[RuntimePaperReplayRun] = []
    for sensitivity_run in comparison.runs:
        captured = captured_by_manifest[sensitivity_run.manifest.content_hash]
        if isinstance(sensitivity_run.result, ReplayRejected):
            results.append(
                RuntimePaperReplayRun(
                    sensitivity_run=sensitivity_run,
                    trade_outcomes=(),
                    partial_trade_count=len(captured),
                )
            )
            continue
        if len(captured) != sensitivity_run.result.trade_count:
            raise ReplayEvaluationContextError(
                "Trade outcomes thu được không khớp số trade trong evaluation report."
            )
        results.append(
            RuntimePaperReplayRun(
                sensitivity_run=sensitivity_run,
                trade_outcomes=tuple(captured),
            )
        )
    return RuntimePaperReplayComparison(tuple(results))


def _runtime_parameters(scenario: SensitivityScenario) -> tuple[tuple[str, int], ...]:
    if scenario.kind != "PARAMETER":
        if scenario.parameter_overrides:
            raise ReplayEvaluationContextError("Scenario không nhận strategy parameters.")
        return ()
    parsed: list[tuple[str, int]] = []
    for key, value in scenario.parameter_overrides:
        try:
            number = Decimal(value)
        except Exception as error:
            raise ReplayEvaluationContextError(
                "Strategy parameter phải là Decimal số nguyên."
            ) from error
        if not number.is_finite() or number != number.to_integral_value():
            raise ReplayEvaluationContextError("Strategy parameter phải là Decimal số nguyên.")
        parsed.append((key, int(number)))
    try:
        from trading_bot.domain.strategy.contracts import StrategySelection

        StrategySelection("runtime-parameter-check", ("h03-v1",), True, tuple(parsed))
    except ValueError as error:
        raise ReplayEvaluationContextError(str(error)) from error
    return tuple(sorted(parsed))


def _validate_runtime_scenario(scenario: SensitivityScenario, manifest: ReplayManifest) -> None:
    """Giới hạn replay vào các override đã có đường đi thực trong PAPER runtime."""
    if scenario.kind == "COST":
        overrides = dict(scenario.cost_overrides)
        assumptions = dict(manifest.cost_assumptions)
        fee_keys = {"entry_fee_rate", "exit_fee_rate"}
        slippage_keys = {"entry_slippage_rate", "exit_slippage_rate"}
        if set(overrides) == fee_keys:
            if not fee_keys.issubset(assumptions):
                raise ReplayEvaluationContextError(
                    "Manifest thiếu entry/exit fee để chạy PAPER fee stress."
                )
            if Decimal(overrides["entry_fee_rate"]) != Decimal(overrides["exit_fee_rate"]):
                raise ReplayEvaluationContextError(
                    "PAPER fill policy chỉ có một fee_rate; stress entry/exit phải bằng nhau."
                )
            if Decimal(overrides["entry_fee_rate"]) != Decimal(assumptions["entry_fee_rate"]):
                raise ReplayEvaluationContextError(
                    "Cost override chưa được ghi vào replay manifest."
                )
        elif set(overrides) == slippage_keys:
            required = slippage_keys | {"spread_rate", "adverse_funding_per_unit"}
            if not required.issubset(assumptions):
                raise ReplayEvaluationContextError(
                    "Manifest thiếu lineage slippage, spread hoặc funding cho PAPER stress."
                )
            if Decimal(overrides["entry_slippage_rate"]) != Decimal(
                overrides["exit_slippage_rate"]
            ):
                raise ReplayEvaluationContextError(
                    "PAPER slippage stress yêu cầu entry/exit rate đối xứng."
                )
            if Decimal(overrides["entry_slippage_rate"]) >= Decimal("1"):
                raise ReplayEvaluationContextError(
                    "PAPER slippage rate phải nhỏ hơn 1 và được từ chối trước khi tạo evaluator."
                )
            if (
                Decimal(assumptions["entry_slippage_rate"])
                != Decimal(assumptions["exit_slippage_rate"])
                or Decimal(assumptions["spread_rate"]) != Decimal("0")
                or Decimal(assumptions["adverse_funding_per_unit"]) != Decimal("0")
            ):
                raise ReplayEvaluationContextError(
                    "Slippage stress chỉ hỗ trợ spread và funding baseline bằng 0."
                )
        else:
            raise ReplayEvaluationContextError(
                "PAPER replay chỉ hỗ trợ cặp fee hoặc cặp entry/exit slippage."
            )
    if scenario.kind == "PARAMETER" and manifest.strategy_release_id != "h03-v1":
        raise ReplayEvaluationContextError("Runtime parameter replay hiện chỉ hỗ trợ H03-v1.")


def _decimal_parameter(value: str) -> str:
    return format(Decimal(value).normalize(), "f")


def _hash_payload(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
