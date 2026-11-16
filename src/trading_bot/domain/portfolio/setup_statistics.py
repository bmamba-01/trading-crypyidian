"""Thống kê lệnh PAPER đã đóng cho cột phải chart (CK-10), từ số liệu ledger của báo cáo.

- Thắng = PnL ròng (sau phí, trượt giá, funding) > 0; hòa (= 0) tính là không thắng.
- Tỷ lệ hòa vốn = lỗ TB / (lãi TB + lỗ TB): tỷ lệ thắng tối thiểu để kỳ vọng ≥ 0 với cặp lãi/lỗ
  trung bình hiện tại. Kỳ vọng = PnL ròng trung bình mỗi lệnh.
- Dưới 30 lệnh thì gắn cờ "chưa đủ mẫu"; không suy ra hiệu quả chiến lược từ mẫu nhỏ.
- Phân phối R chỉ dùng lệnh có ``r_multiple`` (cần rủi ro ban đầu của kế hoạch).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

STATISTICS_VERSION = "ck10-stats-v1"
MIN_SAMPLE = 30
R_BUCKETS = ("≤ −1R", "−1R…0", "0…1R", "1R…2R", "≥ 2R")


def _bucket(r: float) -> str:
    if r <= -1:
        return R_BUCKETS[0]
    if r < 0:
        return R_BUCKETS[1]
    if r < 1:
        return R_BUCKETS[2]
    if r < 2:
        return R_BUCKETS[3]
    return R_BUCKETS[4]


def trade_statistics(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Thống kê một nhóm lệnh cùng đồng định giá; không làm tròn số ledger."""
    assets = {str(trade.get("quote_asset")) for trade in trades}
    if len(assets) > 1:
        raise ValueError("Không gộp lệnh khác đồng định giá trong một thống kê")
    pnls = [Decimal(str(trade["net_pnl"])) for trade in trades]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    n = len(pnls)
    avg_win = float(sum(wins) / len(wins)) if wins else None
    avg_loss = float(sum(losses) / len(losses)) if losses else None
    win_rate = len(wins) / n if n else None
    breakeven = (
        avg_loss / (avg_win + avg_loss)
        if avg_win is not None and avg_loss is not None and avg_win + avg_loss > 0
        else None
    )
    if n == 0:
        edge = "no_data"
    elif breakeven is None or win_rate is None:
        edge = "above_breakeven" if not losses else "below_breakeven"
    else:
        edge = "above_breakeven" if win_rate > breakeven else "below_breakeven"
    r_values = [
        float(trade["r_multiple"]) for trade in trades if trade.get("r_multiple") is not None
    ]
    buckets = dict.fromkeys(R_BUCKETS, 0)
    for value in r_values:
        buckets[_bucket(value)] += 1
    return {
        "formula_version": STATISTICS_VERSION,
        "quote_asset": next(iter(assets)) if assets else None,
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "breakeven_win_rate": breakeven,
        "expectancy": float(sum(pnls) / n) if n else None,
        "profit_factor": float(sum(wins) / sum(losses)) if losses and wins else None,
        "total_net": float(sum(pnls)) if n else None,
        "edge": edge,
        "sample_warning": n < MIN_SAMPLE,
        "r_count": len(r_values),
        "r_buckets": buckets,
    }
