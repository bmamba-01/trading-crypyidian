"""Cấp context thực thi tuần tự cho PAPER replay từ bundle đã kiểm quyền."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal

from trading_bot.application.backtest import (
    BacktestRunner,
    ReplayManifest,
    ReplayObservation,
    validate_replay_observation,
)
from trading_bot.application.backtest_dataset_replay_bundle import BacktestDatasetReplayBundle
from trading_bot.application.backtest_dataset_replay_observations import (
    build_backtest_dataset_replay_observations,
)
from trading_bot.application.backtest_sensitivity import SensitivityScenario
from trading_bot.application.paper_decision_cycle import PaperDecisionCycle
from trading_bot.application.runtime_replay_evaluator import (
    ReplayEvaluationContext,
    ReplayExitEvidence,
    RuntimePaperReplayComparison,
    RuntimeReplayEvaluator,
    run_runtime_paper_replay,
)
from trading_bot.domain.execution.paper import PaperBar, PaperExecutionGateway
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.risk.engine import RiskDecision, RiskEngine
from trading_bot.domain.risk.planner import TradePlanner
from trading_bot.domain.strategy.contracts import DecisionSnapshot, FeatureQuality
from trading_bot.domain.strategy.registry import StrategyRegistry
from trading_bot.domain.strategy.rules_v2 import EXIT_EVIDENCE_BY_RECIPE, ExitEvidenceSpec

OpposingSignalProvider = Callable[[PaperBar, ReplayObservation | None], bool | None]
ContextSeedFactory = Callable[[SensitivityScenario, ReplayManifest], ReplayEvaluationContext]
GatewayFactory = Callable[[SensitivityScenario, ReplayManifest], PaperExecutionGateway]
OpposingSignalProviderFactory = Callable[
    [SensitivityScenario, ReplayManifest], OpposingSignalProvider | None
]


class BacktestReplayContextBuildError(ValueError):
    """Từ chối dựng context nếu state đầu vào hoặc timeline replay không đầy đủ."""


class BacktestDatasetReplayContextProvider:
    """Chuyển bundle thành execution event chỉ xuất hiện khi đã available.

    Account/risk seed và trạng thái ban đầu phải do caller xác nhận. MVP chỉ nhận
    seed phẳng vì hiện chưa có bộ khôi phục PaperPosition ban đầu từ ledger.
    """

    def __init__(
        self,
        bundle: BacktestDatasetReplayBundle,
        observations: tuple[ReplayObservation, ...],
        *,
        seed: ReplayEvaluationContext,
        execution_interval: str,
        opposing_signal_provider: OpposingSignalProvider | None = None,
        evaluated_at_offset: timedelta = timedelta(microseconds=1),
        capture_opportunities: bool = False,
    ) -> None:
        if not isinstance(bundle, BacktestDatasetReplayBundle):
            raise BacktestReplayContextBuildError("Cần bundle replay đã kiểm rights/hash.")
        if type(seed) is not ReplayEvaluationContext:
            raise BacktestReplayContextBuildError("Cần ReplayEvaluationContext seed tường minh.")
        if (
            seed.execution_events
            or seed.atr_by_bar
            or seed.opposing_signal_by_bar
            or seed.exit_evidence_by_bar
        ):
            raise BacktestReplayContextBuildError("Seed không được chứa execution event phát sẵn.")
        if not execution_interval.strip():
            raise BacktestReplayContextBuildError("execution_interval không được để trống.")
        if evaluated_at_offset < timedelta(0):
            raise BacktestReplayContextBuildError("evaluated_at_offset không được âm.")
        if not seed.account.is_known or not seed.risk_state.is_known:
            raise BacktestReplayContextBuildError("Account/RiskState ban đầu phải đã xác nhận.")
        if (
            seed.published_settings.workspace_id != bundle.workspace_id
            or seed.planner_settings.workspace_id != str(bundle.workspace_id)
            or seed.account.workspace_id != str(bundle.workspace_id)
            or seed.risk_state.workspace_id != str(bundle.workspace_id)
        ):
            raise BacktestReplayContextBuildError(
                "Workspace của settings/account/risk không khớp replay bundle."
            )
        if (
            seed.open_exposures
            or seed.risk_state.open_exposures
            or seed.risk_state.open_position_count
            or seed.risk_state.pending_reservation_count
            or seed.risk_state.pending_reservation_risk > 0
            or seed.risk_state.pending_reservation_notional > 0
            or seed.risk_state.pending_symbol_reservation_notional > 0
            or seed.risk_state.pending_reservation_initial_margin > 0
        ):
            raise BacktestReplayContextBuildError(
                "Replay hiện chỉ nhận seed phẳng, không có vị thế hoặc reservation đang mở."
            )

        component = next(
            (item for item in bundle.components if item.manifest.interval == execution_interval),
            None,
        )
        if component is None:
            raise BacktestReplayContextBuildError(
                "Bundle thiếu timeframe execution đã chọn; không tự resample nến."
            )
        ordered_observations = tuple(
            sorted(
                observations,
                key=lambda item: (item.available_at, item.event_at, item.event_id),
            )
        )
        if not ordered_observations:
            raise BacktestReplayContextBuildError("Replay cần ít nhất một observation.")

        source_bars = {
            (component.manifest.interval, bar.open_time): bar
            for component in bundle.components
            for bar in component.bars
        }
        if capture_opportunities and not all(
            item.manifest.has_pinned_source_identities for item in bundle.components
        ):
            raise BacktestReplayContextBuildError(
                "Opportunity capture đòi source manifest v4 đã ghim."
            )
        self._opportunity_sources = (
            {
                item.snapshot.snapshot_id: (item, ordinal)
                for ordinal, item in enumerate(ordered_observations)
            }
            if capture_opportunities
            else {}
        )
        if capture_opportunities and len(self._opportunity_sources) != len(ordered_observations):
            raise BacktestReplayContextBuildError("Snapshot nguồn opportunity bị trùng.")
        usable: list[ReplayObservation] = []
        for observation in ordered_observations:
            decision_time = observation.snapshot.decision_time
            if observation.snapshot.symbol != bundle.symbol:
                raise BacktestReplayContextBuildError("Observation khác symbol của bundle.")
            if not bundle.evaluation_start <= decision_time < bundle.range_end:
                raise BacktestReplayContextBuildError("Observation nằm ngoài evaluation window.")
            if any(
                source_bars.get((series.timeframe, bar.open_time)) != bar
                for series in observation.snapshot.series
                for bar in series.bars
            ):
                raise BacktestReplayContextBuildError(
                    "Observation chứa bar không thuộc bundle đã kiểm hash."
                )
            try:
                validate_replay_observation(observation)
            except ValueError as error:
                if str(error).startswith("INPUT_NOT_READY:"):
                    continue
                raise BacktestReplayContextBuildError(str(error)) from error
            usable.append(observation)
        if not usable and not capture_opportunities:
            raise BacktestReplayContextBuildError("Không có observation usable để replay.")
        if seed.account.captured_at > ordered_observations[0].snapshot.decision_time:
            raise BacktestReplayContextBuildError(
                "Account seed được chụp sau observation đầu tiên."
            )

        bars = tuple(
            sorted(
                (
                    _to_paper_bar(bundle.symbol, execution_interval, bar)
                    for bar in component.bars
                    if bar.is_closed and bar.close_time < bundle.range_end
                ),
                key=lambda item: (item.available_at, item.opened_at, item.bar_key),
            )
        )
        # Nến chiến lược đã đóng theo từng timeframe, dùng cho bằng chứng thoát của recipe v2.
        self._strategy_bars = {
            item.manifest.interval: tuple(
                sorted(
                    (bar for bar in item.bars if bar.is_closed),
                    key=lambda bar: (bar.close_time, bar.open_time),
                )
            )
            for item in bundle.components
        }
        self._strategy_close_times = {
            interval: frozenset(bar.close_time for bar in interval_bars)
            for interval, interval_bars in self._strategy_bars.items()
        }
        self._bundle = bundle
        self._observations = tuple(usable)
        self._execution_bars = bars
        self._seed = seed
        self._opposing_signal_provider = opposing_signal_provider
        self._evaluated_at_offset = evaluated_at_offset
        self._next_observation = 0
        self._last_decision_time = ordered_observations[0].snapshot.decision_time

    def opportunity_source_for(
        self, snapshot: DecisionSnapshot
    ) -> tuple[ReplayObservation, int] | None:
        """Tra đúng nội dung nguồn đã ghim trước readiness filter, không tra gần thời gian."""
        source = self._opportunity_sources.get(snapshot.snapshot_id)
        if source is not None and source[0].snapshot != snapshot:
            raise BacktestReplayContextBuildError(
                "Snapshot opportunity khác nội dung nguồn đã ghim."
            )
        return source

    def __call__(self, snapshot: DecisionSnapshot) -> ReplayEvaluationContext:
        """Trả event mới giữa hai quyết định, không lặp bar hoặc phát bar tương lai."""
        if self._next_observation >= len(self._observations):
            raise BacktestReplayContextBuildError("Context provider đã hết observation.")
        observation = self._observations[self._next_observation]
        if snapshot != observation.snapshot:
            raise BacktestReplayContextBuildError(
                "RuntimeReplayEvaluator phải dùng observation nguyên bản "
                "theo thứ tự BacktestRunner."
            )

        decision_time = snapshot.decision_time
        events = tuple(
            bar
            for bar in self._execution_bars
            if self._last_decision_time < bar.available_at <= decision_time
        )
        atr_by_bar = tuple(
            (event.symbol, event.bar_key, atr)
            for event in events
            if (atr := _atr_available_at(self._observations, event)) is not None
        )
        opposing_by_bar = tuple(
            (
                event.symbol,
                event.bar_key,
                self._opposing_signal_provider(
                    event, _latest_observation_at(self._observations, event)
                ),
            )
            for event in events
            if self._opposing_signal_provider is not None
        )
        exit_evidence_by_bar = tuple(
            evidence
            for event in events
            for recipe_id, spec in EXIT_EVIDENCE_BY_RECIPE.items()
            if (evidence := self._exit_evidence_at(event, recipe_id, spec)) is not None
        )
        evaluated_at = decision_time + self._evaluated_at_offset
        risk_state = replace(
            self._seed.risk_state,
            evaluated_at=evaluated_at,
            market_data_as_of=decision_time,
        )
        context = replace(
            self._seed,
            evaluated_at=evaluated_at,
            risk_state=risk_state,
            execution_events=events,
            opposing_signal=None,
            atr_by_symbol=(),
            atr_by_bar=atr_by_bar,
            opposing_signal_by_bar=opposing_by_bar,
            exit_evidence_by_bar=exit_evidence_by_bar,
            reserve_risk=_reserve_in_memory,
        )
        self._last_decision_time = decision_time
        self._next_observation += 1
        return context

    def _exit_evidence_at(
        self, event: PaperBar, recipe_id: str, spec: ExitEvidenceSpec
    ) -> ReplayExitEvidence | None:
        """Dựng bằng chứng thoát chỉ từ nến/feature đã biết tại execution bar.

        Bundle thiếu khung chiến lược của recipe thì không phát bằng chứng; evaluator sẽ dừng
        nếu thật sự có vị thế dùng recipe đó, thay vì tự resample nến.
        """
        strategy_bars = self._strategy_bars.get(spec.timeframe)
        if strategy_bars is None:
            return None
        channel_low: Decimal | None = None
        channel_high: Decimal | None = None
        if spec.channel_period is not None:
            # Chỉ dùng nến chiến lược đóng trước khi execution bar mở: mức kênh phải biết
            # trước khi giá của bar này diễn ra, nếu không là nhìn trước tương lai.
            known = [
                bar
                for bar in strategy_bars
                if bar.close_time <= event.opened_at and bar.available_at <= event.available_at
            ]
            if len(known) >= spec.channel_period:
                window = known[-spec.channel_period :]
                channel_low = min(bar.low for bar in window)
                channel_high = max(bar.high for bar in window)
        strategy_bar_closed = event.closed_at in self._strategy_close_times[spec.timeframe]
        feature_value: Decimal | None = None
        if spec.feature_name is not None and strategy_bar_closed:
            observation = _latest_observation_at(self._observations, event)
            if observation is not None:
                ready = [
                    feature
                    for feature in observation.snapshot.features
                    if feature.name == spec.feature_name
                    and feature.timeframe == spec.timeframe
                    and feature.quality is FeatureQuality.READY
                    and feature.value is not None
                    and feature.available_at <= event.available_at
                ]
                if ready:
                    selected = max(ready, key=lambda item: (item.available_at, item.feature_id))
                    feature_value = Decimal(str(selected.value))
        return ReplayExitEvidence(
            symbol=event.symbol,
            bar_key=event.bar_key,
            recipe_id=recipe_id,
            strategy_bar_closed=strategy_bar_closed,
            channel_low=channel_low,
            channel_high=channel_high,
            feature_value=feature_value,
        )


@dataclass(frozen=True, slots=True)
class BacktestDatasetRuntimeEvaluatorFactory:
    """Tạo một context provider, cycle và gateway PAPER mới cho mỗi scenario."""

    bundle: BacktestDatasetReplayBundle
    observations: tuple[ReplayObservation, ...]
    strategies: StrategyRegistry
    execution_interval: str
    context_seed_factory: ContextSeedFactory
    gateway_factory: GatewayFactory
    opposing_signal_provider_factory: OpposingSignalProviderFactory | None = None

    def __call__(
        self, scenario: SensitivityScenario, manifest: ReplayManifest
    ) -> RuntimeReplayEvaluator:
        """Khép wiring bundle → PAPER evaluator mà không chia sẻ state giữa trial."""
        _validate_manifest_bundle(self.bundle, manifest)
        gateway = self.gateway_factory(scenario, manifest)
        seed = self.context_seed_factory(scenario, manifest)
        opposing_provider = (
            self.opposing_signal_provider_factory(scenario, manifest)
            if self.opposing_signal_provider_factory is not None
            else None
        )
        context_provider = BacktestDatasetReplayContextProvider(
            self.bundle,
            self.observations,
            seed=seed,
            execution_interval=self.execution_interval,
            opposing_signal_provider=opposing_provider,
            capture_opportunities=(
                scenario.kind == "BASE"
                and manifest.learning_intent == "TRAINING_BASE"
                and all(
                    item.manifest.has_pinned_source_identities for item in self.bundle.components
                )
            ),
        )
        cycle = PaperDecisionCycle(
            self.strategies,
            TradePlanner(),
            RiskEngine(),
            gateway,
        )
        return RuntimeReplayEvaluator(
            cycle=cycle,
            strategies=self.strategies,
            gateway=gateway,
            manifest=manifest,
            context_provider=context_provider,
            scenario=scenario,
        )


def run_backtest_dataset_runtime_replay(
    bundle: BacktestDatasetReplayBundle,
    manifest: ReplayManifest,
    *,
    strategies: StrategyRegistry,
    execution_interval: str,
    context_seed_factory: ContextSeedFactory,
    gateway_factory: GatewayFactory,
    opposing_signal_provider_factory: OpposingSignalProviderFactory | None = None,
    scenarios: tuple[SensitivityScenario, ...] | None = None,
    runner: BacktestRunner | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> RuntimePaperReplayComparison:
    """Chạy bundle đã kiểm quyền qua snapshot builder và PAPER runtime evaluator."""
    _validate_manifest_bundle(bundle, manifest)
    observations = build_backtest_dataset_replay_observations(
        bundle,
        strategy_release_id=manifest.strategy_release_id,
    )
    evaluator_factory = BacktestDatasetRuntimeEvaluatorFactory(
        bundle=bundle,
        observations=observations,
        strategies=strategies,
        execution_interval=execution_interval,
        context_seed_factory=context_seed_factory,
        gateway_factory=gateway_factory,
        opposing_signal_provider_factory=opposing_signal_provider_factory,
    )
    return run_runtime_paper_replay(
        observations,
        manifest=manifest,
        runtime_evaluator_factory=evaluator_factory,
        scenarios=scenarios,
        runner=runner,
        progress_callback=progress_callback,
        cancel_requested=cancel_requested,
    )


def _validate_manifest_bundle(
    bundle: BacktestDatasetReplayBundle,
    manifest: ReplayManifest,
) -> None:
    if (
        manifest.dataset_hash != bundle.dataset_hash
        or manifest.range_start != bundle.evaluation_start
        or manifest.range_end != bundle.range_end
    ):
        raise BacktestReplayContextBuildError(
            "ReplayManifest phải ghim đúng bundle và evaluation window."
        )


def _to_paper_bar(symbol: str, interval: str, bar: OHLCVBar) -> PaperBar:
    if bar.symbol != symbol or bar.timeframe != interval:
        raise BacktestReplayContextBuildError("Execution bar lệch symbol/timeframe của bundle.")
    if not bar.is_closed or bar.available_at < bar.close_time:
        raise BacktestReplayContextBuildError("Execution bar chưa đóng hoặc available sai mốc.")
    return PaperBar(
        bar_key=f"{symbol}:{interval}:{bar.open_time.isoformat()}",
        symbol=symbol,
        opened_at=bar.open_time,
        closed_at=bar.close_time,
        available_at=bar.available_at,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
    )


def _atr_available_at(
    observations: tuple[ReplayObservation, ...], event: PaperBar
) -> Decimal | None:
    available = [
        feature
        for observation in observations
        for feature in observation.snapshot.features
        if feature.name == "atr_14"
        and feature.quality is FeatureQuality.READY
        and feature.value is not None
        and feature.available_at <= event.available_at
    ]
    if not available:
        return None
    selected = max(available, key=lambda item: (item.available_at, item.feature_id))
    atr = Decimal(str(selected.value))
    return atr if atr.is_finite() and atr > 0 else None


def _latest_observation_at(
    observations: tuple[ReplayObservation, ...], event: PaperBar
) -> ReplayObservation | None:
    available = tuple(
        item for item in observations if item.snapshot.decision_time <= event.available_at
    )
    if not available:
        return None
    return max(
        available,
        key=lambda item: (item.snapshot.decision_time, item.available_at, item.event_id),
    )


def _reserve_in_memory(decision: RiskDecision, idempotency_key: str) -> bool:
    """Không cho replay gọi reservation persistence bên ngoài process."""
    return decision.approved and bool(idempotency_key.strip())
