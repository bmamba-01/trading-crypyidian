from decimal import Decimal

from trading_bot.domain.risk.sizing import RiskSizingInput, size_position


def test_position_quantity_respects_risk_budget_costs_and_exchange_step() -> None:
    sizing_input = RiskSizingInput(
        equity=Decimal("10000"),
        per_trade_risk_fraction=Decimal("0.01"),
        remaining_open_risk_budget=Decimal("100"),
        side="LONG",
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        contract_multiplier=Decimal("1"),
        entry_fee_per_unit=Decimal("0.05"),
        exit_fee_per_unit=Decimal("0.05"),
        adverse_funding_per_unit=Decimal("0.02"),
        slippage_stress_per_unit=Decimal("0.03"),
        quantity_step=Decimal("0.1"),
        min_notional=Decimal("5"),
        max_notional=Decimal("3000"),
        exchange_max_quantity=Decimal("100"),
        leverage=Decimal("2"),
        max_leverage=Decimal("5"),
        available_margin=Decimal("1000"),
    )

    result = size_position(sizing_input)

    assert result.accepted is True
    assert result.risk_budget == Decimal("100.00")
    assert result.loss_per_unit == Decimal("5.15")
    assert result.quantity == Decimal("19.4")
    assert result.estimated_loss == Decimal("99.910")
    assert result.notional == Decimal("1940.0")


def test_position_quantity_is_capped_by_remaining_open_risk_budget() -> None:
    sizing_input = _valid_input(remaining_open_risk_budget=Decimal("5"))

    result = size_position(sizing_input)

    assert result.accepted is True
    assert result.risk_budget == Decimal("5")
    assert result.quantity == Decimal("1.0")


def test_position_quantity_rejects_stop_on_wrong_side() -> None:
    result = size_position(_valid_input(stop_price=Decimal("101")))

    assert result.accepted is False
    assert result.reason == "STOP_DIRECTION_INVALID"
    assert result.quantity == Decimal("0")


def test_position_quantity_rejects_when_rounded_value_is_below_minimum() -> None:
    result = size_position(_valid_input(min_notional=Decimal("1000")))

    assert result.accepted is False
    assert result.reason == "MIN_NOTIONAL_NOT_MET"
    assert result.quantity == Decimal("0")


def test_position_quantity_rejects_negative_cost_inputs() -> None:
    result = size_position(
        _valid_input(
            entry_price=Decimal("100"),
            stop_price=Decimal("99.9"),
            entry_fee_per_unit=Decimal("-1"),
            exit_fee_per_unit=Decimal("-1"),
            adverse_funding_per_unit=Decimal("-1"),
            slippage_stress_per_unit=Decimal("-1"),
        )
    )

    assert result.accepted is False
    assert result.reason == "COST_INPUT_INVALID"


def test_position_quantity_rejects_non_finite_decimal_input() -> None:
    result = size_position(_valid_input(entry_price=Decimal("NaN")))

    assert result.accepted is False
    assert result.reason == "DECIMAL_INPUT_INVALID"


def test_position_quantity_is_capped_by_available_margin() -> None:
    result = size_position(_valid_input(available_margin=Decimal("50")))

    assert result.accepted is True
    assert result.quantity == Decimal("1.0")
    assert result.notional == Decimal("100.0")
    assert result.initial_margin == Decimal("50.0")


def test_position_quantity_rejects_leverage_above_hard_cap() -> None:
    result = size_position(_valid_input(leverage=Decimal("6"), max_leverage=Decimal("5")))

    assert result.accepted is False
    assert result.reason == "LEVERAGE_CAP_EXCEEDED"


def _valid_input(**overrides: Decimal | str) -> RiskSizingInput:
    values: dict[str, Decimal | str] = {
        "equity": Decimal("1000"),
        "per_trade_risk_fraction": Decimal("0.01"),
        "remaining_open_risk_budget": Decimal("100"),
        "side": "LONG",
        "entry_price": Decimal("100"),
        "stop_price": Decimal("95"),
        "contract_multiplier": Decimal("1"),
        "entry_fee_per_unit": Decimal("0"),
        "exit_fee_per_unit": Decimal("0"),
        "adverse_funding_per_unit": Decimal("0"),
        "slippage_stress_per_unit": Decimal("0"),
        "quantity_step": Decimal("0.1"),
        "min_notional": Decimal("5"),
        "max_notional": Decimal("10000"),
        "exchange_max_quantity": Decimal("100"),
        "leverage": Decimal("2"),
        "max_leverage": Decimal("5"),
        "available_margin": Decimal("10000"),
    }
    values.update(overrides)
    return RiskSizingInput(**values)  # type: ignore[arg-type]
