"""Tính khối lượng vị thế theo ngân sách lỗ và bước khối lượng của sàn."""

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Literal

TradingSide = Literal["LONG", "SHORT"]


@dataclass(frozen=True, slots=True)
class RiskSizingInput:
    """Đầu vào đã chuẩn hóa cho phép tính khối lượng vị thế."""

    equity: Decimal
    per_trade_risk_fraction: Decimal
    remaining_open_risk_budget: Decimal
    side: TradingSide
    entry_price: Decimal
    stop_price: Decimal
    contract_multiplier: Decimal
    entry_fee_per_unit: Decimal
    exit_fee_per_unit: Decimal
    adverse_funding_per_unit: Decimal
    slippage_stress_per_unit: Decimal
    quantity_step: Decimal
    min_notional: Decimal
    max_notional: Decimal
    exchange_max_quantity: Decimal
    leverage: Decimal
    max_leverage: Decimal
    available_margin: Decimal


@dataclass(frozen=True, slots=True)
class RiskSizingResult:
    """Kết quả sizing; bị từ chối thì quantity bằng không và có reason code."""

    accepted: bool
    reason: str | None
    risk_budget: Decimal
    loss_per_unit: Decimal
    quantity: Decimal
    estimated_loss: Decimal
    notional: Decimal
    initial_margin: Decimal


ZERO = Decimal("0")


def size_position(sizing_input: RiskSizingInput) -> RiskSizingResult:
    """Tính quantity làm tròn xuống sau khi áp ngân sách rủi ro và giới hạn sàn.

    Các chi phí theo đơn vị gồm phí vào/ra, funding bất lợi và slippage stress,
    cùng đơn vị quote với mức lỗ giá. Hàm này không thay thế RiskEngine hoặc
    reservation giao dịch đồng thời. Lượng vào còn bị chặn theo leverage cap,
    notional tối đa và margin khả dụng sau khi chia theo leverage đã chọn.
    """

    result_values = {
        "risk_budget": ZERO,
        "loss_per_unit": ZERO,
        "quantity": ZERO,
        "estimated_loss": ZERO,
        "notional": ZERO,
        "initial_margin": ZERO,
    }
    decimal_values = (
        sizing_input.equity,
        sizing_input.per_trade_risk_fraction,
        sizing_input.remaining_open_risk_budget,
        sizing_input.entry_price,
        sizing_input.stop_price,
        sizing_input.contract_multiplier,
        sizing_input.entry_fee_per_unit,
        sizing_input.exit_fee_per_unit,
        sizing_input.adverse_funding_per_unit,
        sizing_input.slippage_stress_per_unit,
        sizing_input.quantity_step,
        sizing_input.min_notional,
        sizing_input.max_notional,
        sizing_input.exchange_max_quantity,
        sizing_input.leverage,
        sizing_input.max_leverage,
        sizing_input.available_margin,
    )
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in decimal_values):
        return _rejected("DECIMAL_INPUT_INVALID", **result_values)
    if sizing_input.side not in ("LONG", "SHORT"):
        return _rejected("SIDE_INVALID", **result_values)

    positive_values = (
        sizing_input.equity,
        sizing_input.per_trade_risk_fraction,
        sizing_input.remaining_open_risk_budget,
        sizing_input.entry_price,
        sizing_input.stop_price,
        sizing_input.contract_multiplier,
        sizing_input.quantity_step,
        sizing_input.max_notional,
        sizing_input.exchange_max_quantity,
        sizing_input.leverage,
        sizing_input.max_leverage,
        sizing_input.available_margin,
    )
    if any(value <= ZERO for value in positive_values):
        return _rejected("INPUT_NOT_POSITIVE", **result_values)
    if sizing_input.per_trade_risk_fraction > Decimal("1"):
        return _rejected("RISK_FRACTION_INVALID", **result_values)
    if sizing_input.leverage > sizing_input.max_leverage:
        return _rejected("LEVERAGE_CAP_EXCEEDED", **result_values)
    if sizing_input.min_notional < ZERO:
        return _rejected("MIN_NOTIONAL_INVALID", **result_values)

    costs = (
        sizing_input.entry_fee_per_unit,
        sizing_input.exit_fee_per_unit,
        sizing_input.adverse_funding_per_unit,
        sizing_input.slippage_stress_per_unit,
    )
    if any(value < ZERO for value in costs):
        return _rejected("COST_INPUT_INVALID", **result_values)

    if sizing_input.side == "LONG" and sizing_input.stop_price >= sizing_input.entry_price:
        return _rejected("STOP_DIRECTION_INVALID", **result_values)
    if sizing_input.side == "SHORT" and sizing_input.stop_price <= sizing_input.entry_price:
        return _rejected("STOP_DIRECTION_INVALID", **result_values)

    risk_budget = min(
        sizing_input.equity * sizing_input.per_trade_risk_fraction,
        sizing_input.remaining_open_risk_budget,
    )
    loss_per_unit = abs(
        sizing_input.entry_price - sizing_input.stop_price
    ) * sizing_input.contract_multiplier + sum(costs, ZERO)
    if loss_per_unit <= ZERO:
        return _rejected(
            "LOSS_PER_UNIT_INVALID",
            risk_budget=risk_budget,
            loss_per_unit=loss_per_unit,
            quantity=ZERO,
            estimated_loss=ZERO,
            notional=ZERO,
            initial_margin=ZERO,
        )

    max_quantity_by_notional = sizing_input.max_notional / (
        sizing_input.entry_price * sizing_input.contract_multiplier
    )
    max_quantity_by_margin = (
        sizing_input.available_margin
        * sizing_input.leverage
        / (sizing_input.entry_price * sizing_input.contract_multiplier)
    )
    raw_quantity = risk_budget / loss_per_unit
    capped_quantity = min(
        raw_quantity,
        max_quantity_by_notional,
        max_quantity_by_margin,
        sizing_input.exchange_max_quantity,
    )
    step_count = (capped_quantity / sizing_input.quantity_step).to_integral_value(
        rounding=ROUND_DOWN
    )
    quantity = step_count * sizing_input.quantity_step
    notional = quantity * sizing_input.entry_price * sizing_input.contract_multiplier
    estimated_loss = quantity * loss_per_unit
    initial_margin = notional / sizing_input.leverage

    if quantity <= ZERO or notional < sizing_input.min_notional:
        return _rejected(
            "MIN_NOTIONAL_NOT_MET",
            risk_budget=risk_budget,
            loss_per_unit=loss_per_unit,
            quantity=ZERO,
            estimated_loss=ZERO,
            notional=notional,
            initial_margin=ZERO,
        )

    return RiskSizingResult(
        accepted=True,
        reason=None,
        risk_budget=risk_budget,
        loss_per_unit=loss_per_unit,
        quantity=quantity,
        estimated_loss=estimated_loss,
        notional=notional,
        initial_margin=initial_margin,
    )


def _rejected(
    reason: str,
    *,
    risk_budget: Decimal,
    loss_per_unit: Decimal,
    quantity: Decimal,
    estimated_loss: Decimal,
    notional: Decimal,
    initial_margin: Decimal,
) -> RiskSizingResult:
    return RiskSizingResult(
        accepted=False,
        reason=reason,
        risk_budget=risk_budget,
        loss_per_unit=loss_per_unit,
        quantity=quantity,
        estimated_loss=estimated_loss,
        notional=notional,
        initial_margin=initial_margin,
    )
