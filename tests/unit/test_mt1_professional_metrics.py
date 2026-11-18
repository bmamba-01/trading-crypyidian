"""Unit test MT-1: ledger tất định cho MTM equity, drawdown %, Sharpe/Sortino/Calmar."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_bot.domain.portfolio.analytics import (
    MTMEquityBar,
    calmar_ratio,
    cost_to_gross_profit_ratio,
    daily_returns,
    max_consecutive_losses,
    max_drawdown_pct,
    mtm_equity_curve,
    r_multiple_distribution,
    sharpe_ratio,
    slippage_vs_plan_ratio,
    sortino_ratio,
)
from trading_bot.domain.portfolio.post_mortem import (
    TradeQualityClassification,
    evaluate_trade_post_mortem,
)
from trading_bot.domain.portfolio.run_report import BotRunInput, TradeExtremes, build_run_report

DAY0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def test_mtm_equity_drawdown_pct_and_daily_risk_adjusted_ratios() -> None:
    bars = [
        MTMEquityBar(DAY0, Decimal("0"), Decimal("0")),  # 1000
        MTMEquityBar(DAY0 + timedelta(days=1), Decimal("100"), Decimal("0")),  # 1100 (+10%)
        MTMEquityBar(
            DAY0 + timedelta(days=2), Decimal("100"), Decimal("-110")
        ),  # 990 (-10% from peak 1100)
        MTMEquityBar(DAY0 + timedelta(days=3), Decimal("200"), Decimal("0")),  # 1200 (+21.2121%)
    ]
    curve = mtm_equity_curve(Decimal("1000"), bars)
    assert [p.equity for p in curve] == [
        Decimal("1000"),
        Decimal("1100"),
        Decimal("990"),
        Decimal("1200"),
    ]
    assert curve[2].peak_equity == Decimal("1100")
    assert curve[2].drawdown_abs == Decimal("110")
    assert curve[2].drawdown_pct == Decimal("10")
    dd_pct = max_drawdown_pct(curve)
    assert dd_pct == Decimal("10")

    rets = daily_returns(curve)
    assert len(rets) == 3
    assert rets[0] == Decimal("0.1")
    assert rets[1] == Decimal("-0.1")

    sr = sharpe_ratio(rets)
    so = sortino_ratio(rets)
    ca = calmar_ratio(rets, max_dd_pct=dd_pct)
    assert sr is not None
    assert sr > Decimal("0")
    assert so is not None
    assert so > Decimal("0")
    assert ca is not None
    assert ca > Decimal("0")

    # Fail-closed khi < 2 ngày hoặc drawdown = 0.
    assert sharpe_ratio([Decimal("0.01")]) is None
    assert sortino_ratio([Decimal("0.01")]) is None
    assert calmar_ratio(rets, max_dd_pct=Decimal("0")) is None


def test_r_distribution_consecutive_losses_and_cost_ratios() -> None:
    r_vals = [
        Decimal("-2.5"),
        Decimal("-1.0"),
        Decimal("-0.5"),
        Decimal("0.5"),
        Decimal("1.5"),
        Decimal("3.0"),
    ]
    dist = r_multiple_distribution(r_vals)
    assert dist["count"] == 6
    assert dist["buckets"] == {
        "<=-2R": 1,
        "(-2R,-1R]": 1,
        "(-1R,0R)": 1,
        "[0R,1R)": 1,
        "[1R,2R)": 1,
        ">=2R": 1,
    }
    assert dist["min"] == "-2.5"
    assert dist["max"] == "3"
    assert dist["median"] == "0"
    assert (
        max_consecutive_losses(
            [Decimal("10"), Decimal("-5"), Decimal("-2"), Decimal("-1"), Decimal("4")]
        )
        == 3
    )
    assert cost_to_gross_profit_ratio(Decimal("25"), Decimal("100")) == Decimal("0.2500")
    assert cost_to_gross_profit_ratio(Decimal("25"), Decimal("0")) is None
    assert slippage_vs_plan_ratio(Decimal("3"), Decimal("2")) == Decimal("1.5000")


def test_post_mortem_false_breakout_by_bars_and_process_score() -> None:
    # Khung 1h (3600s), giữ 2 giờ = 2 nến (< 3 nến) → DEVIANT_FALSE_BREAKOUT dù > 900s!
    trade_fast_1h = {
        "trade_id": str(uuid4()),
        "bot_id": str(uuid4()),
        "symbol": "BTCUSDT",
        "side": "LONG",
        "quote_asset": "USDT",
        "gross_pnl": "-9",
        "fee_total": "0.5",
        "funding_total": "0",
        "slippage_total": "0.5",
        "net_pnl": "-10",
        "exit_reason": "STOP_LOSS",
        "opened_at": DAY0.isoformat(),
        "closed_at": (DAY0 + timedelta(hours=2)).isoformat(),
        "entry_reason": {"context_gate_passed": True},
    }
    pm = evaluate_trade_post_mortem(
        trade_fast_1h,
        plan={"estimated_loss": "10", "entry_slippage_cost": "1"},
        timeframe_seconds=3600,
    )
    assert pm.classification == TradeQualityClassification.DEVIANT_FALSE_BREAKOUT
    assert pm.process_checks["not_false_breakout"] is False
    assert pm.process_score_pct == Decimal("80.00")

    # Cùng thời gian giữ 2 giờ nhưng trên khung 15m (900s) = 8 nến (>= 3 nến) → không phá vỡ giả.
    pm_15m = evaluate_trade_post_mortem(
        trade_fast_1h,
        plan={"estimated_loss": "10", "entry_slippage_cost": "1"},
        timeframe_seconds=900,
    )
    assert pm_15m.classification == TradeQualityClassification.ACCEPTABLE
    assert pm_15m.process_score_pct == Decimal("100.00")


def test_run_report_includes_mt1_metrics() -> None:
    trades = [
        {
            "trade_id": "t1",
            "symbol": "BTCUSDT",
            "side": "long",
            "net_pnl": "20",
            "exit_reason": "TAKE_PROFIT",
            "opened_at": DAY0.isoformat(),
            "closed_at": (DAY0 + timedelta(hours=1)).isoformat(),
            "fills": [{"role": "ENTRY", "quantity": "1", "price": "100"}],
        },
        {
            "trade_id": "t2",
            "symbol": "BTCUSDT",
            "side": "long",
            "net_pnl": "-10",
            "exit_reason": "STOP_LOSS",
            "opened_at": (DAY0 + timedelta(days=1)).isoformat(),
            "closed_at": (DAY0 + timedelta(days=1, hours=1)).isoformat(),
            "fills": [{"role": "ENTRY", "quantity": "1", "price": "100"}],
        },
    ]
    bot = BotRunInput(
        bot_id="b1",
        bot_name="B1",
        release_id="h01-v1",
        timeframe="1h",
        parameters={},
        risk={},
        universe=("BTCUSDT",),
        bot_report={
            "report_hash": "a" * 64,
            "trades": trades,
            "open_trades": [],
            "totals": {"USDT": {"net_pnl": "10", "gross_pnl": "12", "total_cost": "2"}},
        },
        extremes=(
            TradeExtremes("t1", Decimal("10"), Decimal("99"), Decimal("103")),
            TradeExtremes("t2", Decimal("10"), Decimal("98"), Decimal("101")),
        ),
        initial_capital=Decimal("1000"),
        mtm_bars=(
            MTMEquityBar(DAY0, Decimal("0"), Decimal("0")),
            MTMEquityBar(DAY0 + timedelta(days=1), Decimal("20"), Decimal("0")),
            MTMEquityBar(DAY0 + timedelta(days=2), Decimal("10"), Decimal("0")),
        ),
    )
    report = build_run_report(
        workspace_id="w1",
        run_label="mt1",
        window_start=DAY0,
        window_end=DAY0 + timedelta(days=3),
        bots=[bot],
    )
    perf = report.payload["bots"][0]["performance"]
    assert perf["r_distribution"]["buckets"][">=2R"] == 1
    assert perf["r_distribution"]["buckets"]["(-2R,-1R]"] == 1
    assert perf["max_consecutive_losses"] == 1
    assert perf["cost_to_gross_profit_ratio"] is not None
    assert pytest.approx(float(perf["cost_to_gross_profit_ratio"]), abs=0.001) == 2 / 12
    assert perf["risk_adjusted"]["sharpe_ratio"] is not None
    assert perf["risk_adjusted"]["sortino_ratio"] is not None
    assert perf["risk_adjusted"]["calmar_ratio"] is not None
    assert perf["risk_adjusted"]["max_drawdown_percent"] is not None
