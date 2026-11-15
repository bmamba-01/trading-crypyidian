"""Cấu trúc thị trường không nhìn trước: swing đã xác nhận, BOS/CHoCH và trendline tự động.

Cài đặt độc lập (clean-room) từ các khái niệm công khai về price action; không sao chép mã
Pine của bất kỳ chỉ báo TradingView nào. Mọi sự kiện chỉ được dùng từ ``confirmed_index``:
pivot tại nến ``t`` chỉ biết được khi nến ``t + right_bars`` đóng. Vì vậy chạy lần lượt từng
nến (streaming) và chạy cả chuỗi (batch) luôn cho cùng kết quả ở mọi nến đã qua.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from trading_bot.domain.strategy.geometry import find_confirmed_pivots

STRUCTURE_FORMULA_VERSION = "sig1-structure-v1"
TRENDLINE_FORMULA_VERSION = "sig1-trendline-v1"

SwingKind = Literal["high", "low"]
SwingLabel = Literal["HH", "HL", "LH", "LL"]
Trend = Literal["up", "down"]


@dataclass(frozen=True, slots=True)
class SwingPoint:
    """Đỉnh/đáy dao động đã xác nhận; ``label`` so với swing cùng loại trước đó."""

    kind: SwingKind
    index: int
    price: float
    confirmed_index: int
    label: SwingLabel | None


@dataclass(frozen=True, slots=True)
class StructureBreak:
    """Giá đóng cửa vượt swing đã xác nhận.

    ``BOS`` (phá vỡ cấu trúc) cùng chiều xu hướng hiện tại hoặc lần phá đầu tiên;
    ``CHOCH`` (đổi tính chất) là lần phá đầu tiên ngược chiều xu hướng đang có.
    """

    kind: Literal["BOS", "CHOCH"]
    direction: Literal["bullish", "bearish"]
    level: float
    swing_index: int
    break_index: int


@dataclass(frozen=True, slots=True)
class StructureResult:
    """Kết quả cấu trúc; ``trend[i]`` là xu hướng đã biết sau khi nến ``i`` đóng."""

    swings: tuple[SwingPoint, ...]
    breaks: tuple[StructureBreak, ...]
    trend: tuple[Trend | None, ...]
    formula_version: str = STRUCTURE_FORMULA_VERSION


def _validate_ohlc(high: Sequence[float], low: Sequence[float], close: Sequence[float]) -> None:
    if not len(high) == len(low) == len(close):
        raise ValueError("Các chuỗi high/low/close phải có cùng độ dài")
    for h, lo, c in zip(high, low, close, strict=True):
        if not (lo <= c <= h):
            raise ValueError("Nến không hợp lệ: close phải nằm trong [low, high]")


def confirmed_swings(
    high: Sequence[float], low: Sequence[float], *, left_bars: int, right_bars: int
) -> tuple[SwingPoint, ...]:
    """Swing đã xác nhận kèm nhãn HH/HL/LH/LL, xếp theo thời điểm xác nhận.

    Tái dùng ``find_confirmed_pivots`` (so sánh nghiêm ngặt hai phía). Đỉnh bằng đỉnh trước
    được xếp ``LH`` và đáy bằng đáy trước xếp ``HL`` — tức "không tạo cực trị mới".
    """
    pivots = find_confirmed_pivots(
        tuple(Decimal(repr(float(value))) for value in high),
        tuple(Decimal(repr(float(value))) for value in low),
        left_bars=left_bars,
        right_bars=right_bars,
    )
    previous: dict[str, float] = {}
    swings: list[SwingPoint] = []
    for pivot in sorted(pivots, key=lambda item: (item.confirmed_at_index, item.index, item.kind)):
        price = float(pivot.price)
        prior = previous.get(pivot.kind)
        label: SwingLabel | None
        if prior is None:
            label = None
        elif pivot.kind == "high":
            label = "HH" if price > prior else "LH"
        else:
            label = "LL" if price < prior else "HL"
        previous[pivot.kind] = price
        swings.append(SwingPoint(pivot.kind, pivot.index, price, pivot.confirmed_at_index, label))
    return tuple(swings)


def market_structure(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    *,
    left_bars: int = 3,
    right_bars: int = 3,
) -> StructureResult:
    """Máy trạng thái BOS/CHoCH chỉ dùng giá đóng cửa và swing đã xác nhận trước nến xét.

    Ở nến ``i``: (1) kiểm phá vỡ bằng ``close[i]`` so với swing đã biết tới nến ``i-1``;
    (2) sau đó mới nạp swing xác nhận tại ``i``. Mỗi swing chỉ bị phá một lần.
    """
    _validate_ohlc(high, low, close)
    swings = confirmed_swings(high, low, left_bars=left_bars, right_bars=right_bars)
    by_confirmation: dict[int, list[SwingPoint]] = {}
    for swing in swings:
        by_confirmation.setdefault(swing.confirmed_index, []).append(swing)

    active_high: SwingPoint | None = None
    active_low: SwingPoint | None = None
    trend: Trend | None = None
    breaks: list[StructureBreak] = []
    trend_by_bar: list[Trend | None] = []
    for index, closing in enumerate(close):
        if active_high is not None and closing > active_high.price:
            kind: Literal["BOS", "CHOCH"] = "CHOCH" if trend == "down" else "BOS"
            breaks.append(
                StructureBreak(kind, "bullish", active_high.price, active_high.index, index)
            )
            trend = "up"
            active_high = None
        if active_low is not None and closing < active_low.price:
            kind = "CHOCH" if trend == "up" else "BOS"
            breaks.append(
                StructureBreak(kind, "bearish", active_low.price, active_low.index, index)
            )
            trend = "down"
            active_low = None
        for swing in by_confirmation.get(index, ()):
            if swing.kind == "high":
                active_high = swing
            else:
                active_low = swing
        trend_by_bar.append(trend)
    return StructureResult(swings, tuple(breaks), tuple(trend_by_bar))


TrendlineSide = Literal["resistance", "support"]


@dataclass(frozen=True, slots=True)
class _LineCandidate:
    side: TrendlineSide
    first: SwingPoint
    second: SwingPoint
    slope: float
    slope_atr: float


@dataclass(frozen=True, slots=True)
class Trendline:
    """Đường nối hai swing cùng phía đã xác nhận.

    ``side='resistance'`` nối hai đỉnh, ``'support'`` nối hai đáy. Line có hiệu lực từ
    ``confirmed_index`` và bị vô hiệu tại ``invalidated_index`` khi giá đóng cửa vượt line
    quá ``break_atr`` ATR, hoặc khi một line mới cùng phía thay thế (``replaced_index``).
    """

    side: TrendlineSide
    p1_index: int
    p1_price: float
    p2_index: int
    p2_price: float
    confirmed_index: int
    slope_per_bar: float
    slope_atr: float | None
    invalidated_index: int | None
    replaced_index: int | None

    def price_at(self, index: int) -> float:
        """Giá của line tại một chỉ số nến (ngoại suy tuyến tính)."""
        return self.p2_price + self.slope_per_bar * (index - self.p2_index)

    @property
    def end_index(self) -> int | None:
        """Nến cuối line còn hiệu lực; ``None`` nghĩa là còn hiệu lực tới hiện tại."""
        candidates = [
            value for value in (self.invalidated_index, self.replaced_index) if value is not None
        ]
        return min(candidates) if candidates else None


@dataclass(frozen=True, slots=True)
class TrendlineResult:
    lines: tuple[Trendline, ...]
    formula_version: str = TRENDLINE_FORMULA_VERSION

    def active(self, side: TrendlineSide) -> Trendline | None:
        """Line đang hiệu lực ở cuối chuỗi cho một phía."""
        for line in reversed(self.lines):
            if line.side == side and line.end_index is None:
                return line
        return None


def auto_trendlines(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    atr_values: Sequence[float | None],
    *,
    left_bars: int = 3,
    right_bars: int = 3,
    tolerance_atr: float = 0.3,
    break_atr: float = 0.5,
    max_prior_checks: int = 4,
) -> TrendlineResult:
    """Trendline tự động từ hai swing gần nhất cùng phía, chỉ dùng dữ liệu đã biết.

    Line bị loại nếu một trong ``max_prior_checks`` swing cùng phía trước ``p1`` nằm vượt
    line quá ``tolerance_atr`` ATR (đo tại nến xác nhận ``p2``). Thiếu ATR tại nến xác nhận
    thì không tạo line (không đoán).
    """
    _validate_ohlc(high, low, close)
    if len(atr_values) != len(close):
        raise ValueError("Chuỗi ATR phải cùng độ dài với nến")
    swings = confirmed_swings(high, low, left_bars=left_bars, right_bars=right_bars)
    built: list[_LineCandidate] = []
    history: dict[str, list[SwingPoint]] = {"high": [], "low": []}
    for swing in swings:
        prior = history[swing.kind]
        atr_at = atr_values[swing.confirmed_index]
        if prior and atr_at is not None and atr_at > 0 and swing.index != prior[-1].index:
            first = prior[-1]
            slope = (swing.price - first.price) / (swing.index - first.index)
            side: TrendlineSide = "resistance" if swing.kind == "high" else "support"
            violated = False
            for older in prior[-1 - max_prior_checks : -1]:
                projected = swing.price + slope * (older.index - swing.index)
                excess = (
                    older.price - projected if side == "resistance" else projected - older.price
                )
                if excess > tolerance_atr * atr_at:
                    violated = True
                    break
            if not violated:
                built.append(_LineCandidate(side, first, swing, slope, slope / atr_at))
        prior.append(swing)

    lines: list[Trendline] = []
    for position, candidate in enumerate(built):
        replaced = next(
            (
                other.second.confirmed_index
                for other in built[position + 1 :]
                if other.side == candidate.side
            ),
            None,
        )
        invalidated: int | None = None
        second = candidate.second
        limit = replaced if replaced is not None else len(close)
        for index in range(second.confirmed_index + 1, limit):
            atr_now = atr_values[index]
            if atr_now is None:
                continue
            line_price = second.price + candidate.slope * (index - second.index)
            beyond = (
                close[index] - line_price
                if candidate.side == "resistance"
                else line_price - close[index]
            )
            if beyond > break_atr * atr_now:
                invalidated = index
                break
        lines.append(
            Trendline(
                side=candidate.side,
                p1_index=candidate.first.index,
                p1_price=candidate.first.price,
                p2_index=second.index,
                p2_price=second.price,
                confirmed_index=second.confirmed_index,
                slope_per_bar=candidate.slope,
                slope_atr=candidate.slope_atr,
                invalidated_index=invalidated,
                replaced_index=replaced,
            )
        )
    return TrendlineResult(tuple(lines))


StructureState = Literal["uptrend", "downtrend", "expanding", "contracting", "range", "unknown"]


def structure_state(result: TrendlineResult, *, flat_slope_atr: float = 0.02) -> StructureState:
    """Phân loại trạng thái theo dấu độ dốc hai line đang hiệu lực (đơn vị ATR/nến)."""
    resistance = result.active("resistance")
    support = result.active("support")
    if resistance is None or support is None:
        return "unknown"

    def direction(line: Trendline) -> int:
        slope = line.slope_atr or 0.0
        if abs(slope) < flat_slope_atr:
            return 0
        return 1 if slope > 0 else -1

    upper, lower = direction(resistance), direction(support)
    if upper > 0 and lower > 0:
        return "uptrend"
    if upper < 0 and lower < 0:
        return "downtrend"
    if upper > 0 and lower < 0:
        return "expanding"
    if upper < 0 and lower > 0:
        return "contracting"
    return "range"
