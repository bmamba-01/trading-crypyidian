from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from test_paper_execution import NOW, _bar, _gateway, _plan

from trading_bot.domain.execution.position_monitor import monitor_position
from trading_bot.domain.strategy.recipes import ExitRecipe


def _open_position():
    gateway = _gateway()
    order = gateway.submit(_plan(), "intent-1", submitted_at=NOW)
    gateway.apply_market_event(_bar("bar-entry", 1), available_quantity=Decimal("1"))
    position = gateway.position(order.intent_id)
    assert position is not None
    return position


def test_same_bar_stop_and_target_uses_conservative_stop_first() -> None:
    position = replace(
        _open_position(),
        stop_price=Decimal("98"),
        target_price=Decimal("102"),
    )
    event = _bar("both-hit", 2, high="103", low="97")

    result = monitor_position(position, event)

    assert result.exit_reason == "STOP_LOSS"
    assert result.exit_reference_price == position.stop_price
    assert result.position.is_closed


def test_trailing_stop_only_moves_in_favorable_direction() -> None:
    long_position = replace(
        _open_position(),
        stop_price=Decimal("96"),
        recipe=ExitRecipe("trail", 2, trailing_atr_multiple=1, max_hold_bars=10),
    )
    advanced = monitor_position(
        long_position, _bar("trail-up", 2, high="110", low="100"), atr=Decimal("2")
    ).position
    retreated = monitor_position(
        advanced, _bar("trail-down", 3, high="105", low="99"), atr=Decimal("2")
    ).position

    assert advanced.stop_price == Decimal("108")
    assert retreated.stop_price == advanced.stop_price


def test_time_and_opposing_signal_exit_reasons_are_reported() -> None:
    position = replace(_open_position(), recipe=ExitRecipe("time", 2, max_hold_bars=1))
    time_result = monitor_position(position, _bar("time", 2))
    assert time_result.exit_reason == "TIME_STOP"

    opposite = replace(
        position, recipe=ExitRecipe("opposite", 2, opposite_signal_exit=True, max_hold_bars=5)
    )
    opposite_result = monitor_position(opposite, _bar("opposite", 3), opposing_signal=True)
    assert opposite_result.exit_reason == "OPPOSING_SIGNAL"


def test_funding_event_is_applied_once_and_cost_components_sum_to_net() -> None:
    gateway = _gateway()
    order = gateway.submit(_plan(), "intent-1", submitted_at=NOW)
    gateway.apply_market_event(_bar("entry", 1), available_quantity=Decimal("1"))
    position = gateway.position(order.intent_id)
    assert position is not None

    charged = gateway.apply_funding_event(position.position_id, "funding-1", Decimal("0.25"))
    repeated = gateway.apply_funding_event(position.position_id, "funding-1", Decimal("0.25"))
    assert repeated == charged
    assert charged.funding_cost == Decimal("0.25")

    closed = gateway.close_position(
        position.position_id,
        position.quantity,
        reference_price=Decimal("101"),
        closed_at=NOW + timedelta(minutes=2),
        idempotency_key="close-1",
    )
    assert closed.position.net_realized_pnl == (
        closed.position.gross_realized_pnl
        - closed.position.fee_cost
        - closed.position.funding_cost
        - closed.position.slippage_cost
    )
    assert charged.funding_event_ids == ("funding-1",)


def test_monitor_event_is_idempotent_and_duplicate_bar_does_not_increment_hold_count() -> None:
    position = _open_position()
    bar = _bar("held", 2)

    first = monitor_position(position, bar)
    repeated = monitor_position(first.position, bar)

    assert first.position.bars_held == 1
    assert repeated == first
