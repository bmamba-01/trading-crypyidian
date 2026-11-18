"""Chỉ số phân tích hiệu suất chuyên nghiệp MT-1 (hàm thuần, tất định).

Quy ước ``mt1-analytics-v1``:
- **Equity MTM theo nến**: ``equity_t = initial_capital + realized_pnl_t + unrealized_pnl_t``;
  ``drawdown_pct_t = max(0, (peak_t - equity_t) / peak_t × 100)``.
- **Lợi suất ngày**: lấy equity cuối mỗi ngày (theo ``tz_name``),
  ``r_d = (E_d - E_{d-1}) / E_{d-1}``.
- **Sharpe / Sortino / Calmar**: chuẩn hóa năm với ``periods_per_year = 365`` (crypto 24/7).
  Trả ``None`` khi mẫu ``< 2`` ngày hoặc mẫu số bằng 0 (fail-closed, không bịa số).
- **Phân phối R**: 6 bucket chuẩn ``<=-2R``, ``(-2R,-1R]``, ``(-1R,0R)``, ``[0R,1R)``,
  ``[1R,2R)``, ``>=2R`` kèm tứ phân vị ``p25``, ``median``, ``p75``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

MT1_ANALYTICS_VERSION = "mt1-analytics-v1"
CRYPTO_DAYS_PER_YEAR = 365
_Q = Decimal("0.0001")

R_HISTOGRAM_BUCKETS: tuple[tuple[str, Decimal | None, Decimal | None, bool, bool], ...] = (
    ("<=-2R", None, Decimal("-2"), False, True),
    ("(-2R,-1R]", Decimal("-2"), Decimal("-1"), False, True),
    ("(-1R,0R)", Decimal("-1"), Decimal("0"), False, False),
    ("[0R,1R)", Decimal("0"), Decimal("1"), True, False),
    ("[1R,2R)", Decimal("1"), Decimal("2"), True, False),
    (">=2R", Decimal("2"), None, True, False),
)


@dataclass(frozen=True, slots=True)
class MTMEquityBar:
    """Đầu vào MTM tại một nến: PnL đã thực hiện lũy kế và PnL chưa thực hiện."""

    time: datetime
    realized_pnl_cumulative: Decimal
    unrealized_pnl: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class EquityBarPoint:
    """Một điểm trên đường vốn mark-to-market theo nến."""

    time: datetime
    equity: Decimal
    peak_equity: Decimal
    drawdown_abs: Decimal
    drawdown_pct: Decimal


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(_Q).normalize(), "f")


def mtm_equity_curve(
    initial_capital: Decimal, bars: Sequence[MTMEquityBar]
) -> list[EquityBarPoint]:
    """Dựng đường vốn MTM theo nến và drawdown % từ đỉnh lũy kế."""
    if initial_capital <= 0:
        raise ValueError("Vốn ban đầu phải dương.")
    ordered = sorted(bars, key=lambda bar: bar.time)
    peak = initial_capital
    points: list[EquityBarPoint] = []
    for bar in ordered:
        equity = initial_capital + bar.realized_pnl_cumulative + bar.unrealized_pnl
        if equity > peak:
            peak = equity
        dd_abs = max(Decimal("0"), peak - equity)
        dd_pct = (dd_abs / peak * Decimal("100")) if peak > 0 else Decimal("0")
        points.append(
            EquityBarPoint(
                time=bar.time,
                equity=equity,
                peak_equity=peak,
                drawdown_abs=dd_abs,
                drawdown_pct=dd_pct,
            )
        )
    return points


def max_drawdown_pct(points: Sequence[EquityBarPoint]) -> Decimal | None:
    """Drawdown % lớn nhất trên đường vốn MTM."""
    if not points:
        return None
    return max(point.drawdown_pct for point in points)


def daily_returns(points: Sequence[EquityBarPoint], *, tz_name: str = "UTC") -> list[Decimal]:
    """Lợi suất ngày ``(E_d - E_{d-1}) / E_{d-1}`` từ equity cuối mỗi ngày."""
    if len(points) < 2:
        return []
    tz = ZoneInfo(tz_name)
    by_day: dict[str, Decimal] = {}
    for point in sorted(points, key=lambda p: p.time):
        day_key = point.time.astimezone(tz).date().isoformat()
        by_day[day_key] = point.equity
    equities = [by_day[day] for day in sorted(by_day)]
    returns: list[Decimal] = []
    for prev, curr in zip(equities, equities[1:], strict=False):
        if prev > 0:
            returns.append((curr - prev) / prev)
    return returns


def sharpe_ratio(
    returns: Sequence[Decimal],
    *,
    risk_free_daily: Decimal = Decimal("0"),
    periods_per_year: int = CRYPTO_DAYS_PER_YEAR,
) -> Decimal | None:
    """Sharpe chuẩn hóa năm từ chuỗi lợi suất ngày (``ddof=1``)."""
    n = len(returns)
    if n < 2 or periods_per_year <= 0:
        return None
    excess = [float(r - risk_free_daily) for r in returns]
    mean_ex = sum(excess) / n
    var = sum((x - mean_ex) ** 2 for x in excess) / (n - 1)
    std = math.sqrt(var)
    if std <= 0:
        return None
    value = (mean_ex / std) * math.sqrt(periods_per_year)
    return Decimal(str(value)).quantize(_Q)


def sortino_ratio(
    returns: Sequence[Decimal],
    *,
    mar_daily: Decimal = Decimal("0"),
    periods_per_year: int = CRYPTO_DAYS_PER_YEAR,
) -> Decimal | None:
    """Sortino chuẩn hóa năm: chỉ phạt độ lệch âm dưới ``mar_daily``."""
    n = len(returns)
    if n < 2 or periods_per_year <= 0:
        return None
    excess = [float(r - mar_daily) for r in returns]
    mean_ex = sum(excess) / n
    downside_sq = [min(0.0, x) ** 2 for x in excess]
    downside_dev = math.sqrt(sum(downside_sq) / n)
    if downside_dev <= 0:
        return None
    value = (mean_ex / downside_dev) * math.sqrt(periods_per_year)
    return Decimal(str(value)).quantize(_Q)


def calmar_ratio(
    returns: Sequence[Decimal],
    *,
    max_dd_pct: Decimal | None,
    periods_per_year: int = CRYPTO_DAYS_PER_YEAR,
) -> Decimal | None:
    """Calmar = lợi suất năm hóa (%) / Max Drawdown (%)."""
    if len(returns) < 2 or max_dd_pct is None or max_dd_pct <= 0 or periods_per_year <= 0:
        return None
    mean_daily = sum(float(r) for r in returns) / len(returns)
    annual_pct = mean_daily * periods_per_year * 100.0
    return Decimal(str(annual_pct / float(max_dd_pct))).quantize(_Q)


def _quantile(sorted_vals: Sequence[Decimal], q: float) -> Decimal:
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    weight = Decimal(str(pos - lo))
    return sorted_vals[lo] * (Decimal("1") - weight) + sorted_vals[hi] * weight


def r_multiple_distribution(r_values: Sequence[Decimal]) -> dict[str, Any]:
    """Phân phối R-multiple: 6 bucket chuẩn + thống kê mô tả."""
    buckets = {label: 0 for label, *_ in R_HISTOGRAM_BUCKETS}
    for r in r_values:
        for label, lo, hi, inc_lo, inc_hi in R_HISTOGRAM_BUCKETS:
            lo_ok = True if lo is None else (r >= lo if inc_lo else r > lo)
            hi_ok = True if hi is None else (r <= hi if inc_hi else r < hi)
            if lo_ok and hi_ok:
                buckets[label] += 1
                break
    if not r_values:
        return {
            "count": 0,
            "buckets": buckets,
            "mean": None,
            "median": None,
            "p25": None,
            "p75": None,
            "min": None,
            "max": None,
        }
    ordered = sorted(r_values)
    return {
        "count": len(ordered),
        "buckets": buckets,
        "mean": _text(sum(ordered, Decimal("0")) / Decimal(len(ordered))),
        "median": _text(Decimal(str(median(ordered)))),
        "p25": _text(_quantile(ordered, 0.25)),
        "p75": _text(_quantile(ordered, 0.75)),
        "min": _text(ordered[0]),
        "max": _text(ordered[-1]),
    }


def max_consecutive_losses(pnls: Sequence[Decimal]) -> int:
    """Chuỗi lệnh thua liên tiếp dài nhất (``pnl < 0``)."""
    best = 0
    current = 0
    for pnl in pnls:
        if pnl < 0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def cost_to_gross_profit_ratio(total_cost: Decimal, gross_profit: Decimal) -> Decimal | None:
    """Tỷ lệ tổng chi phí / lãi gộp (chỉ tính khi ``gross_profit > 0``)."""
    if gross_profit <= 0:
        return None
    return (total_cost / gross_profit).quantize(_Q)


def slippage_vs_plan_ratio(actual_slippage: Decimal, planned_slippage: Decimal) -> Decimal | None:
    """Tỷ lệ trượt giá thực tế / trượt giá kế hoạch (``> 1`` là tệ hơn kế hoạch)."""
    if planned_slippage <= 0:
        return None
    return (actual_slippage / planned_slippage).quantize(_Q)
