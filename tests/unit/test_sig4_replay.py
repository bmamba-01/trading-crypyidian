"""SIG-4: replay PAPER theo thời gian dữ liệu — H18 → thang chốt lời → TP1 một phần → TP2."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from tests.fixtures.sig4_runtime_replay import DAY, build_h18_dataset_runtime_replay

from trading_bot.application.backtest_sensitivity import SensitivityScenario
from trading_bot.domain.execution.paper import PaperExecutionGateway

WORKSPACE = UUID("51640000-0000-4000-8000-000000000001")


def _positions(gateway: PaperExecutionGateway):  # type: ignore[no-untyped-def]
    return list(gateway._positions.values())  # noqa: SLF001 - soi trạng thái PAPER trong test


def test_h18_replay_takes_partial_profit_then_closes_at_second_target() -> None:
    gateways: list[PaperExecutionGateway] = []
    _, comparison = build_h18_dataset_runtime_replay(WORKSPACE, gateways=gateways)
    assert comparison.is_complete
    report = comparison.runs[0].sensitivity_run.result
    assert report.trade_count == 1
    (trade,) = comparison.runs[0].trade_outcomes
    assert trade.opened_at == DAY + timedelta(hours=10, minutes=5)
    assert trade.closed_at == DAY + timedelta(hours=10, minutes=20) - timedelta(milliseconds=1)
    (position,) = _positions(gateways[0])
    assert position.is_closed
    assert position.direction == "short"
    assert position.exit_reason == "TAKE_PROFIT"
    assert position.targets_hit == 2
    tp1, tp2 = position.targets
    assert tp1.stop_to_entry_after
    half = position.initial_quantity * Decimal("0.5")
    entry = position.entry_execution_price
    # Lãi gộp = nửa khối lượng chốt ở TP1 + phần còn lại ở TP2 (khớp đúng giá mục tiêu).
    expected = half * (entry - tp1.price) + (position.initial_quantity - half) * (entry - tp2.price)
    assert position.gross_realized_pnl == expected
    assert report.net_pnl == position.net_realized_pnl
    assert report.net_pnl > 0
    # Phí taker 0,05% mỗi chiều: vào một lần + hai lần thoát.
    assert position.fee_cost == (
        position.initial_quantity * entry + half * tp1.price + half * tp2.price
    ) * Decimal("0.0005")


def test_same_bundle_gives_same_report_hash_and_cost_scenario_is_worse() -> None:
    scenarios = (
        SensitivityScenario("base", "BASE"),
        SensitivityScenario(
            "taker-double",
            "COST",
            cost_overrides=(("entry_fee_rate", "0.001"), ("exit_fee_rate", "0.001")),
        ),
    )
    _, first = build_h18_dataset_runtime_replay(WORKSPACE, scenarios=scenarios)
    _, second = build_h18_dataset_runtime_replay(WORKSPACE, scenarios=scenarios)
    hashes = [run.sensitivity_run.result.report_hash for run in first.runs]
    assert hashes == [run.sensitivity_run.result.report_hash for run in second.runs]
    base, costly = (run.sensitivity_run.result for run in first.runs)
    assert costly.trade_count == base.trade_count == 1
    assert costly.net_pnl < base.net_pnl
