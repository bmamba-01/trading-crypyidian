"""Bộ chuyển trạng thái vị thế theo nến, không dùng dữ liệu tương lai."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_FLOOR, Decimal

from trading_bot.domain.execution.paper import ZERO, PaperBar, PaperPosition


@dataclass(frozen=True, slots=True)
class PartialExit:
    """Chốt lời một phần (SIG-3 lát 2): lượng, giá tham chiếu và stop mới (nếu dời)."""

    target_index: int
    quantity: Decimal
    reference_price: Decimal
    targets_hit: int
    new_stop: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PositionMonitorResult:
    """Kết quả kiểm tra exit và cập nhật protective stop cho một nến."""

    position: PaperPosition
    exit_reason: str | None = None
    exit_reference_price: Decimal | None = None
    partial_exit: PartialExit | None = None


def _ladder_step(position: PaperPosition, bar: PaperBar) -> tuple[int, Decimal] | None:
    """Mục tiêu chưa chốt bị chạm trong nến: (số mục tiêu đã chạm sau nến, giá chốt bảo thủ).

    Nhiều mục tiêu cùng nến được gộp và chốt ở giá mục tiêu ĐẦU TIÊN chạm (bảo thủ: không giả
    định giá đi hết tới mục tiêu xa nhất rồi mới chốt từng phần). Gap qua mục tiêu dùng giá mở.
    """
    pending = position.targets[position.targets_hit :]
    reached = [
        target
        for target in pending
        if (bar.high >= target.price if position.direction == "long" else bar.low <= target.price)
    ]
    if not reached:
        return None
    first = reached[0].price
    price = max(bar.open, first) if position.direction == "long" else min(bar.open, first)
    return position.targets_hit + len(reached), price


def monitor_position(
    position: PaperPosition,
    bar: PaperBar,
    *,
    opposing_signal: bool = False,
    atr: Decimal | None = None,
    exit_channel_level: Decimal | None = None,
    exit_feature_value: Decimal | None = None,
    strategy_bar_closed: bool = True,
    forced_exit_reason: str | None = None,
) -> PositionMonitorResult:
    """Kiểm tra stop/target, rồi exit theo kênh, thời gian, feature và tín hiệu ngược.

    Nếu cùng nến chạm stop và target, chọn stop trước. Trailing mới tính từ nến
    hiện tại chỉ có hiệu lực cho nến kế tiếp, tránh giả định thứ tự OHLC không có.

    - `exit_channel_level`: mức kênh thoát point-in-time (long: đáy thấp nhất, short: đỉnh cao
      nhất của `exit_channel_period` nến chiến lược trước đó). Chỉ có hiệu lực khi recipe khai
      `exit_channel_period`; chạm mức thì thoát như lệnh stop, gap qua mức khớp tại giá mở.
    - `exit_feature_value`: giá trị point-in-time của `recipe.exit_feature_name`; so với ngưỡng
      theo toán tử của recipe và thoát tại giá đóng nến.
    - `strategy_bar_closed`: nến monitor này có khép một nến của timeframe chiến lược hay không.
      `max_hold_bars` đếm theo nến chiến lược, nên nến thực thi nhỏ hơn (ví dụ 1 phút khi chiến
      lược chạy 1 giờ) chỉ tăng bộ đếm khi cờ này là True.

    Nơi gọi chỉ truyền hai mức exit khi release của vị thế có hiệu lực các exit đó; `None` giữ
    nguyên hành vi cũ.
    - `forced_exit_reason`: lý do đóng bắt buộc do vận hành (vd ``DELIST_EXIT`` khi Binance sắp gỡ
      hợp đồng). Xét **sau** stop/target (các mức đó có thể đã chạm trong nến) và đóng tại giá
      đóng nến, trước mọi exit theo chiến lược.
    """
    if position.is_closed or bar.bar_key in position.seen_bar_keys:
        return PositionMonitorResult(position, position.exit_reason, position.exit_reference_price)
    if position.symbol != bar.symbol:
        raise ValueError("Nến monitor phải cùng symbol với vị thế.")
    seen = (*position.seen_bar_keys, bar.bar_key)
    if position.pending_exit_reason is not None:
        # FX-4: lệnh thoát chờ từ nến trước khớp ngay tại giá mở nến kế tiếp.
        reason = position.pending_exit_reason
        closed = replace(
            position,
            quantity=ZERO,
            protective_stop_quantity=ZERO,
            is_closed=True,
            seen_bar_keys=seen,
            exit_reason=reason,
            exit_reference_price=bar.open,
            pending_exit_reason=None,
        )
        return PositionMonitorResult(closed, reason, bar.open)
    bars_held = position.bars_held + (1 if strategy_bar_closed else 0)
    stop_hit = (
        bar.low <= position.stop_price
        if position.direction == "long"
        else bar.high >= position.stop_price
    )
    ladder = _ladder_step(position, bar) if position.targets else None
    target_hit = (
        ladder is None
        and not position.targets
        and position.target_price is not None
        and (
            bar.high >= position.target_price
            if position.direction == "long"
            else bar.low <= position.target_price
        )
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
    if ladder is not None:
        hit_count, price = ladder
        if hit_count >= len(position.targets):
            closed = replace(
                position,
                quantity=ZERO,
                protective_stop_quantity=ZERO,
                bars_held=bars_held,
                is_closed=True,
                seen_bar_keys=seen,
                exit_reason="TAKE_PROFIT",
                exit_reference_price=price,
                targets_hit=hit_count,
            )
            return PositionMonitorResult(closed, "TAKE_PROFIT", price)
        fraction = sum(
            (target.fraction for target in position.targets[position.targets_hit : hit_count]),
            ZERO,
        )
        raw = position.initial_quantity * fraction
        step = position.quantity_step
        quantity = (
            (raw / step).to_integral_value(rounding=ROUND_FLOOR) * step if step > ZERO else raw
        )
        quantity = min(quantity, position.quantity)
        moves_stop = any(
            target.stop_to_entry_after
            for target in position.targets[position.targets_hit : hit_count]
        )
        new_stop = None
        if moves_stop:
            entry = position.entry_execution_price
            better = (
                entry > position.stop_price
                if position.direction == "long"
                else entry < position.stop_price
            )
            new_stop = entry if better else None
        advanced = replace(
            position,
            bars_held=bars_held,
            seen_bar_keys=seen,
            targets_hit=hit_count,
            stop_price=new_stop if new_stop is not None else position.stop_price,
        )
        if quantity <= ZERO:
            # Phần chốt nhỏ hơn bước khối lượng: chỉ ghi đã chạm; phần còn lại chờ mục tiêu sau.
            return PositionMonitorResult(advanced)
        partial = PartialExit(
            target_index=hit_count,
            quantity=quantity,
            reference_price=price,
            targets_hit=hit_count,
            new_stop=new_stop,
        )
        return PositionMonitorResult(advanced, partial_exit=partial)
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

    if forced_exit_reason is not None:
        if not forced_exit_reason.strip():
            raise ValueError("forced_exit_reason không được rỗng.")
        closed = replace(
            position,
            quantity=ZERO,
            protective_stop_quantity=ZERO,
            bars_held=bars_held,
            is_closed=True,
            seen_bar_keys=seen,
            exit_reason=forced_exit_reason,
            exit_reference_price=bar.close,
        )
        return PositionMonitorResult(closed, forced_exit_reason, bar.close)

    recipe = position.recipe
    if recipe.exit_channel_period is not None and exit_channel_level is not None:
        if not exit_channel_level.is_finite() or exit_channel_level <= ZERO:
            raise ValueError("Mức kênh thoát phải hữu hạn và dương.")
        channel_hit = (
            bar.low <= exit_channel_level
            if position.direction == "long"
            else bar.high >= exit_channel_level
        )
        if channel_hit:
            channel_price = (
                min(bar.open, exit_channel_level)
                if position.direction == "long"
                else max(bar.open, exit_channel_level)
            )
            closed = replace(
                position,
                quantity=ZERO,
                protective_stop_quantity=ZERO,
                bars_held=bars_held,
                is_closed=True,
                seen_bar_keys=seen,
                exit_reason="CHANNEL_EXIT",
                exit_reference_price=channel_price,
            )
            return PositionMonitorResult(closed, "CHANNEL_EXIT", channel_price)
    if bars_held >= recipe.max_hold_bars:
        pending = replace(
            position,
            bars_held=bars_held,
            seen_bar_keys=seen,
            pending_exit_reason="TIME_STOP",
        )
        return PositionMonitorResult(pending)
    if (
        recipe.exit_feature_name is not None
        and recipe.exit_feature_threshold is not None
        and exit_feature_value is not None
    ):
        if not exit_feature_value.is_finite():
            raise ValueError("Giá trị feature thoát phải hữu hạn.")
        threshold = Decimal(str(recipe.exit_feature_threshold))
        feature_hit = (
            exit_feature_value >= threshold
            if recipe.exit_feature_operator == ">="
            else exit_feature_value <= threshold
        )
        if feature_hit:
            closed = replace(
                position,
                quantity=ZERO,
                protective_stop_quantity=ZERO,
                bars_held=bars_held,
                is_closed=True,
                seen_bar_keys=seen,
                exit_reason="FEATURE_EXIT",
                exit_reference_price=bar.close,
            )
            return PositionMonitorResult(closed, "FEATURE_EXIT", bar.close)
    if recipe.opposite_signal_exit and opposing_signal:
        pending = replace(
            position,
            bars_held=bars_held,
            seen_bar_keys=seen,
            pending_exit_reason="OPPOSING_SIGNAL",
        )
        return PositionMonitorResult(pending)

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
