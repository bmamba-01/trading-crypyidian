"""Công thức thoát lệnh dùng chung cho các giả thuyết chiến lược."""

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExitRecipe:
    """Thông số stop, chốt lời và thời hạn giữ; chưa tự gửi lệnh ra sàn."""

    recipe_id: str
    stop_atr_multiple: float
    trailing_atr_multiple: float | None = None
    take_profit_atr_multiple: float | None = None
    opposite_signal_exit: bool = False
    max_hold_bars: int = 1
    exit_channel_period: int | None = None
    exit_feature_name: str | None = None
    exit_feature_operator: str | None = None
    exit_feature_threshold: float | None = None
    allow_scale_in: bool = False

    def __post_init__(self) -> None:
        if not self.recipe_id.strip():
            raise ValueError("recipe_id không được để trống.")
        for name in ("stop_atr_multiple", "trailing_atr_multiple", "take_profit_atr_multiple"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} phải hữu hạn và dương.")
        if self.exit_feature_threshold is not None and not math.isfinite(
            self.exit_feature_threshold
        ):
            raise ValueError("exit_feature_threshold phải hữu hạn.")
        if self.max_hold_bars <= 0:
            raise ValueError("max_hold_bars phải lớn hơn 0.")
        if self.exit_channel_period is not None and self.exit_channel_period <= 0:
            raise ValueError("exit_channel_period phải lớn hơn 0.")
        if (self.exit_feature_name is None) != (self.exit_feature_operator is None):
            raise ValueError("Điều kiện thoát theo feature cần cả tên và toán tử.")
        if self.exit_feature_name is not None:
            if self.exit_feature_threshold is None or self.exit_feature_operator not in {
                "<=",
                ">=",
            }:
                raise ValueError("Điều kiện thoát theo feature cần ngưỡng và toán tử <= hoặc >=.")
        elif self.exit_feature_threshold is not None:
            raise ValueError("Không được đặt ngưỡng feature khi thiếu tên feature.")
