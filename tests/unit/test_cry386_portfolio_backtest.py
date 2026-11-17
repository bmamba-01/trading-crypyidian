"""CRY-386: backtest danh mục nhiều coin (trần vị thế, trần đòn bẩy, vốn chung)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.application.proposal_backtest import (
    STARTING_EQUITY,
    SymbolResult,
    limits_for_proposal,
    portfolio_limits,
    run_portfolio,
)
from trading_bot.domain.portfolio.portfolio_backtest import (
    PortfolioLimits,
    PortfolioTrade,
    simulate_portfolio,
    trades_with_symbol_equity,
)

T0 = datetime(2026, 11, 1, tzinfo=UTC)
H = timedelta(hours=1)
E = Decimal("10000")


def _t(symbol: str, start: int, end: int, pnl: str, exposure: str = "5000") -> PortfolioTrade:
    return PortfolioTrade(symbol, T0 + start * H, T0 + end * H, Decimal(pnl), Decimal(exposure), E)


def test_max_positions_skips_overlapping_entries_and_frees_slot_on_close() -> None:
    trades = [_t("A", 0, 5, "100"), _t("B", 1, 3, "-50"), _t("C", 2, 4, "80"), _t("D", 5, 6, "10")]
    one = simulate_portfolio(trades, starting_equity=E, limits=PortfolioLimits(1, Decimal(5)))
    # A mở → B, C bị bỏ; A đóng lúc 5 trước khi D mở cùng mốc → D được vào.
    assert (one.taken, one.skipped_positions) == (2, 2)
    assert one.net_pnl > 0
    two = simulate_portfolio(trades, starting_equity=E, limits=PortfolioLimits(2, Decimal(5)))
    assert (two.taken, two.skipped_positions, two.peak_open_positions) == (3, 1, 2)


def test_leverage_caps_total_open_notional() -> None:
    trades = [_t("A", 0, 4, "10", "8000"), _t("B", 1, 3, "10", "8000")]
    tight = simulate_portfolio(trades, starting_equity=E, limits=PortfolioLimits(5, Decimal(1)))
    assert (tight.taken, tight.skipped_leverage) == (1, 1)
    loose = simulate_portfolio(trades, starting_equity=E, limits=PortfolioLimits(5, Decimal(2)))
    assert (loose.taken, loose.skipped_leverage) == (2, 0)


def test_size_scales_with_shared_equity_and_drawdown_is_realized() -> None:
    # Sau lệnh lỗ 1000, vốn 9000: lệnh kế phóng 0.9 lần so với vốn coin 10000.
    trades = [_t("A", 0, 1, "-1000"), _t("B", 2, 3, "500")]
    result = simulate_portfolio(trades, starting_equity=E, limits=PortfolioLimits(2, Decimal(2)))
    assert result.net_pnl == Decimal("-1000") + Decimal("500") * Decimal("0.9")
    assert result.max_drawdown == Decimal("1000")
    assert (result.gross_loss, result.wins) == (Decimal("1000"), 1)


def test_symbol_equity_tracks_previous_closed_trades() -> None:
    trades = trades_with_symbol_equity(
        "A",
        [(T0 + 3 * H, T0 + 4 * H, Decimal("50"), Decimal("1")),
         (T0, T0 + H, Decimal("-200"), Decimal("1"))],
        E,
    )  # fmt: skip
    assert [t.symbol_equity_at_entry for t in trades] == [E, E - 200]
    with pytest.raises(ValueError, match="đóng trước"):
        _t("A", 3, 1, "0")
    with pytest.raises(ValueError, match="Trần"):
        PortfolioLimits(0, Decimal(1))


def test_limits_from_config_and_proposal() -> None:
    assert portfolio_limits({}) == PortfolioLimits(1, Decimal(1))
    base, variant = limits_for_proposal(
        {"risk": {"max_positions": 1, "max_leverage": "5"}},
        [{"field": "risk.max_positions", "to": "3"}],
    )
    assert base == PortfolioLimits(1, Decimal(5))
    assert variant == PortfolioLimits(3, Decimal(5))


def test_run_portfolio_merges_symbol_results() -> None:
    def result(symbol: str, *trades: PortfolioTrade) -> SymbolResult:
        return SymbolResult(symbol, len(trades), 0, Decimal(0), Decimal(0), Decimal(0),
                            Decimal(0), portfolio_trades=trades)  # fmt: skip

    merged = run_portfolio(
        [result("A", _t("A", 0, 2, "100")), result("B", _t("B", 1, 3, "100"))],
        PortfolioLimits(1, Decimal(5)),
    )
    assert (merged.taken, merged.skipped_positions, merged.symbols) == (1, 1, 1)
    assert STARTING_EQUITY == E
