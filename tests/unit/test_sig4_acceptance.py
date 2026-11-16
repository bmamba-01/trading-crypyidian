"""SIG-4 chấp nhận (trong tiến trình): nến M5 → H18 → kế hoạch có thang → PAPER → TP1 → TP2.

Chạy ``PaperDecisionCycle`` thật qua đường settings đã publish với registry sản phẩm; giờ cố định
16/11/2026 10:05 UTC (phiên London) nên không phụ thuộc đồng hồ máy. Không có kết nối sàn: gateway
PAPER khớp trên nến 1m giả lập.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from test_sig4_scalping_rules import _h18_bars, _snapshot
from tests.unit.test_risk_engine import _settings as _planner_settings
from tests.unit.test_workspace_settings import valid_payload

from trading_bot.application.paper_decision_cycle import DecisionCycleOutcome, PaperDecisionCycle
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings
from trading_bot.domain.execution.paper import (
    PaperBar,
    PaperExecutionGateway,
    PaperExecutionPolicy,
)
from trading_bot.domain.risk.engine import RiskEngine, RiskState
from trading_bot.domain.risk.planner import AccountSnapshot, CostModel, TradePlan, TradePlanner

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
VERSION_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
SETTINGS_HASH = "e" * 64


def _published() -> PublishedWorkspaceSettings:
    payload = valid_payload()
    payload["market_data"] = {
        "venue": "BINANCE_USDM",
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "timeframes": ["1m", "5m", "15m"],
        "max_age_seconds": 30,
    }
    payload["strategy"] = {
        "default_release": "h18-v1",
        "parameters": {},
        "entry_timeframe": "5m",
        "confirmation_timeframe": "15m",
    }
    return PublishedWorkspaceSettings(
        workspace_id=WORKSPACE_ID,
        settings_version_id=VERSION_ID,
        settings_hash=SETTINGS_HASH,
        settings=WorkspaceSettings.model_validate(payload),
    )


def _minute(key: str, opened, *, high: str, low: str, close: str) -> PaperBar:  # type: ignore[no-untyped-def]
    return PaperBar(
        bar_key=key,
        symbol="BTCUSDT",
        opened_at=opened,
        closed_at=opened + timedelta(minutes=1),
        available_at=opened + timedelta(minutes=1),
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
    )


def test_h18_candidate_flows_to_laddered_paper_trade_with_partial_take_profit() -> None:
    snapshot = _snapshot(_h18_bars(), {"atr_14": 0.5})
    # Snapshot dùng settings của rule test; chọn lại theo version đã publish.
    now = snapshot.decision_time
    evaluated_at = now + timedelta(seconds=1)
    release_hash = strategy_registry().get_release("h18-v1").content_hash
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
        captured_at=now - timedelta(seconds=1),
        equity=Decimal("10000"),
        available_margin=Decimal("1000"),
        is_known=True,
    )
    state = RiskState(
        workspace_id=str(WORKSPACE_ID),
        account_id="paper-a",
        settings_version_id=str(VERSION_ID),
        risk_profile_version_id="risk-v1",
        evaluated_at=evaluated_at,
        account_captured_at=account.captured_at,
        market_data_as_of=now - timedelta(seconds=30),
        equity=account.equity,
        available_margin=account.available_margin,
        open_exposures=(),
        realized_loss_today=Decimal("0"),
        realized_loss_rolling=Decimal("0"),
        open_position_count=0,
        is_known=True,
        pending_reservation_initial_margin=Decimal("0"),
    )
    # Quyết định owner #4: VIP0 taker 0,05% hai chiều, không giảm BNB.
    costs = CostModel(
        "vip0-taker", Decimal("0.0005"), Decimal("0.0005"), Decimal("0"), Decimal("0")
    )
    gateway = PaperExecutionGateway(
        PaperExecutionPolicy(
            version_id="paper-fill-v1",
            latency=timedelta(0),
            spread_rate=Decimal("0"),
            slippage_rate=Decimal("0"),
            fee_rate=Decimal("0.0005"),
        )
    )
    cycle = PaperDecisionCycle(strategy_registry(), TradePlanner(), RiskEngine(), gateway)
    result = cycle.evaluate_published(
        published_settings=_published(),
        snapshot=snapshot,
        evaluated_at=evaluated_at,
        planner_settings=planner_settings,
        account=account,
        risk_state=state,
        open_exposures=(),
        cost_model=costs,
        idempotency_key="cycle:sig4:h18",
    )
    assert result.outcome is DecisionCycleOutcome.PAPER_INTENT_CREATED, result
    plan = result.plan
    assert isinstance(plan, TradePlan)
    assert plan.strategy_release_id == "h18-v1"
    assert plan.direction == "short"
    assert len(plan.targets) == 2
    assert plan.targets[0].stop_to_entry_after is True
    order = result.order
    assert order is not None

    # Khớp lệnh vào trên nến 1m kế tiếp.
    opened = evaluated_at.replace(second=0, microsecond=0) + timedelta(minutes=1)
    entry = plan.entry_reference_price
    gateway.apply_market_event(
        _minute("m1", opened, high=str(entry + 1), low=str(entry - 1), close=str(entry)),
        available_quantity=plan.quantity * 10,
    )
    position = gateway.position(order.intent_id)
    assert position is not None
    assert position.quantity == plan.quantity
    tp1, tp2 = (target.price for target in plan.targets)

    first = gateway.monitor_position(
        position,
        _minute(
            "m2", opened + timedelta(minutes=1), high=str(tp1 + 1), low=str(tp1), close=str(tp1)
        ),
    )
    assert first.partial_exit is not None
    assert first.partial_exit.target_index == 1
    reduced = first.position
    assert not reduced.is_closed
    assert reduced.stop_price == position.entry_execution_price  # hòa vốn sau TP1

    second = gateway.monitor_position(
        reduced,
        _minute("m3", opened + timedelta(minutes=2), high=str(tp1), low=str(tp2), close=str(tp2)),
    )
    assert second.exit_reason == "TAKE_PROFIT"
    assert second.position.is_closed
    assert second.position.targets_hit == 2
