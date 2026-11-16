"""Thang chốt lời nhiều mục tiêu (SIG-3 lát 2) — cấu trúc riêng, không sửa ``ExitRecipe``.

``release_content_hash`` băm mã nguồn ``ExitRecipe``; thêm trường vào đó sẽ đổi danh tính mọi
release đang ghim. Vì vậy thang chốt lời là cấu trúc độc lập, đăng ký theo release (chỉ release
scalping SIG-4 có), và ``TradePlan.targets`` chỉ vào hash khi khác rỗng.

Quy ước ``sig3-ladder-v1``:

- Mỗi bậc: bội số R (khoảng cách entry→stop) và tỷ lệ khối lượng ban đầu cần chốt; tổng tỷ lệ = 1,
  R tăng dần, tối đa 3 bậc. Giá mục tiêu = entry ± R × |entry − stop|, làm tròn về phía bảo thủ
  (LONG làm tròn lên, SHORT làm tròn xuống — xa entry hơn, khó chạm hơn).
- R:R ròng mỗi bậc dùng cùng công thức với planner (``net_reward_to_risk``): lợi nhuận tại mục
  tiêu và lỗ tại stop đều đã trừ phí hai chiều, funding bất lợi và trượt giá thoát.
- ``breakeven_after_first``: sau khi chạm mục tiêu đầu, dời stop của phần còn lại về entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from trading_bot.domain.risk.planner import CostModel

LADDER_VERSION = "sig3-ladder-v1"
MAX_LADDER_STEPS = 3
ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class LadderStep:
    r_multiple: Decimal
    fraction: Decimal


@dataclass(frozen=True, slots=True)
class TakeProfitLadder:
    steps: tuple[LadderStep, ...]
    breakeven_after_first: bool = False
    version: str = LADDER_VERSION

    def __post_init__(self) -> None:
        if not 1 <= len(self.steps) <= MAX_LADDER_STEPS:
            raise ValueError(f"Thang chốt lời cần 1–{MAX_LADDER_STEPS} bậc.")
        if any(step.r_multiple <= ZERO or step.fraction <= ZERO for step in self.steps):
            raise ValueError("Bội số R và tỷ lệ chốt phải dương.")
        if any(
            b.r_multiple <= a.r_multiple for a, b in zip(self.steps, self.steps[1:], strict=False)
        ):
            raise ValueError("Bội số R của các bậc phải tăng dần.")
        if sum((step.fraction for step in self.steps), ZERO) != ONE:
            raise ValueError("Tổng tỷ lệ chốt của thang phải bằng 1.")


@dataclass(frozen=True, slots=True)
class PlanTarget:
    """Một mục tiêu của kế hoạch: giá, tỷ lệ khối lượng ban đầu, bội số R, R:R ròng sau phí."""

    index: int
    price: Decimal
    fraction: Decimal
    r_multiple: Decimal
    net_reward_to_risk: Decimal
    # Sau khi chạm mục tiêu này, dời stop của phần còn lại về giá vào (chỉ bậc 1 nếu thang bật).
    stop_to_entry_after: bool = False


def net_reward_to_risk(
    *,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    multiplier: Decimal,
    cost_model: CostModel,
) -> Decimal:
    """R:R ròng trên một đơn vị — cùng công thức planner dùng cho mục tiêu đơn."""
    common_cost = entry * multiplier * cost_model.entry_fee_rate + cost_model.funding_per_unit(
        entry, multiplier
    )
    net_reward = (
        abs(target - entry) * multiplier
        - common_cost
        - target * multiplier * (cost_model.exit_fee_rate + cost_model.exit_slippage_rate)
    )
    net_risk = (
        abs(entry - stop) * multiplier
        + common_cost
        + stop * multiplier * cost_model.exit_fee_rate
        + stop * multiplier * cost_model.exit_slippage_rate
    )
    return net_reward / net_risk


def _round(price: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (price / tick).to_integral_value(rounding=rounding) * tick


def build_targets(
    *,
    side: Literal["LONG", "SHORT"],
    entry: Decimal,
    stop: Decimal,
    ladder: TakeProfitLadder,
    tick_size: Decimal,
    multiplier: Decimal,
    cost_model: CostModel,
) -> tuple[PlanTarget, ...]:
    """Giá mục tiêu theo R của khoảng entry→stop; kiểm hướng và tăng dần sau làm tròn."""
    risk = abs(entry - stop)
    if risk <= ZERO:
        raise ValueError("Khoảng entry→stop phải dương để dựng thang chốt lời.")
    targets: list[PlanTarget] = []
    for index, step in enumerate(ladder.steps, start=1):
        raw = entry + step.r_multiple * risk if side == "LONG" else entry - step.r_multiple * risk
        price = _round(raw, tick_size, ROUND_CEILING if side == "LONG" else ROUND_FLOOR)
        if (
            price <= ZERO
            or (side == "LONG" and price <= entry)
            or (side == "SHORT" and price >= entry)
        ):
            raise ValueError("Mục tiêu không nằm phía lợi nhuận của entry.")
        if targets and (
            (side == "LONG" and price <= targets[-1].price)
            or (side == "SHORT" and price >= targets[-1].price)
        ):
            raise ValueError("Các mục tiêu trùng nhau sau khi làm tròn bước giá.")
        targets.append(
            PlanTarget(
                index=index,
                price=price,
                fraction=step.fraction,
                r_multiple=step.r_multiple,
                net_reward_to_risk=net_reward_to_risk(
                    entry=entry,
                    stop=stop,
                    target=price,
                    multiplier=multiplier,
                    cost_model=cost_model,
                ),
                stop_to_entry_after=ladder.breakeven_after_first and index == 1,
            )
        )
    return tuple(targets)


def weighted_net_reward_to_risk(targets: tuple[PlanTarget, ...]) -> Decimal:
    """R:R ròng bình quân theo tỷ lệ chốt — dùng cho cổng R:R tối thiểu của cả thang."""
    return sum((target.fraction * target.net_reward_to_risk for target in targets), ZERO)
