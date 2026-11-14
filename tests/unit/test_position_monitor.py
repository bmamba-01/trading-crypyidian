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


def test_channel_exit_closes_long_when_bar_touches_the_exit_channel() -> None:
    """H03 v2: chạm đáy kênh thoát thì đóng như lệnh stop, trước time stop."""
    position = replace(
        _open_position(),
        stop_price=Decimal("90"),
        target_price=None,
        recipe=ExitRecipe("channel", 2, max_hold_bars=50, exit_channel_period=20),
    )

    untouched = monitor_position(
        position, _bar("above", 2, high="104", low="99.5"), exit_channel_level=Decimal("99")
    )
    assert untouched.exit_reason is None

    touched = monitor_position(
        untouched.position,
        _bar("touch", 3, high="101", low="98.5"),
        exit_channel_level=Decimal("99"),
    )
    assert touched.exit_reason == "CHANNEL_EXIT"
    assert touched.exit_reference_price == Decimal("99")
    assert touched.position.is_closed


def test_channel_exit_gap_fills_at_open_and_stop_still_wins() -> None:
    recipe = ExitRecipe("channel", 2, max_hold_bars=50, exit_channel_period=20)
    position = replace(_open_position(), stop_price=Decimal("90"), target_price=None, recipe=recipe)

    gapped = monitor_position(
        position,
        _bar("gap", 2, open_price="96", high="97", low="95", close="96"),
        exit_channel_level=Decimal("99"),
    )
    assert gapped.exit_reason == "CHANNEL_EXIT"
    assert gapped.exit_reference_price == Decimal("96")

    stopped = monitor_position(
        position, _bar("stop", 2, high="100", low="89"), exit_channel_level=Decimal("99")
    )
    assert stopped.exit_reason == "STOP_LOSS"


def test_channel_exit_for_short_uses_the_channel_high() -> None:
    position = replace(
        _open_position(),
        direction="short",
        stop_price=Decimal("110"),
        target_price=None,
        recipe=ExitRecipe("channel", 2, max_hold_bars=50, exit_channel_period=20),
    )
    result = monitor_position(
        position, _bar("short-touch", 2, high="101.5", low="99"), exit_channel_level=Decimal("101")
    )
    assert result.exit_reason == "CHANNEL_EXIT"
    assert result.exit_reference_price == Decimal("101")


def test_channel_level_is_ignored_when_recipe_declares_no_channel_exit() -> None:
    position = replace(
        _open_position(),
        stop_price=Decimal("90"),
        target_price=None,
        recipe=ExitRecipe("plain", 2, max_hold_bars=50),
    )
    result = monitor_position(
        position, _bar("touch", 2, high="101", low="98"), exit_channel_level=Decimal("99")
    )
    assert result.exit_reason is None


def test_feature_exit_follows_recipe_operator_and_threshold() -> None:
    """H06 v2: long thoát khi z-score hồi lên tới ngưỡng; chưa tới thì giữ."""
    recipe = ExitRecipe(
        "zscore",
        1.5,
        max_hold_bars=16,
        exit_feature_name="zscore_20",
        exit_feature_operator=">=",
        exit_feature_threshold=-0.2,
    )
    position = replace(_open_position(), stop_price=Decimal("90"), target_price=None, recipe=recipe)

    waiting = monitor_position(
        position, _bar("z-low", 2, high="101", low="99"), exit_feature_value=Decimal("-1.4")
    )
    assert waiting.exit_reason is None

    reverted = monitor_position(
        waiting.position,
        _bar("z-back", 3, high="101", low="99"),
        exit_feature_value=Decimal("-0.1"),
    )
    assert reverted.exit_reason == "FEATURE_EXIT"
    assert reverted.exit_reference_price == _bar("z-back", 3, high="101", low="99").close


def test_feature_value_is_ignored_without_recipe_feature_exit() -> None:
    position = replace(
        _open_position(),
        stop_price=Decimal("90"),
        target_price=None,
        recipe=ExitRecipe("plain", 2, max_hold_bars=50),
    )
    result = monitor_position(
        position, _bar("z", 2, high="101", low="99"), exit_feature_value=Decimal("5")
    )
    assert result.exit_reason is None


def test_hold_count_only_advances_on_closed_strategy_bars() -> None:
    """Nến thực thi 1 phút không được làm `max_hold_bars` của chiến lược 1 giờ hết sớm."""
    position = replace(
        _open_position(),
        stop_price=Decimal("90"),
        target_price=None,
        recipe=ExitRecipe("time", 2, max_hold_bars=2),
    )
    current = position
    for index in range(5):
        result = monitor_position(
            current,
            _bar(f"intrabar-{index}", 2 + index, high="101", low="99"),
            strategy_bar_closed=False,
        )
        assert result.exit_reason is None
        current = result.position
    assert current.bars_held == position.bars_held

    first = monitor_position(current, _bar("strategy-1", 10, high="101", low="99"))
    assert first.exit_reason is None
    second = monitor_position(first.position, _bar("strategy-2", 11, high="101", low="99"))
    assert second.exit_reason == "TIME_STOP"


def test_replay_selects_exit_evidence_only_for_v2_recipes() -> None:
    from trading_bot.application.runtime_replay_evaluator import (
        ReplayEvaluationContextError,
        ReplayExitEvidence,
        _exit_evidence_for_position,
    )

    position = _open_position()
    bar = _bar("evidence-bar", 2)
    v1 = replace(position, recipe=ExitRecipe("h03-exit-v1", 2.0, exit_channel_period=20))
    assert _exit_evidence_for_position(v1, bar, {}) == (None, None, True)

    v2 = replace(position, recipe=ExitRecipe("h03-exit-v2", 2.0, exit_channel_period=20))
    # Recipe v2 thiếu bằng chứng thì dừng replay, không âm thầm bỏ qua điều kiện thoát.
    try:
        _exit_evidence_for_position(v2, bar, {})
    except ReplayEvaluationContextError:
        pass
    else:  # pragma: no cover - bảo vệ hồi quy
        raise AssertionError("Recipe v2 phải yêu cầu bằng chứng thoát.")

    evidence = ReplayExitEvidence(
        symbol=bar.symbol,
        bar_key=bar.bar_key,
        recipe_id="h03-exit-v2",
        strategy_bar_closed=False,
        channel_low=Decimal("99"),
        channel_high=Decimal("110"),
    )
    key = (bar.symbol, bar.bar_key, "h03-exit-v2")
    expected_level = Decimal("99") if v2.direction == "long" else Decimal("110")
    assert _exit_evidence_for_position(v2, bar, {key: evidence}) == (expected_level, None, False)

    h06 = replace(
        position,
        recipe=ExitRecipe(
            "h06-exit-v2",
            1.5,
            exit_feature_name="zscore_20",
            exit_feature_operator=">=",
            exit_feature_threshold=-0.2,
        ),
    )
    key = (bar.symbol, bar.bar_key, "h06-exit-v2")
    open_bar = replace(evidence, recipe_id="h06-exit-v2", feature_value=Decimal("0.1"))
    # Nến chiến lược chưa đóng: không dùng giá trị feature dù có được cấp.
    assert _exit_evidence_for_position(h06, bar, {key: open_bar}) == (None, None, False)
    closed_bar = replace(open_bar, strategy_bar_closed=True)
    assert _exit_evidence_for_position(h06, bar, {key: closed_bar}) == (
        None,
        Decimal("0.1"),
        True,
    )


def test_funding_settlement_uses_mark_price_notional_and_rate_sign() -> None:
    from trading_bot.domain.execution.funding import FundingSettlement, funding_cashflow

    gateway = _gateway()
    order = gateway.submit(_plan(), "intent-1", submitted_at=NOW)
    gateway.apply_market_event(_bar("bar-entry", 1), available_quantity=Decimal("1"))
    position = gateway.position(order.intent_id)
    assert position is not None
    settlement = FundingSettlement(
        symbol=position.symbol,
        funding_time=NOW + timedelta(hours=8),
        funding_rate=Decimal("0.0001"),
        mark_price=Decimal("102"),
    )
    nominal = Decimal("102") * position.quantity * position.contract_multiplier

    # Rate dương: long trả (chi phí dương), short nhận (chi phí âm) đúng cùng một số tiền.
    paid = funding_cashflow(
        direction="long",
        quantity=position.quantity,
        contract_multiplier=position.contract_multiplier,
        settlement=settlement,
    )
    received = funding_cashflow(
        direction="short",
        quantity=position.quantity,
        contract_multiplier=position.contract_multiplier,
        settlement=settlement,
    )
    assert paid == nominal * Decimal("0.0001")
    assert received == -paid

    updated = gateway.apply_funding_settlement(position.position_id, settlement)
    again = gateway.apply_funding_settlement(position.position_id, settlement)
    expected = paid if position.direction == "long" else received
    assert updated.funding_cost == position.funding_cost + expected
    # Cùng một kỳ funding không bị ghi hai lần.
    assert again.funding_cost == updated.funding_cost
    assert again.funding_event_ids.count(settlement.event_id) == 1
