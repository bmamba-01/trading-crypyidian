from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain.learning.evaluation import EvaluationTrade, build_evaluation_metrics


def test_evaluation_report_reports_net_costs_drawdown_expectancy_and_uncertainty() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    trades = (
        EvaluationTrade(
            "t1",
            start,
            start + timedelta(hours=1),
            Decimal("20"),
            Decimal("1"),
            Decimal("2"),
            Decimal("1"),
            Decimal("100"),
        ),
        EvaluationTrade(
            "t2",
            start + timedelta(hours=2),
            start + timedelta(hours=3),
            Decimal("-5"),
            Decimal("1"),
            Decimal("0"),
            Decimal("1"),
            Decimal("100"),
        ),
        EvaluationTrade(
            "t3",
            start + timedelta(hours=4),
            start + timedelta(hours=5),
            Decimal("10"),
            Decimal("0.5"),
            Decimal("1"),
            Decimal("0.5"),
            Decimal("80"),
        ),
    )

    metrics = build_evaluation_metrics(trades, observation_count=4, seed=11)

    assert metrics.trade_count == 3
    assert metrics.coverage == 1.0
    assert metrics.trade_frequency == 0.75
    assert metrics.gross_pnl == Decimal("25")
    assert metrics.net_pnl == Decimal("17")
    assert metrics.fees == Decimal("2.5")
    assert metrics.funding == Decimal("3")
    assert metrics.slippage == Decimal("2.5")
    assert metrics.expectancy == Decimal("17") / Decimal("3")
    assert metrics.max_drawdown == Decimal("7")
    assert metrics.confidence_interval is None
    assert "Chưa đủ mẫu" in metrics.caveat_vi
    assert "không phải" in metrics.caveat_vi
    assert "không phải exposure theo thời gian" in metrics.caveat_vi


def test_evaluation_report_computes_confidence_interval_when_sample_reaches_minimum() -> None:
    from trading_bot.domain.learning.evaluation import MIN_TRADES_FOR_CONFIDENCE_INTERVAL

    start = datetime(2026, 1, 1, tzinfo=UTC)
    trades = tuple(
        EvaluationTrade(
            f"t-{idx}",
            start + timedelta(hours=idx * 2),
            start + timedelta(hours=idx * 2 + 1),
            Decimal("10") if idx % 3 != 0 else Decimal("-4"),
            Decimal("0.5"),
            Decimal("0"),
            Decimal("0.5"),
            Decimal("100"),
        )
        for idx in range(MIN_TRADES_FOR_CONFIDENCE_INTERVAL)
    )
    metrics = build_evaluation_metrics(
        trades, observation_count=MIN_TRADES_FOR_CONFIDENCE_INTERVAL, seed=11
    )
    assert metrics.confidence_interval is not None
    assert metrics.confidence_interval[0] <= metrics.expectancy <= metrics.confidence_interval[1]
    assert "Chưa đủ mẫu" not in metrics.caveat_vi


def test_evaluation_metrics_reject_duplicate_trade_ids_and_bad_time_order() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    trade = EvaluationTrade(
        "t1",
        start,
        start + timedelta(minutes=1),
        Decimal("1"),
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        Decimal("1"),
    )

    with pytest.raises(ValueError, match="trade_id bị trùng"):
        build_evaluation_metrics((trade, trade), observation_count=2, seed=0)


def test_signed_funding_credit_increases_evaluation_net_pnl_once() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    trade = EvaluationTrade(
        "funding-credit",
        start,
        start + timedelta(hours=1),
        Decimal("10"),
        Decimal("1"),
        Decimal("-2"),
        Decimal("1"),
        Decimal("100"),
    )

    metrics = build_evaluation_metrics((trade,), observation_count=1, seed=0)

    assert trade.net_pnl == Decimal("10")
    assert metrics.net_pnl == Decimal("10")
    assert metrics.funding == Decimal("-2")
