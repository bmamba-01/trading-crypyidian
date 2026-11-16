"""Unit test FX-2: cổng lỗ xấu nhất (đã chốt + rủi ro mở + lệnh mới) và ngắt mạch drawdown."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tests.unit.test_risk_engine import _plan, _settings, _state

from trading_bot.domain.risk.drawdown_breaker import (
    PAPER_DRAWDOWN_BREAKER_POLICY,
    ClosedTradePnl,
    DrawdownBreakerPolicy,
    evaluate_drawdown_breaker,
)
from trading_bot.domain.risk.engine import OpenExposure, RiskEngine

T0 = datetime(2026, 11, 15, tzinfo=UTC)


def _exposure(risk: str) -> OpenExposure:
    return OpenExposure("workspace-a", "paper-a", "ETHUSDT", Decimal("100"), Decimal(risk))


def test_daily_loss_gate_counts_open_risk_and_new_plan_loss() -> None:
    plan = _plan()
    room = Decimal("200") - plan.estimated_loss  # ngưỡng lỗ ngày fixture = 200
    allowed = RiskEngine().evaluate(plan, _state(realized_loss_today=room), _settings())
    assert allowed.approved, allowed.reason_code

    over = room + Decimal("0.01")
    blocked = RiskEngine().evaluate(plan, _state(realized_loss_today=over), _settings())
    assert (blocked.approved, blocked.reason_code) == (False, "DAILY_LOSS_CAP_WOULD_BREACH")

    with_open = RiskEngine().evaluate(
        plan,
        _state(
            realized_loss_today=room - Decimal("10"),
            open_exposures=(_exposure("10.01"),),
            open_position_count=1,
        ),
        _settings(),
    )
    assert with_open.reason_code == "DAILY_LOSS_CAP_WOULD_BREACH"


def test_rolling_loss_gate_counts_pending_reservation_risk() -> None:
    plan = _plan()
    state = _state(
        realized_loss_rolling=Decimal("500") - plan.estimated_loss,
        pending_reservation_risk=Decimal("0.01"),
        pending_reservation_count=1,
    )
    result = RiskEngine().evaluate(plan, state, _settings())
    assert result.reason_code == "ROLLING_LOSS_CAP_WOULD_BREACH"


def test_tripped_drawdown_breaker_blocks_new_entry() -> None:
    result = RiskEngine().evaluate(_plan(), _state(drawdown_breaker_tripped=True), _settings())
    assert (result.approved, result.reason_code) == (False, "CIRCUIT_BREAKER_DRAWDOWN")
    assert "tiếp tục bot" in result.detail_vi


def _trades(*pnls: str) -> list[ClosedTradePnl]:
    return [ClosedTradePnl(T0 + timedelta(hours=i), Decimal(p)) for i, p in enumerate(pnls)]


def test_drawdown_measures_from_running_peak_including_unrealized_equity() -> None:
    state = evaluate_drawdown_breaker(
        seed_equity=Decimal("10000"),
        closed_trades=_trades("500", "-200"),
        current_equity=Decimal("9450"),  # đỉnh 10.500 → sụt 10%
        baseline_at=None,
    )
    assert state.peak_equity == Decimal("10500")
    assert state.drawdown_fraction == Decimal("0.1")
    assert state.tripped is True
    assert PAPER_DRAWDOWN_BREAKER_POLICY.max_drawdown_fraction == Decimal("0.10")


def test_drawdown_just_below_threshold_does_not_trip_and_gain_is_zero() -> None:
    below = evaluate_drawdown_breaker(
        seed_equity=Decimal("10000"),
        closed_trades=[],
        current_equity=Decimal("9000.01"),
        baseline_at=None,
    )
    assert below.tripped is False
    gain = evaluate_drawdown_breaker(
        seed_equity=Decimal("10000"),
        closed_trades=_trades("100"),
        current_equity=Decimal("10200"),
        baseline_at=None,
    )
    assert gain.drawdown_fraction == 0


def test_resume_baseline_resets_peak_to_equity_at_resume() -> None:
    trades = _trades("1000", "-1500", "100")  # đỉnh cũ 11.000, sau 2 lệnh còn 9.500
    before = evaluate_drawdown_breaker(
        seed_equity=Decimal("10000"),
        closed_trades=trades,
        current_equity=Decimal("9600"),
        baseline_at=None,
    )
    assert before.tripped is True
    after = evaluate_drawdown_breaker(
        seed_equity=Decimal("10000"),
        closed_trades=trades,
        current_equity=Decimal("9600"),
        baseline_at=T0 + timedelta(hours=1, minutes=30),  # tiếp tục sau lệnh thứ hai
    )
    assert after.peak_equity == Decimal("9600")
    assert after.tripped is False
    assert (
        after.evidence()["drawdown_baseline_at"]
        == (T0 + timedelta(hours=1, minutes=30)).isoformat()
    )


def test_resume_without_later_trades_uses_realized_equity_as_peak() -> None:
    state = evaluate_drawdown_breaker(
        seed_equity=Decimal("10000"),
        closed_trades=_trades("-500"),
        current_equity=Decimal("9400"),
        baseline_at=T0 + timedelta(days=1),
    )
    assert state.peak_equity == Decimal("9500")
    assert state.tripped is False


@pytest.mark.parametrize("fraction", ["0", "1", "-0.1"])
def test_policy_rejects_invalid_threshold(fraction: str) -> None:
    with pytest.raises(ValueError, match="drawdown"):
        DrawdownBreakerPolicy(max_drawdown_fraction=Decimal(fraction))


def test_invalid_seed_is_rejected() -> None:
    with pytest.raises(ValueError, match="seed"):
        evaluate_drawdown_breaker(
            seed_equity=Decimal("0"),
            closed_trades=[],
            current_equity=Decimal("1"),
            baseline_at=None,
        )
