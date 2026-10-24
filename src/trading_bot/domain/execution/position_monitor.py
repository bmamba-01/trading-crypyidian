"""Bộ chuyển trạng thái vị thế theo nến, không dùng dữ liệu tương lai."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from trading_bot.domain.execution.paper import ZERO, PaperBar, PaperPosition


@dataclass(frozen=True, slots=True)
class PositionMonitorResult:
    """Kết quả kiểm tra exit và cập nhật protective stop cho một nến."""

    position: PaperPosition
    exit_reason: str | None = None
    exit_reference_price: Decimal | None = None


def monitor_position(
    position: PaperPosition,
    bar: PaperBar,
    *,
    opposing_signal: bool = False,
    atr: Decimal | None = None,
) -> PositionMonitorResult:
    """Kiểm tra stop/target trước time/opposing exit, rồi chỉ siết trailing stop.

    Nếu cùng nến chạm stop và target, chọn stop trước. Trailing mới tính từ nến
    hiện tại chỉ có hiệu lực cho nến kế tiếp, tránh giả định thứ tự OHLC không có.
    """
    if position.is_closed or bar.bar_key in position.seen_bar_keys:
        return PositionMonitorResult(position, position.exit_reason, position.exit_reference_price)
    if position.symbol != bar.symbol:
        raise ValueError("Nến monitor phải cùng symbol với vị thế.")
    bars_held = position.bars_held + 1
    seen = (*position.seen_bar_keys, bar.bar_key)
    stop_hit = (
        bar.low <= position.stop_price
        if position.direction == "long"
        else bar.high >= position.stop_price
    )
    target_hit = position.target_price is not None and (
        bar.high >= position.target_price
        if position.direction == "long"
        else bar.low <= position.target_price
    )
    if stop_hit:
        gap_price = (
            min(bar.open, position.stop_price)
            if position.direction == "long"
            else max(bar.open, position.stop_price)
        )
        closed = replace(
            position,
            quantity=ZERO,
            protective_stop_quantity=ZERO,
            bars_held=bars_held,
            is_closed=True,
            seen_bar_keys=seen,
            exit_reason="STOP_LOSS",
            exit_reference_price=gap_price,
        )
        return PositionMonitorResult(closed, "STOP_LOSS", gap_price)
    if target_hit:
        assert position.target_price is not None
        gap_price = (
            max(bar.open, position.target_price)
            if position.direction == "long"
            else min(bar.open, position.target_price)
        )
        closed = replace(
            position,
            quantity=ZERO,
            protective_stop_quantity=ZERO,
            bars_held=bars_held,
            is_closed=True,
            seen_bar_keys=seen,
            exit_reason="TAKE_PROFIT",
            exit_reference_price=gap_price,
        )
        return PositionMonitorResult(closed, "TAKE_PROFIT", gap_price)

    recipe = position.recipe
    if bars_held >= recipe.max_hold_bars:
        closed = replace(
            position,
            quantity=ZERO,
            protective_stop_quantity=ZERO,
            bars_held=bars_held,
            is_closed=True,
            seen_bar_keys=seen,
            exit_reason="TIME_STOP",
            exit_reference_price=bar.close,
        )
        return PositionMonitorResult(closed, "TIME_STOP", bar.close)
    if recipe.opposite_signal_exit and opposing_signal:
        closed = replace(
            position,
            quantity=ZERO,
            protective_stop_quantity=ZERO,
            bars_held=bars_held,
            is_closed=True,
            seen_bar_keys=seen,
            exit_reason="OPPOSING_SIGNAL",
            exit_reference_price=bar.close,
        )
        return PositionMonitorResult(closed, "OPPOSING_SIGNAL", bar.close)

    new_stop = position.stop_price
    trailing_multiple = recipe.trailing_atr_multiple
    if trailing_multiple is not None and atr is not None:
        if not atr.is_finite() or atr <= ZERO:
            raise ValueError("ATR trailing phải hữu hạn và dương.")
        multiple = Decimal(str(trailing_multiple))
        if position.direction == "long":
            candidate = bar.high - atr * multiple
            new_stop = max(position.stop_price, candidate)
        else:
            candidate = bar.low + atr * multiple
            new_stop = min(position.stop_price, candidate)
    updated = replace(position, stop_price=new_stop, bars_held=bars_held, seen_bar_keys=seen)
    return PositionMonitorResult(updated)
