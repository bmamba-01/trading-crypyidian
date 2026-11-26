"""Thống kê lệnh PAPER đã đóng cho cột phải chart (CK-10), từ số liệu ledger của báo cáo.

- Thắng = PnL ròng (sau phí, trượt giá, funding) > 0; hòa (= 0) tính là không thắng.
- Tỷ lệ hòa vốn = lỗ TB / (lãi TB + lỗ TB): tỷ lệ thắng tối thiểu để kỳ vọng ≥ 0 với cặp lãi/lỗ
  trung bình hiện tại. Kỳ vọng = PnL ròng trung bình mỗi lệnh.
- Dưới 30 lệnh thì gắn cờ "chưa đủ mẫu"; không suy ra hiệu quả chiến lược từ mẫu nhỏ.
- Phân phối R tính theo ``1R = planned_risk`` (rủi ro kế hoạch tại SL từ ``TradePlanRecord``);
  fail-closed (``None``) khi thiếu hoặc ``planned_risk <= 0``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

STATISTICS_VERSION = "ck10-stats-v1"
MIN_SAMPLE = 30
R_BUCKETS = ("≤ −1R", "−1R…0", "0…1R", "1R…2R", "≥ 2R")
_DEFAULT_SETUP_LABEL = "Chưa phân loại"


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


def _resolve_r_multiple(trade: Mapping[str, Any], net_pnl: Decimal) -> float | None:
    """Trả bội số R (``net_pnl / planned_risk``); fail-closed ``None`` khi thiếu rủi ro kế hoạch."""
    explicit = trade.get("r_multiple")
    if explicit is not None:
        try:
            value = float(explicit)
        except TypeError, ValueError:
            return None
        return value if math.isfinite(value) else None

    for key in ("planned_risk", "estimated_loss_at_stop", "estimated_loss"):
        raw_risk = trade.get(key)
        if raw_risk is None or raw_risk == "":
            continue
        try:
            risk = Decimal(str(raw_risk))
        except InvalidOperation:
            return None
        if not risk.is_finite() or risk <= 0:
            return None
        ratio = float(net_pnl / risk)
        return ratio if math.isfinite(ratio) else None
    return None


def _derive_setup_key(trade: Mapping[str, Any]) -> str:
    """Chuẩn hóa nhãn setup từ ``setup_type`` / ``setup`` / ``strategy_id`` / release."""
    for key in ("setup_type", "setup", "strategy_id"):
        raw = trade.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    release = trade.get("strategy_release_id")
    if isinstance(release, str) and release.strip():
        token = release.strip()
        prefix = token.split("-", 1)[0].split(":", 1)[0].strip()
        if prefix and len(prefix) <= 12 and prefix.isalnum():
            return prefix.upper()
        return token
    return _DEFAULT_SETUP_LABEL


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Nội suy tuyến tính phân vị ``q`` trong ``[0, 1]`` trên danh sách đã sắp tăng dần."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return sorted_values[low]
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (pos - low)


def _r_quantiles(r_values: Sequence[float]) -> dict[str, float] | None:
    if not r_values:
        return None
    ordered = sorted(r_values)
    return {
        "p25": _percentile(ordered, 0.25),
        "p50": _percentile(ordered, 0.50),
        "p75": _percentile(ordered, 0.75),
    }


def _summarize_group(
    trades: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[float]]:
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

    r_values: list[float] = []
    for trade, pnl in zip(trades, pnls, strict=True):
        resolved = _resolve_r_multiple(trade, pnl)
        if resolved is not None:
            r_values.append(resolved)
    buckets = dict.fromkeys(R_BUCKETS, 0)
    for value in r_values:
        buckets[_bucket(value)] += 1

    summary = {
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
        "r_expectancy": (sum(r_values) / len(r_values)) if r_values else None,
        "r_quantiles": _r_quantiles(r_values),
        "r_buckets": buckets,
    }
    return summary, r_values


def trade_statistics(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Thống kê một nhóm lệnh cùng đồng định giá; tách thêm theo setup và phân phối R."""
    assets = {str(trade.get("quote_asset")) for trade in trades}
    if len(assets) > 1:
        raise ValueError("Không gộp lệnh khác đồng định giá trong một thống kê")
    summary, _ = _summarize_group(trades)

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for trade in trades:
        grouped.setdefault(_derive_setup_key(trade), []).append(trade)
    by_setup: list[dict[str, Any]] = []
    for setup_label, group_trades in grouped.items():
        group_summary, _ = _summarize_group(group_trades)
        by_setup.append({"setup": setup_label, **group_summary})
    by_setup.sort(key=lambda row: (-int(row["n"]), str(row["setup"])))

    return {
        "formula_version": STATISTICS_VERSION,
        "quote_asset": next(iter(assets)) if assets else None,
        **summary,
        "by_setup": by_setup,
    }
