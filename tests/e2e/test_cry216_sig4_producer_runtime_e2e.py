"""E2E CRY-216 (SIG-4): producer market-signal-worker + trading-runtime qua create_app."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from tests.unit.test_risk_engine import _settings as _planner_settings
from tests.unit.test_sig4_acceptance import SETTINGS_HASH, VERSION_ID, WORKSPACE_ID, _published
from tests.unit.test_sig4_scalping_rules import _h18_bars, _snapshot
from trading_bot.app import create_app
from trading_bot.application.market_signal_scanner import MarketSignalScanResult
from trading_bot.application.paper_decision_cycle import (
    DecisionCycleOutcome,
    PaperDecisionCycle,
)
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.application.strategy_market_requirements import feature_settings_for
from trading_bot.config.settings import AppSettings
from trading_bot.domain.execution.paper import (
    OrderStatus,
    PaperBar,
    PaperExecutionGateway,
    PaperExecutionPolicy,
)
from trading_bot.domain.market.feature_snapshot import FeatureCalculator
from trading_bot.domain.market.models import MarketSnapshot, OHLCVBar
from trading_bot.domain.risk.engine import RiskEngine, RiskState
from trading_bot.domain.risk.planner import AccountSnapshot, CostModel, TradePlan, TradePlanner
from trading_bot.domain.strategy.contracts import StrategyCandidate
from trading_bot.domain.strategy.rules import release_content_hash
from trading_bot.domain.strategy.scalping_rules import H18SweepReclaimRule
from trading_bot.workers.market_signal_producer import MarketSignalProducerWorker


async def _db_ready() -> bool:
    return True


@pytest.mark.asyncio
async def test_cry216_producer_and_runtime_create_app_with_controlled_clock() -> None:
    """Kiểm tra create_app truyền đồng hồ kiểm soát cho producer và trading-runtime."""
    # Mốc thời gian cố định trong phiên London, ngoài cửa sổ funding ±10p.
    london_bars = _h18_bars()
    controlled_now = london_bars[-1].available_at + timedelta(seconds=1)
    recorded_scan_times: list[datetime] = []
    emitted_candidates: list[StrategyCandidate] = []

    # 1. Pha producer: tạo app market-signal-worker với clock tiêm qua create_app.
    class _FakeScanner:
        async def scan_once(self, *, workspace_id: UUID, now: datetime) -> MarketSignalScanResult:
            recorded_scan_times.append(now)
            # Tính feature 5m bằng FeatureCalculator thật để xác nhận adx_14 & efficiency_ratio_40.
            base = now - timedelta(minutes=5 * 210)
            bars_5m = tuple(
                OHLCVBar(
                    symbol="BTCUSDT",
                    timeframe="5m",
                    open_time=base + timedelta(minutes=5 * i),
                    close_time=base + timedelta(minutes=5 * (i + 1)) - timedelta(microseconds=1),
                    event_at=base + timedelta(minutes=5 * (i + 1)) - timedelta(microseconds=1),
                    received_at=base + timedelta(minutes=5 * (i + 1)),
                    available_at=base + timedelta(minutes=5 * (i + 1)),
                    open=Decimal("100") + Decimal(i % 4) * Decimal("0.2"),
                    high=Decimal("101") + Decimal(i % 4) * Decimal("0.2"),
                    low=Decimal("99.5") + Decimal(i % 4) * Decimal("0.2"),
                    close=Decimal("100.3") + Decimal(i % 4) * Decimal("0.2"),
                    volume=Decimal("10"),
                    is_closed=True,
                )
                for i in range(210)
            )
            calc = FeatureCalculator(feature_settings_for("5m")).calculate(
                MarketSnapshot(as_of=bars_5m[-1].available_at, bars=bars_5m),
                workspace_id=workspace_id,
                input_event_ids=tuple(str(uuid4()) for _ in bars_5m),
                symbol="BTCUSDT",
                timeframe="5m",
                computed_at=bars_5m[-1].available_at,
            )
            assert calc.features["adx_14"] is not None
            assert calc.features["efficiency_ratio_40"] is not None

            # Đánh giá rule H18 trên snapshot phiên London bằng đồng hồ kiểm soát.
            decision_snapshot = _snapshot(london_bars, {"atr_14": 0.5})
            selection = strategy_registry().get_release("h18-v1")
            _ = selection
            from trading_bot.domain.strategy.contracts import StrategySelection

            sel = StrategySelection(
                settings_version_id=str(VERSION_ID),
                active_release_ids=("h18-v1",),
                published=True,
            )
            res = H18SweepReclaimRule().evaluate(decision_snapshot, sel)
            assert isinstance(res, StrategyCandidate)
            emitted_candidates.append(res)
            return MarketSignalScanResult(
                dispatched_evidence_ids=(uuid4(),),
                decision_count=1,
                stale_count=0,
                unusable_count=0,
                expired_count=0,
                no_context_count=0,
                duplicate_count=0,
            )

    def _producer_factory(**kwargs: Any) -> MarketSignalProducerWorker:
        return MarketSignalProducerWorker(
            _FakeScanner(),
            workspace_id=kwargs["workspace_id"],
            tick_seconds=kwargs["tick_seconds"],
            now=lambda: controlled_now,
        )

    producer_app = create_app(
        AppSettings(
            app_env="test",
            process_role="market-signal-worker",
            market_signal_workspace_id=WORKSPACE_ID,
            signal_outcome_tick_seconds=0,
        ),
        session_factory=object(),  # type: ignore[arg-type]
        database_probe=_db_ready,
        market_signal_worker_factory=_producer_factory,
        clock=lambda: controlled_now,
    )
    with TestClient(producer_app) as client:
        assert client.get("/health/live").status_code == 200
        scan_result = await producer_app.state.market_signal_worker.tick()
        assert scan_result.decision_count == 1
        assert scan_result.dispatched_count == 1

    assert len(recorded_scan_times) >= 1
    assert all(ts == controlled_now.astimezone(UTC) for ts in recorded_scan_times)
    assert len(emitted_candidates) >= 1

    # 2. Pha trading-runtime: tạo app trading-runtime với clock tiêm qua create_app.
    captured_supervisor_clocks: list[Any] = []

    class _FakeBootstrap:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def bootstrap(self) -> Any:
            return SimpleNamespace(
                ready=True,
                database_ready=True,
                error_code=None,
                workspace_id=WORKSPACE_ID,
                owner_id="owner-e2e",
                active_accounts={},
                account_states={},
            )

        async def bootstrap_bot(self, *_args: Any, **_kwargs: Any) -> Any:
            return None

    class _FakeSupervisor:
        def __init__(self, **kwargs: Any) -> None:
            captured_supervisor_clocks.append(kwargs.get("clock"))

        async def tick_once(self) -> None:
            return None

        async def run(self) -> None:
            await asyncio.sleep(0)

        async def stop(self) -> None:
            return None

    runtime_app = create_app(
        AppSettings(
            app_env="test",
            process_role="trading-runtime",
            runtime_workspace_id=WORKSPACE_ID,
            runtime_market_dispatch_enabled=True,
        ),
        session_factory=object(),  # type: ignore[arg-type]
        database_probe=_db_ready,
        paper_runtime_bootstrap_factory=_FakeBootstrap,
        paper_decision_context_provider_factory=lambda **_kwargs: object(),
        paper_runtime_supervisor_factory=_FakeSupervisor,
        clock=lambda: controlled_now,
    )
    with TestClient(runtime_app) as client:
        assert client.get("/health/live").status_code == 200
        assert runtime_app.state.paper_market_loop._clock() == controlled_now
        assert len(captured_supervisor_clocks) == 1
        assert captured_supervisor_clocks[0]() == controlled_now

    # 3. Hoàn tất vòng khớp limit maker + thang TP1/TP2 cho candidate vừa phát.
    snapshot = _snapshot(london_bars, {"atr_14": 0.5})
    evaluated_at = snapshot.decision_time + timedelta(seconds=1)
    release_hash = release_content_hash(H18SweepReclaimRule())
    planner_settings = replace(
        _planner_settings(),
        workspace_id=str(WORKSPACE_ID),
        settings_version_id=str(VERSION_ID),
        settings_hash=SETTINGS_HASH,
        active_release_ids=("h18-v1",),
        release_content_hashes=(("h18-v1", release_hash),),
    )
    account = AccountSnapshot(
        workspace_id=str(WORKSPACE_ID),
        account_id="paper-a",
        settings_version_id=str(VERSION_ID),
        captured_at=snapshot.decision_time - timedelta(seconds=1),
        equity=Decimal("10000"),
        available_margin=Decimal("1000"),
        is_known=True,
    )
    risk_state = RiskState(
        workspace_id=str(WORKSPACE_ID),
        account_id="paper-a",
        settings_version_id=str(VERSION_ID),
        risk_profile_version_id="risk-v1",
        evaluated_at=evaluated_at,
        account_captured_at=account.captured_at,
        market_data_as_of=snapshot.decision_time - timedelta(seconds=30),
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
        "vip0-maker", Decimal("0.0002"), Decimal("0.0002"), Decimal("0"), Decimal("0")
    )
    gateway = PaperExecutionGateway(
        PaperExecutionPolicy(
            version_id="sig4-maker-e2e-v1",
            latency=timedelta(0),
            spread_rate=Decimal("0"),
            slippage_rate=Decimal("0"),
            fee_rate=Decimal("0.0005"),
            maker_fee_rate=Decimal("0.0002"),
            min_maker_penetration_ticks=1,
        )
    )
    cycle = PaperDecisionCycle(strategy_registry(), TradePlanner(), RiskEngine(), gateway)
    prepared = cycle.prepare_published(
        published_settings=_published(),
        snapshot=snapshot,
        evaluated_at=evaluated_at,
        planner_settings=planner_settings,
        account=account,
        risk_state=risk_state,
        open_exposures=(),
        cost_model=costs,
    )
    assert prepared.outcome is DecisionCycleOutcome.RISK_APPROVED
    assert isinstance(prepared.plan, TradePlan)
    order = gateway.submit(
        prepared.plan,
        "e2e-maker-h18",
        submitted_at=evaluated_at,
        limit_price=prepared.plan.entry_price,
        max_pending_bars=3,
    )
    # Nến 1m kế tiếp xuyên qua giá limit >= 1 tick -> khớp maker.
    t1 = evaluated_at + timedelta(minutes=1)
    fill_bar = PaperBar(
        bar_key="BTCUSDT:1m:e2e-1",
        symbol="BTCUSDT",
        opened_at=t1,
        closed_at=t1 + timedelta(minutes=1),
        available_at=t1 + timedelta(minutes=1),
        open=prepared.plan.entry_price,
        high=prepared.plan.entry_price + Decimal("0.2"),
        low=prepared.plan.entry_price - Decimal("0.2"),
        close=prepared.plan.entry_price,
    )
    filled = gateway.apply_market_event(fill_bar)[0]
    assert filled.intent_id == order.intent_id
    assert filled.status is OrderStatus.FILLED
    assert filled.fills[0].is_maker is True
