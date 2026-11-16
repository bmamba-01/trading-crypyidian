"""CK-10: thống kê lệnh đã đóng của mã đang xem — tỷ lệ thắng so với tỷ lệ hòa vốn, kỳ vọng."""

from __future__ import annotations

import pytest

from trading_bot.domain.portfolio.setup_statistics import STATISTICS_VERSION, trade_statistics


def _trade(net: str, *, release: str = "h01-v1", r: float | None = None) -> dict[str, object]:
    return {"net_pnl": net, "quote_asset": "USDT", "strategy_release_id": release, "r_multiple": r}


def test_win_rate_breakeven_and_expectancy_after_fees() -> None:
    trades = [_trade("30"), _trade("-10"), _trade("-10"), _trade("20"), _trade("-10")]
    stats = trade_statistics(trades)
    assert STATISTICS_VERSION == "ck10-stats-v1"
    assert stats["n"] == 5
    assert stats["win_rate"] == pytest.approx(0.4)
    assert stats["avg_win"] == pytest.approx(25.0)
    assert stats["avg_loss"] == pytest.approx(10.0)
    # Hòa vốn khi p × 25 = (1 − p) × 10 → p = 10/35.
    assert stats["breakeven_win_rate"] == pytest.approx(10 / 35)
    assert stats["expectancy"] == pytest.approx(4.0)
    assert stats["profit_factor"] == pytest.approx(50 / 30)
    assert stats["edge"] == "above_breakeven"
    assert stats["sample_warning"] is True  # < 30 lệnh: chưa đủ mẫu


def test_r_distribution_when_available_and_empty_input() -> None:
    trades = [_trade("5", r=1.2), _trade("-4", r=-1.0), _trade("-2", r=-0.4), _trade("1")]
    stats = trade_statistics(trades)
    assert stats["r_count"] == 3
    assert stats["r_buckets"] == {"≤ −1R": 1, "−1R…0": 1, "0…1R": 0, "1R…2R": 1, "≥ 2R": 0}
    empty = trade_statistics([])
    assert empty["n"] == 0
    assert empty["win_rate"] is None
    assert empty["edge"] == "no_data"


def test_mixed_quote_assets_are_rejected() -> None:
    with pytest.raises(ValueError, match="đồng định giá"):
        trade_statistics([_trade("1"), {"net_pnl": "1", "quote_asset": "USDC"}])
