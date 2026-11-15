"""Hồ sơ giá theo thời gian (TPO — Time Price Opportunity) và mức "trần" chưa bị chạm lại (CK-4).

- Chia thời gian thành **khối 30 phút** neo theo mốc UTC; mỗi khối là một TPO cho *mỗi hàng giá*
  mà biên [low, high] của khối chạm tới (giao mở: chạm đúng cạnh hàng không tính). Nến nhỏ hơn
  30 phút được gộp theo khối; nến lớn hơn 30 phút không dựng được TPO.
- POC TPO = hàng nhiều TPO nhất (hòa → hàng gần giữa biên nhất, rồi hàng thấp hơn); vùng giá
  trị = mở rộng từ POC về phía hàng nhiều TPO hơn tới khi đủ 70% tổng TPO (giống volume profile).
- POC TPO có thể khác POC khối lượng: một tính *thời gian giá ở lại*, một tính *khối lượng*.
- Mức "trần" (naked): mức của phiên đã đóng mà giá chưa quay lại chạm; biến mất ở nến đầu tiên
  có râu phủ mức, tính từ nến mức có hiệu lực.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

TPO_FORMULA_VERSION = "ck4-tpo-v1"
TPO_BLOCK = timedelta(minutes=30)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class TpoRow:
    price_low: float
    price_high: float
    count: int


@dataclass(frozen=True, slots=True)
class TpoProfile:
    rows: tuple[TpoRow, ...]
    poc_price: float
    value_area_high: float
    value_area_low: float
    total_tpo: int
    block_count: int
    row_size: float
    formula_version: str = TPO_FORMULA_VERSION


def tpo_profile(
    open_times: Sequence[datetime],
    highs: Sequence[float],
    lows: Sequence[float],
    interval: timedelta,
    *,
    row_size: float | None = None,
    rows: int = 24,
    value_area: float = 0.70,
) -> TpoProfile | None:
    """TPO profile trên các nến truyền vào (một phiên/ngày); ``None`` khi không có nến."""
    if not len(open_times) == len(highs) == len(lows):
        raise ValueError("Các chuỗi nến phải có cùng độ dài")
    if interval > TPO_BLOCK:
        raise ValueError("TPO cần nến từ 30 phút trở xuống")
    if not 0 < value_area <= 1:
        raise ValueError("value_area phải trong (0, 1]")
    if not open_times:
        return None
    if any(moment.tzinfo is None for moment in open_times):
        raise ValueError("Thời điểm mở nến phải có timezone")

    blocks: dict[int, list[float]] = {}
    for moment, high, low in zip(open_times, highs, lows, strict=True):
        if low > high:
            raise ValueError("Nến không hợp lệ")
        key = (moment - _EPOCH) // TPO_BLOCK
        block = blocks.setdefault(key, [low, high])
        block[0] = min(block[0], low)
        block[1] = max(block[1], high)

    bottom_price = min(lows)
    top_price = max(highs)
    if row_size is None:
        span = top_price - bottom_price
        row_size = span / rows if span > 0 else max(abs(top_price) * 1e-6, 1e-9)
    if row_size <= 0:
        raise ValueError("row_size phải dương")
    start = math.floor(bottom_price / row_size) * row_size
    count = max(1, math.ceil((top_price - start) / row_size - 1e-12))
    counts = [0] * count
    for low, high in blocks.values():
        for index in range(count):
            row_low = start + index * row_size
            row_high = row_low + row_size
            touches = (low < row_high and high > row_low) or (
                low == high and row_low <= low < row_high
            )
            if touches:
                counts[index] += 1

    total = sum(counts)
    middle = (bottom_price + top_price) / 2
    poc = max(
        range(count),
        key=lambda i: (counts[i], -abs(start + (i + 0.5) * row_size - middle), -i),
    )
    included = {poc}
    below, above = poc - 1, poc + 1
    covered = counts[poc]
    while covered < value_area * total and (below >= 0 or above < count):
        below_count = counts[below] if below >= 0 else -1
        above_count = counts[above] if above < count else -1
        if above_count >= below_count:
            included.add(above)
            covered += above_count
            above += 1
        else:
            included.add(below)
            covered += below_count
            below -= 1
    return TpoProfile(
        rows=tuple(
            TpoRow(start + i * row_size, start + (i + 1) * row_size, counts[i])
            for i in range(count)
        ),
        poc_price=start + (poc + 0.5) * row_size,
        value_area_high=start + (max(included) + 1) * row_size,
        value_area_low=start + min(included) * row_size,
        total_tpo=total,
        block_count=len(blocks),
        row_size=row_size,
    )


def naked_levels(
    levels: Sequence[tuple[float, int]],
    highs: Sequence[float],
    lows: Sequence[float],
) -> tuple[int | None, ...]:
    """Với mỗi ``(giá, nến bắt đầu hiệu lực)`` trả nến đầu tiên chạm mức, ``None`` nếu còn trần."""
    if len(highs) != len(lows):
        raise ValueError("Các chuỗi nến phải có cùng độ dài")
    return tuple(
        next(
            (i for i in range(max(armed, 0), len(highs)) if lows[i] <= price <= highs[i]),
            None,
        )
        for price, armed in levels
    )
