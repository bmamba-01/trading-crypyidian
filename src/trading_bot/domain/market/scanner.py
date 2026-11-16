"""Bộ quét thị trường (CK-11, giai đoạn 1) trên nến đã đóng trong kho nội bộ — không gọi mạng.

Mỗi mã dùng một khung "ngày" (1d; nếu không có thì 4h hoặc 1h đã lưu). Chỉ số:

- ``change_7d_pct`` = đóng cửa mới nhất / đóng cửa của nến đã đóng tại mốc 7 ngày trước − 1;
- ``quote_volume_24h`` = Σ ``quote_volume`` của nến trong 24 giờ cuối (thiếu thì khối lượng ×
  giá đóng, gắn cờ ước tính);
- ``range_21d_pct`` = (đỉnh cao nhất − đáy thấp nhất trong 21 ngày) / giá đóng mới nhất.

Loại mã kèm lý do: thiếu dữ liệu 21 ngày, dữ liệu cũ (nến cuối đóng quá 2 khung trước ``now``),
thanh khoản dưới ngưỡng. Kết quả tất định: sắp theo chỉ số rồi theo tên mã.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

SCANNER_VERSION = "ck11-scanner-v1"
SCAN_INTERVALS = {"1h": 3600, "4h": 14400, "1d": 86400}
TOP_N = 10


@dataclass(frozen=True, slots=True)
class ScanCriteria:
    """Tiêu chí quét; giá trị % là phần trăm (10 = 10%)."""

    min_quote_volume_24h: float = 1_000_000.0
    range_min_pct: float = 0.0
    range_max_pct: float = 1_000.0

    def as_dict(self) -> dict[str, float]:
        return {
            "min_quote_volume_24h": self.min_quote_volume_24h,
            "range_min_pct": self.range_min_pct,
            "range_max_pct": self.range_max_pct,
        }


def _metrics(
    bars: Sequence[Mapping[str, Any]], *, interval: int, now: datetime
) -> tuple[dict[str, Any] | None, str | None]:
    if not bars:
        return None, "Thiếu dữ liệu: không có nến đã đóng."
    now_epoch = int(now.timestamp())
    last = bars[-1]
    last_close_at = int(last["time"]) + interval
    if now_epoch - last_close_at > 2 * interval:
        hours = (now_epoch - last_close_at) // 3600
        return None, f"Dữ liệu cũ: nến cuối đóng cách đây {hours} giờ."
    first_needed = last_close_at - 21 * 86400
    if int(bars[0]["time"]) > first_needed:
        return None, "Thiếu dữ liệu 21 ngày để tính biên và thay đổi tuần."
    close = float(last["close"])
    week_ago = last_close_at - 7 * 86400
    # Giá đóng của nến cuối cùng đã đóng tại hoặc trước mốc 7 ngày trước.
    reference = max(
        (bar for bar in bars if int(bar["time"]) + interval <= week_ago),
        key=lambda bar: int(bar["time"]),
    )
    window = [bar for bar in bars if int(bar["time"]) >= first_needed]
    day = [bar for bar in bars if int(bar["time"]) + interval > last_close_at - 86400]
    estimated = any(bar.get("quote_volume") is None for bar in day)
    quote = sum(
        float(bar["quote_volume"])
        if bar.get("quote_volume") is not None
        else float(bar["volume"]) * float(bar["close"])
        for bar in day
    )
    high = max(float(bar["high"]) for bar in window)
    low = min(float(bar["low"]) for bar in window)
    return (
        {
            "close": close,
            "change_7d_pct": (close / float(reference["close"]) - 1) * 100,
            "quote_volume_24h": quote,
            "quote_volume_estimated": estimated,
            "range_21d_pct": (high - low) / close * 100 if close else None,
            "high_21d": high,
            "low_21d": low,
            "last_close_at": last_close_at,
        },
        None,
    )


def scan_markets(
    series: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    timeframe: str,
    now: datetime,
    criteria: ScanCriteria,
) -> dict[str, Any]:
    """Quét các mã; ``series`` là nến đã đóng (tăng dần) của khung ``timeframe`` theo từng mã."""
    if timeframe not in SCAN_INTERVALS:
        raise ValueError("Bộ quét chỉ dùng khung 1h, 4h hoặc 1d")
    if now.tzinfo is None:
        raise ValueError("now phải có timezone")
    interval = SCAN_INTERVALS[timeframe]
    rows: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for symbol in sorted(series):
        metrics, reason = _metrics(series[symbol], interval=interval, now=now)
        if metrics is None:
            excluded.append({"symbol": symbol, "reason": reason or "Không rõ lý do."})
            continue
        if metrics["quote_volume_24h"] < criteria.min_quote_volume_24h:
            excluded.append(
                {
                    "symbol": symbol,
                    "reason": (
                        f"Dưới ngưỡng thanh khoản: {metrics['quote_volume_24h']:,.0f} "
                        f"< {criteria.min_quote_volume_24h:,.0f} (quote 24h)."
                    ),
                }
            )
            continue
        rows.append({"symbol": symbol, **metrics})
    movers = sorted(rows, key=lambda row: (-abs(row["change_7d_pct"]), row["symbol"]))[:TOP_N]
    ranged = sorted(
        (
            row
            for row in rows
            if row["range_21d_pct"] is not None
            and criteria.range_min_pct <= row["range_21d_pct"] <= criteria.range_max_pct
        ),
        key=lambda row: (-row["range_21d_pct"], row["symbol"]),
    )[:TOP_N]
    volume = sorted(rows, key=lambda row: (-row["quote_volume_24h"], row["symbol"]))[:TOP_N]
    return {
        "formula_version": SCANNER_VERSION,
        "timeframe": timeframe,
        "criteria": criteria.as_dict(),
        "universe": len(series),
        "passed": len(rows),
        "movers_7d": movers,
        "top_range_21d": ranged,
        "top_volume": volume,
        "rows": sorted(rows, key=lambda row: row["symbol"]),
        "excluded": excluded,
    }
