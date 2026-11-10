"""Bộ dựng vùng thực thi và hình học giá (Execution Zones) tại tầng L6 theo QUANTSMART.

Tầng L6 đóng vai trò thiết lập cấu trúc rủi ro và mục tiêu:
- Tọa độ điểm vào lệnh (Entry Price).
- Điểm dừng lỗ bảo vệ (Initial Stop Loss) dựa trên bội số ATR.
- Điểm chốt lời mục tiêu (Take Profit) tương ứng với tỷ lệ Risk:Reward (R:R).
- Bảo đảm tính bất biến hình học: SL và TP luôn nằm đúng phía tương đối so với Entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ExecutionZones:
    """Vùng thực thi cấu trúc giá gồm Entry, Stop Loss, Take Profit và tỷ lệ R:R."""

    entry_price: Decimal
    stop_loss_price: Decimal
    take_profit_price: Decimal
    risk_reward_ratio: Decimal
    direction: str = "LONG"

    def __post_init__(self) -> None:
        if self.entry_price <= Decimal("0"):
            raise ValueError(f"entry_price phải là số dương, nhận được: {self.entry_price}")
        if self.stop_loss_price <= Decimal("0"):
            raise ValueError(f"stop_loss_price phải là số dương, nhận được: {self.stop_loss_price}")
        if self.take_profit_price <= Decimal("0"):
            raise ValueError(
                f"take_profit_price phải là số dương, nhận được: {self.take_profit_price}"
            )
        if self.risk_reward_ratio <= Decimal("0"):
            raise ValueError(
                f"risk_reward_ratio phải là số dương, nhận được: {self.risk_reward_ratio}"
            )

        dir_upper = self.direction.upper()
        if dir_upper == "LONG":
            if not (self.stop_loss_price < self.entry_price < self.take_profit_price):
                raise ValueError(
                    f"LONG: stop_loss_price ({self.stop_loss_price}) phải nhỏ hơn "
                    f"entry_price ({self.entry_price}) và take_profit_price "
                    f"({self.take_profit_price}) phải lớn hơn entry_price."
                )
        elif dir_upper == "SHORT":
            if not (self.take_profit_price < self.entry_price < self.stop_loss_price):
                raise ValueError(
                    f"SHORT: stop_loss_price ({self.stop_loss_price}) phải lớn hơn "
                    f"entry_price ({self.entry_price}) và take_profit_price "
                    f"({self.take_profit_price}) phải nhỏ hơn entry_price."
                )


class ZoneBuilder:
    """Bộ tính toán và xây dựng các vùng giá thực thi tầng L6."""

    def build_zones(
        self,
        direction: str,
        current_price: Decimal,
        atr: Decimal,
        sl_multiplier: Decimal = Decimal("2.0"),
        rr_ratio: Decimal = Decimal("2.0"),
    ) -> ExecutionZones:
        """Tính toán tọa độ Entry, Stop Loss, Take Profit theo tỷ lệ ATR và R:R."""
        if atr <= Decimal("0"):
            # Phòng thủ nếu ATR bằng 0: đặt khoảng biến động tối thiểu 0.5% giá hiện tại
            atr = current_price * Decimal("0.005")

        sl_distance = atr * sl_multiplier
        tp_distance = sl_distance * rr_ratio

        dir_upper = direction.upper()
        if dir_upper in {"LONG", "BUY"}:
            stop_loss = current_price - sl_distance
            take_profit = current_price + tp_distance
            actual_dir = "LONG"
        elif dir_upper in {"SHORT", "SELL"}:
            stop_loss = current_price + sl_distance
            take_profit = current_price - tp_distance
            actual_dir = "SHORT"
        else:
            raise ValueError(f"direction không hợp lệ: {direction}. Chỉ chấp nhận LONG hoặc SHORT.")

        return ExecutionZones(
            entry_price=current_price,
            stop_loss_price=stop_loss,
            take_profit_price=take_profit,
            risk_reward_ratio=rr_ratio,
            direction=actual_dir,
        )
