"""Fixture tích hợp để replay PAPER có thể tái lập trong unit và Compose E2E."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid5

from trading_bot.application.backtest import ReplayManifest, ReplayObservation
from trading_bot.application.backtest_sensitivity import SensitivityScenario
from trading_bot.application.paper_decision_cycle import PaperDecisionCycle
from trading_bot.application.runtime_replay_evaluator import (
    ReplayEvaluationContext,
    RuntimePaperReplayComparison,
    RuntimeReplayEvaluator,
    run_runtime_paper_replay,
)
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings
from trading_bot.domain.execution.paper import PaperBar, PaperExecutionGateway, PaperExecutionPolicy
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.risk.engine import RiskDecision, RiskEngine, RiskState
from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    CostModel,
    ExchangeRules,
    OpenExposure,
    PlannerSettings,
    RiskLimits,
    TradePlanner,
)
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    FeatureObservation,
    MarketSeries,
    SeriesQuality,
)
from trading_bot.domain.strategy.registry import StrategyRegistry


def build_h03_runtime_sensitivity(
    workspace_id: UUID,
    scenarios: tuple[SensitivityScenario, ...],
    *,
    include_second_trade: bool = False,
) -> RuntimePaperReplayComparison:
    """Chạy H03-v1 qua PAPER runtime với evaluator/provider mới cho từng trial."""
    start = datetime(2026, 10, 20, tzinfo=UTC)
    decision_time = start + timedelta(hours=60)
    entry_time = decision_time + timedelta(hours=2)
    exit_time = entry_time + timedelta(hours=1)
    settings_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    risk_id = uuid5(workspace_id, "h03-runtime-replay-risk-v1")
    registry = StrategyRegistry()
    release = registry.get_release("h03-v1")
    settings_hash = "f" * 64
    payload = {
        "schema_version": 1,
        "runtime": {"mode": "PAPER", "timezone": "UTC"},
        "market_data": {
            "venue": "BINANCE_USDM",
            "symbols": ["BTCUSDT"],
            "timeframes": ["1h"],
            "max_age_seconds": 7200,
        },
        "features": {"formula_version": "fixture-v1", "sessions_timezone": "UTC"},
        "strategy": {
            "default_release": "h03-v1",
            "parameters": {},
            "entry_timeframe": "1h",
            "confirmation_timeframe": "1h",
        },
        "risk": {"profile_version_id": str(risk_id)},
        "paper_execution": {
            "quote_asset": "USDT",
            "starting_equity": "10000",
            "fee_schedule_version": "fixture-fees-v1",
            "fill_model": "next_bar_with_adverse_costs",
        },
    }
    published = PublishedWorkspaceSettings(
        workspace_id=workspace_id,
        settings_version_id=settings_id,
        settings_hash=settings_hash,
        settings=WorkspaceSettings.model_validate(payload),
    )
    planner_settings = PlannerSettings(
        workspace_id=str(workspace_id),
        settings_version_id=str(settings_id),
        risk_profile_version_id=str(risk_id),
        published=True,
        active_release_ids=("h03-v1",),
        leverage=Decimal("2"),
        exchange=ExchangeRules(
            Decimal("0.1"),
            Decimal("0.01"),
            Decimal("1"),
            Decimal("5"),
            Decimal("10000"),
            Decimal("100"),
        ),
        limits=RiskLimits(
            per_trade_risk_fraction=Decimal("0.01"),
            max_aggregate_open_risk=Decimal("250"),
            max_notional=Decimal("10000"),
            max_leverage=Decimal("5"),
            max_positions=2,
            max_symbol_notional=Decimal("2000"),
            max_concentration_fraction=Decimal("0.2"),
            max_daily_loss=Decimal("200"),
            max_rolling_loss=Decimal("500"),
            max_account_age=timedelta(seconds=30),
            max_data_age=timedelta(minutes=5),
        ),
        release_content_hashes=(("h03-v1", release.content_hash),),
        settings_hash=settings_hash,
    )
    bars = tuple(
        OHLCVBar(
            symbol="BTCUSDT",
            timeframe="1h",
            open_time=decision_time - timedelta(hours=56 - index),
            close_time=decision_time - timedelta(hours=55 - index),
            event_at=decision_time - timedelta(hours=55 - index),
            received_at=decision_time - timedelta(hours=55 - index),
            available_at=decision_time - timedelta(hours=55 - index),
            open=Decimal("99"),
            high=Decimal("100") if index < 55 else Decimal("102"),
            low=Decimal("95"),
            close=Decimal("101.5") if index == 55 else Decimal("99"),
            volume=Decimal("10"),
            is_closed=True,
        )
        for index in range(56)
    )
    snapshot = DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=decision_time,
        snapshot_id="h03-entry-signal",
        series=(
            MarketSeries(
                timeframe="1h",
                bars=bars,
                quote_volumes=(5.0,) * 55 + (6.0,),
                quality=SeriesQuality.READY,
                expected_interval=timedelta(hours=1),
                max_event_age=timedelta(hours=4),
                backfill_complete=True,
            ),
        ),
        features=(
            FeatureObservation(
                feature_id="h03-atr-14",
                name="atr_14",
                timeframe="1h",
                value=2.0,
                previous_value=None,
                available_at=decision_time,
            ),
        ),
        position_is_flat=True,
    )
    entry_snapshot = replace(
        snapshot,
        snapshot_id="h03-entry-fill",
        decision_time=entry_time,
        position_is_flat=False,
    )
    exit_snapshot = replace(
        snapshot,
        snapshot_id="h03-stop-exit",
        decision_time=exit_time,
        position_is_flat=False,
    )
    entry_event = PaperBar(
        bar_key="h03-entry-event",
        symbol="BTCUSDT",
        opened_at=decision_time + timedelta(hours=1),
        closed_at=entry_time,
        available_at=entry_time,
        open=Decimal("101.5"),
        high=Decimal("103"),
        low=Decimal("100"),
        close=Decimal("102"),
    )
    exit_event = PaperBar(
        bar_key="h03-stop-event",
        symbol="BTCUSDT",
        opened_at=entry_time,
        closed_at=exit_time,
        available_at=exit_time,
        open=Decimal("102"),
        high=Decimal("103"),
        low=Decimal("95"),
        close=Decimal("100"),
    )
    observations = [
        ReplayObservation("h03-signal", decision_time, decision_time, snapshot),
        ReplayObservation("h03-entry", entry_time, entry_time, entry_snapshot),
        ReplayObservation("h03-exit", exit_time, exit_time, exit_snapshot),
    ]
    second_signal_time = exit_time + timedelta(hours=1)
    second_entry_time = second_signal_time + timedelta(hours=2)
    second_exit_time = second_entry_time + timedelta(hours=1)
    second_ids: tuple[str, str, str] | None = None
    second_events: dict[str, PaperBar] = {}
    if include_second_trade:
        second_ids = ("h03-entry-signal-2", "h03-entry-fill-2", "h03-stop-exit-2")

        def shift_snapshot(source: DecisionSnapshot, snapshot_id: str, point: datetime):
            delta = point - source.decision_time
            shifted_series = tuple(
                replace(
                    item,
                    bars=tuple(
                        replace(
                            bar,
                            open_time=bar.open_time + delta,
                            close_time=bar.close_time + delta,
                            event_at=bar.event_at + delta,
                            received_at=bar.received_at + delta,
                            available_at=bar.available_at + delta,
                        )
                        for bar in item.bars
                    ),
                )
                for item in source.series
            )
            shifted_features = tuple(
                replace(feature, available_at=feature.available_at + delta)
                for feature in source.features
            )
            return replace(
                source,
                snapshot_id=snapshot_id,
                decision_time=point,
                series=shifted_series,
                features=shifted_features,
            )

        second_signal = shift_snapshot(snapshot, second_ids[0], second_signal_time)
        second_entry = shift_snapshot(entry_snapshot, second_ids[1], second_entry_time)
        second_exit = shift_snapshot(exit_snapshot, second_ids[2], second_exit_time)
        second_entry_event = replace(
            entry_event,
            bar_key="h03-entry-event-2",
            opened_at=second_signal_time + timedelta(hours=1),
            closed_at=second_entry_time,
            available_at=second_entry_time,
        )
        second_exit_event = replace(
            exit_event,
            bar_key="h03-stop-event-2",
            opened_at=second_entry_time,
            closed_at=second_exit_time,
            available_at=second_exit_time,
        )
        second_events = {
            second_ids[1]: second_entry_event,
            second_ids[2]: second_exit_event,
        }
        observations.extend(
            (
                ReplayObservation(
                    "h03-signal-2", second_signal_time, second_signal_time, second_signal
                ),
                ReplayObservation(
                    "h03-entry-2", second_entry_time, second_entry_time, second_entry
                ),
                ReplayObservation("h03-exit-2", second_exit_time, second_exit_time, second_exit),
            )
        )
    base_costs = (
        ("entry_fee_rate", "0.001"),
        ("exit_fee_rate", "0.001"),
        ("adverse_funding_per_unit", "0"),
        ("entry_slippage_rate", "0"),
        ("exit_slippage_rate", "0"),
        ("spread_rate", "0"),
    )
    manifest = ReplayManifest(
        dataset_hash="f" * 64 if include_second_trade else "a" * 64,
        range_start=start,
        range_end=(second_exit_time if include_second_trade else exit_time) + timedelta(hours=1),
        settings_version_id=str(settings_id),
        risk_profile_version_id=str(risk_id),
        strategy_release_id="h03-v1",
        strategy_content_hash=release.content_hash,
        feature_schema_hash="c" * 64,
        fill_model_version="paper-fill-v1",
        pipeline_content_hash="d" * 64,
        cost_assumptions=base_costs,
        parameter_family_hash="e" * 64,
        trial_count=1,
        trial_index=0,
        seed=19,
    )

    def evaluator_factory(
        scenario: SensitivityScenario, scenario_manifest: ReplayManifest
    ) -> RuntimeReplayEvaluator:
        assumptions = dict(scenario_manifest.cost_assumptions)
        entry_fee = Decimal(assumptions["entry_fee_rate"])
        exit_fee = Decimal(assumptions["exit_fee_rate"])
        cost_fingerprint = hashlib.sha256(
            json.dumps(sorted(assumptions.items()), separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        costs = CostModel(
            version_id=f"cost-{cost_fingerprint}",
            entry_fee_rate=entry_fee,
            exit_fee_rate=exit_fee,
            adverse_funding_per_unit=Decimal(assumptions["adverse_funding_per_unit"]),
            entry_slippage_rate=Decimal(assumptions["entry_slippage_rate"]),
            exit_slippage_rate=Decimal(assumptions["exit_slippage_rate"]),
        )
        gateway = PaperExecutionGateway(
            PaperExecutionPolicy(
                version_id=scenario_manifest.fill_model_version,
                latency=timedelta(0),
                spread_rate=Decimal(assumptions["spread_rate"]),
                slippage_rate=Decimal(assumptions["entry_slippage_rate"]),
                fee_rate=entry_fee,
            )
        )
        cycle = PaperDecisionCycle(registry, TradePlanner(), RiskEngine(), gateway)
        reservations: set[str] = set()

        def context_provider(current: DecisionSnapshot) -> ReplayEvaluationContext:
            evaluated_at = current.decision_time + timedelta(seconds=1)
            account = AccountSnapshot(
                workspace_id=str(workspace_id),
                account_id="paper-fixture",
                settings_version_id=str(settings_id),
                captured_at=current.decision_time - timedelta(seconds=1),
                equity=Decimal("10000"),
                available_margin=Decimal("1000"),
                is_known=True,
            )
            flat_signal_ids = {"h03-entry-signal"}
            if second_ids is not None:
                flat_signal_ids.add(second_ids[0])
            exposures = (
                (
                    OpenExposure(
                        str(workspace_id),
                        "paper-fixture",
                        "BTCUSDT",
                        Decimal("100"),
                        Decimal("10"),
                    ),
                )
                if current.snapshot_id not in flat_signal_ids
                else ()
            )
            state = RiskState(
                workspace_id=str(workspace_id),
                account_id="paper-fixture",
                settings_version_id=str(settings_id),
                risk_profile_version_id=str(risk_id),
                evaluated_at=evaluated_at,
                account_captured_at=account.captured_at,
                market_data_as_of=current.decision_time,
                equity=account.equity,
                available_margin=account.available_margin,
                open_exposures=exposures,
                realized_loss_today=Decimal("0"),
                realized_loss_rolling=Decimal("0"),
                open_position_count=len(exposures),
                is_known=True,
                pending_reservation_initial_margin=Decimal("0"),
            )
            execution_events = (
                (entry_event,)
                if current.snapshot_id == "h03-entry-fill"
                else (exit_event,)
                if current.snapshot_id == "h03-stop-exit"
                else (second_events[current.snapshot_id],)
                if current.snapshot_id in second_events
                else ()
            )

            def reserve_risk(decision: RiskDecision, key: str) -> bool:
                if not decision.approved or key in reservations:
                    return False
                reservations.add(key)
                return True

            return ReplayEvaluationContext(
                published_settings=published,
                evaluated_at=evaluated_at,
                planner_settings=planner_settings,
                account=account,
                risk_state=state,
                open_exposures=exposures,
                cost_model=costs,
                feature_schema_hash="c" * 64,
                pipeline_content_hash="d" * 64,
                execution_events=execution_events,
                reserve_risk=reserve_risk,
            )

        return RuntimeReplayEvaluator(
            cycle=cycle,
            strategies=registry,
            gateway=gateway,
            manifest=scenario_manifest,
            context_provider=context_provider,
            scenario=scenario,
        )

    return run_runtime_paper_replay(
        tuple(observations),
        manifest=manifest,
        scenarios=scenarios,
        runtime_evaluator_factory=evaluator_factory,
    )
