"""Các phép tính hình học đồ thị chỉ dùng dữ liệu nến đã đóng."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal


class PatternKind(StrEnum):
    """Loại mẫu hình đã được xác nhận bằng giá đóng cửa."""

    DOUBLE_TOP = "double_top"
    DOUBLE_BOTTOM = "double_bottom"


@dataclass(frozen=True, slots=True)
class Pivot:
    """Điểm xoay chiều và vị trí sớm nhất dữ liệu có thể xác nhận điểm đó."""

    kind: Literal["high", "low"]
    index: int
    price: Decimal
    confirmed_at_index: int


@dataclass(frozen=True, slots=True)
class PatternCandidate:
    """Mẫu hai đỉnh/đáy đã phá ngưỡng; đây là candidate, chưa phải lệnh."""

    kind: PatternKind
    direction: Literal["long", "short"]
    first_swing_index: int
    second_swing_index: int
    neckline: Decimal
    confirmation_index: int
    invalidation_price: Decimal
    geometry_version: str = "double-pattern-v1"


def find_confirmed_pivots(
    highs: Sequence[Decimal],
    lows: Sequence[Decimal],
    *,
    left_bars: int,
    right_bars: int,
) -> tuple[Pivot, ...]:
    """Tìm swing high/low; pivot chỉ khả dụng sau khi đủ nến bên phải đóng.

    So sánh nghiêm ngặt loại các đỉnh/đáy bằng nhau để không sinh nhiều pivot
    cho cùng một vùng phẳng. Dữ liệu thiếu, sai độ dài hoặc OHLC không hợp lệ
    trả về bộ rỗng thay vì suy diễn giá trị.
    """

    if left_bars < 1 or right_bars < 1 or not _valid_high_low(highs, lows):
        return ()

    pivots: list[Pivot] = []
    for index in range(left_bars, len(highs) - right_bars):
        left = range(index - left_bars, index)
        right = range(index + 1, index + right_bars + 1)
        neighbors = (*left, *right)
        if all(highs[index] > highs[neighbor] for neighbor in neighbors):
            pivots.append(Pivot("high", index, highs[index], index + right_bars))
        if all(lows[index] < lows[neighbor] for neighbor in neighbors):
            pivots.append(Pivot("low", index, lows[index], index + right_bars))

    return tuple(sorted(pivots, key=lambda pivot: (pivot.index, pivot.kind)))


def find_double_pattern(
    highs: Sequence[Decimal],
    lows: Sequence[Decimal],
    closes: Sequence[Decimal],
    *,
    tolerance: Decimal = Decimal("0.02"),
    min_pivot_distance: int = 2,
    max_pivot_distance: int = 80,
    left_bars: int = 1,
    right_bars: int = 1,
) -> PatternCandidate | None:
    """Tạo candidate hai đỉnh/đáy sau khi giá đóng cửa phá neckline.

    Chỉ hai pivot mới nhất của mỗi loại đủ điều kiện; một pivot mới hơn sẽ
    thay thế cặp cũ dù cặp mới không tạo candidate. Chỉ nến đóng từ thời điểm
    pivot thứ hai được xác nhận mới được dùng để xác nhận phá vỡ. Nếu cả hai
    hướng hợp lệ, chọn lần xác nhận mới nhất; trường hợp hòa chọn loại mẫu
    theo thứ tự tên enum để kết quả ổn định.
    """

    if (
        len(highs) != len(lows)
        or len(highs) != len(closes)
        or not _valid_high_low(highs, lows)
        or not _valid_positive(closes)
        or any(
            close < low or close > high
            for high, low, close in zip(highs, lows, closes, strict=True)
        )
        or not isinstance(tolerance, Decimal)
        or not tolerance.is_finite()
        or tolerance < 0
        or tolerance >= 1
        or min_pivot_distance < 1
        or max_pivot_distance < min_pivot_distance
    ):
        return None

    pivots = find_confirmed_pivots(highs, lows, left_bars=left_bars, right_bars=right_bars)
    candidates: list[PatternCandidate] = []
    for kind, pivot_type in (
        (PatternKind.DOUBLE_TOP, "high"),
        (PatternKind.DOUBLE_BOTTOM, "low"),
    ):
        same_kind = [pivot for pivot in pivots if pivot.kind == pivot_type]
        if len(same_kind) < 2:
            continue
        first, second = same_kind[-2:]
        distance = second.index - first.index
        if not min_pivot_distance <= distance <= max_pivot_distance:
            continue
        if not _swing_prices_match(first.price, second.price, tolerance):
            continue

        if kind is PatternKind.DOUBLE_TOP:
            neckline = min(lows[first.index + 1 : second.index])
            confirmation_index = _find_close_below(closes, neckline, second.confirmed_at_index)
            if confirmation_index is None:
                continue
            candidates.append(
                PatternCandidate(
                    kind=kind,
                    direction="short",
                    first_swing_index=first.index,
                    second_swing_index=second.index,
                    neckline=neckline,
                    confirmation_index=confirmation_index,
                    invalidation_price=max(first.price, second.price) * (Decimal("1") + tolerance),
                )
            )
            continue

        neckline = max(highs[first.index + 1 : second.index])
        confirmation_index = _find_close_above(closes, neckline, second.confirmed_at_index)
        if confirmation_index is None:
            continue
        candidates.append(
            PatternCandidate(
                kind=kind,
                direction="long",
                first_swing_index=first.index,
                second_swing_index=second.index,
                neckline=neckline,
                confirmation_index=confirmation_index,
                invalidation_price=min(first.price, second.price) * (Decimal("1") - tolerance),
            )
        )

    return _select_latest_candidate(candidates)


def _select_latest_candidate(
    candidates: Sequence[PatternCandidate],
) -> PatternCandidate | None:
    """Chọn xác nhận mới nhất; tên loại mẫu là quy tắc phá hòa ổn định."""

    if not candidates:
        return None
    return max(
        candidates,
        key=lambda candidate: (
            candidate.confirmation_index,
            candidate.kind.value,
        ),
    )


def _valid_positive(values: Sequence[Decimal]) -> bool:
    return bool(values) and all(
        isinstance(value, Decimal) and value.is_finite() and value > 0 for value in values
    )


def _valid_high_low(highs: Sequence[Decimal], lows: Sequence[Decimal]) -> bool:
    return (
        len(highs) == len(lows)
        and _valid_positive(highs)
        and _valid_positive(lows)
        and all(high >= low for high, low in zip(highs, lows, strict=True))
    )


def _swing_prices_match(first: Decimal, second: Decimal, tolerance: Decimal) -> bool:
    reference = max(first, second)
    return abs(first - second) / reference <= tolerance


def _find_close_below(closes: Sequence[Decimal], level: Decimal, start_index: int) -> int | None:
    return next(
        (index for index in range(start_index, len(closes)) if closes[index] < level),
        None,
    )


def _find_close_above(closes: Sequence[Decimal], level: Decimal, start_index: int) -> int | None:
    return next(
        (index for index in range(start_index, len(closes)) if closes[index] > level),
        None,
    )
