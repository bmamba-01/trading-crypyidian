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
    time_pending = monitor_position(position, _bar("time", 2))
    assert time_pending.exit_reason is None
    assert time_pending.position.pending_exit_reason == "TIME_STOP"
    assert not time_pending.position.is_closed
    next_time_bar = _bar("time-next", 3, open_price="100.75", high="102", low="98", close="101.25")
    time_result = monitor_position(time_pending.position, next_time_bar)
    assert time_result.exit_reason == "TIME_STOP"
    assert time_result.exit_reference_price == Decimal("100.75")
    assert time_result.position.is_closed
    assert time_result.position.pending_exit_reason is None

    opposite = replace(
        position, recipe=ExitRecipe("opposite", 2, opposite_signal_exit=True, max_hold_bars=5)
    )
    opposite_pending = monitor_position(opposite, _bar("opposite", 3), opposing_signal=True)
    assert opposite_pending.exit_reason is None
    assert opposite_pending.position.pending_exit_reason == "OPPOSING_SIGNAL"
    next_opp_bar = _bar("opposite-next", 4, open_price="99.25", high="102", low="98", close="98.5")
    opposite_result = monitor_position(opposite_pending.position, next_opp_bar)
    assert opposite_result.exit_reason == "OPPOSING_SIGNAL"
    assert opposite_result.exit_reference_price == Decimal("99.25")
    assert opposite_result.position.is_closed
    assert opposite_result.position.pending_exit_reason is None


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
    assert second.exit_reason is None
    assert second.position.pending_exit_reason == "TIME_STOP"
    third = monitor_position(
        second.position, _bar("strategy-3", 12, open_price="100.4", high="101", low="99")
    )
    assert third.exit_reason == "TIME_STOP"
    assert third.exit_reference_price == Decimal("100.4")
    assert third.position.bars_held == 2


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


def test_forced_delist_exit_closes_at_bar_close_after_stop_target_checks() -> None:
    import pytest

    position = replace(
        _open_position(),
        stop_price=Decimal("90"),
        target_price=Decimal("120"),
        recipe=ExitRecipe("long", 2, opposite_signal_exit=True, max_hold_bars=50),
    )
    calm = _bar("delist", 2, high="101", low="99")
    result = monitor_position(position, calm, forced_exit_reason="DELIST_EXIT")
    assert result.exit_reason == "DELIST_EXIT"
    assert result.exit_reference_price == calm.close
    assert result.position.is_closed
    assert result.position.quantity == Decimal(0)
    # Stop chạm trong cùng nến vẫn thắng (đã xảy ra trước giá đóng nến).
    crash = _bar("crash", 2, high="101", low="89")
    assert monitor_position(position, crash, forced_exit_reason="DELIST_EXIT").exit_reason == (
        "STOP_LOSS"
    )
    # Không ép đóng thì giữ hành vi cũ.
    assert monitor_position(position, calm).exit_reason is None
    with pytest.raises(ValueError, match="forced_exit_reason"):
        monitor_position(position, calm, forced_exit_reason=" ")


def test_gateway_forced_exit_creates_exit_fill_with_reason() -> None:
    gateway = _gateway()
    order = gateway.submit(_plan(), "intent-1", submitted_at=NOW)
    gateway.apply_market_event(_bar("bar-entry", 1), available_quantity=Decimal("1"))
    position = gateway.position(order.intent_id)
    assert position is not None
    result = gateway.monitor_position(
        position, _bar("delist-bar", 2), forced_exit_reason="DELIST_EXIT"
    )
    assert result.exit_reason == "DELIST_EXIT"
    closed = gateway.position(order.intent_id)
    assert closed is not None
    assert closed.is_closed
    assert closed.exit_reason == "DELIST_EXIT"


def test_pending_exit_survives_checkpoint_roundtrip_and_compat_with_legacy_snapshot() -> None:
    """FX-4: pending_exit_reason bền qua checkpoint/restart và tương thích checkpoint cũ."""
    import json

    from trading_bot.domain.execution.paper import PaperBar, PaperExecutionGateway
    from trading_bot.domain.risk.planner import calculate_plan_hash

    gateway = _gateway()
    base_plan = replace(_plan(), exit_recipe=ExitRecipe("time", 2, max_hold_bars=1))
    plan = replace(base_plan, plan_hash=calculate_plan_hash(base_plan))
    order = gateway.submit(plan, "intent-1", submitted_at=NOW)
    gateway.apply_market_event(_bar("bar-entry", 1), available_quantity=Decimal("1"))
    position = gateway.position(order.intent_id)
    assert position is not None

    # Giữ tương thích checkpoint cũ không có khóa `pending_exit_reason`.
    legacy_snapshot = gateway.export_state()
    legacy_body = json.loads(legacy_snapshot.payload)
    assert "pending_exit_reason" not in legacy_body["positions"][0]
    restored_legacy = PaperExecutionGateway.restore(gateway.policy, legacy_snapshot)
    legacy_pos = restored_legacy.position(order.intent_id)
    assert legacy_pos is not None
    assert legacy_pos.pending_exit_reason is None

    pending = gateway.monitor_position(position, _bar("trigger", 2))
    assert pending.exit_reason is None
    assert pending.position.pending_exit_reason == "TIME_STOP"

    # Mô phỏng restart giữa 2 nến: xuất snapshot rồi restore sang gateway mới.
    snapshot = gateway.export_state()
    body = json.loads(snapshot.payload)
    assert body["positions"][0]["pending_exit_reason"] == "TIME_STOP"
    restarted = PaperExecutionGateway.restore(gateway.policy, snapshot)
    restored_pos = restarted.position(order.intent_id)
    assert restored_pos is not None
    assert restored_pos.pending_exit_reason == "TIME_STOP"

    next_open = NOW + timedelta(minutes=3)
    next_bar = PaperBar(
        "after-restart",
        "BTCUSDT",
        next_open,
        next_open + timedelta(minutes=1),
        next_open + timedelta(minutes=1),
        Decimal("101.40"),
        Decimal("102.00"),
        Decimal("100.50"),
        Decimal("101.00"),
    )
    closed_result = restarted.monitor_position(restored_pos, next_bar)
    assert closed_result.exit_reason == "TIME_STOP"
    assert closed_result.exit_reference_price == Decimal("101.40")
    assert closed_result.position.is_closed
    assert closed_result.position.pending_exit_reason is None
    close_receipt = next(iter(restarted._close_keys.values()))
    assert close_receipt.fill is not None
    assert close_receipt.fill.filled_at == next_open


def test_time_stop_pending_exit_matches_time_and_price_across_1m_and_1h_monitor() -> None:
    """FX-4: monitor 1m hay 1h cho cùng thời điểm và giá thoát ở open nến kế tiếp."""
    from trading_bot.domain.execution.paper import PaperBar

    recipe = ExitRecipe("time-1h", 2, max_hold_bars=2)
    entry_open = NOW + timedelta(minutes=1)
    gw_1h = _gateway()
    gw_1m = _gateway()
    intent_ids: list[str] = []
    for gw in (gw_1h, gw_1m):
        submitted = gw.submit(_plan(), "intent-1", submitted_at=NOW)
        intent_ids.append(submitted.intent_id)
        gw.apply_market_event(_bar("entry", 1), available_quantity=Decimal("1"))
        pos = gw.position(submitted.intent_id)
        assert pos is not None
        gw._positions[submitted.intent_id] = replace(pos, stop_price=Decimal("80"), recipe=recipe)
    id_1h, id_1m = intent_ids

    hour1_start = entry_open + timedelta(hours=1)
    hour2_start = entry_open + timedelta(hours=2)
    hour3_start = entry_open + timedelta(hours=3)

    # Gateway 1h: 2 nến chiến lược -> pending TIME_STOP, nến giờ 3 khớp ở open=102.35
    for idx, start in enumerate((hour1_start, hour2_start), start=1):
        p = gw_1h.position(id_1h)
        assert p is not None
        gw_1h.monitor_position(
            p,
            PaperBar(
                f"1h-{idx}",
                "BTCUSDT",
                start,
                start + timedelta(hours=1),
                start + timedelta(hours=1),
                Decimal("100"),
                Decimal("103"),
                Decimal("98"),
                Decimal("102"),
            ),
            strategy_bar_closed=True,
        )
    p_1h = gw_1h.position(id_1h)
    assert p_1h is not None
    assert p_1h.pending_exit_reason == "TIME_STOP"
    res_1h = gw_1h.monitor_position(
        p_1h,
        PaperBar(
            "1h-3",
            "BTCUSDT",
            hour3_start,
            hour3_start + timedelta(hours=1),
            hour3_start + timedelta(hours=1),
            Decimal("102.35"),
            Decimal("104"),
            Decimal("101"),
            Decimal("103"),
        ),
        strategy_bar_closed=True,
    )

    # Gateway 1m: 120 nến 1m (strategy_bar_closed=True ở phút thứ 59 mỗi giờ), nến 1m đầu giờ 3 khớp
    for hour_start in (hour1_start, hour2_start):
        for minute in range(60):
            m_start = hour_start + timedelta(minutes=minute)
            p = gw_1m.position(id_1m)
            assert p is not None
            gw_1m.monitor_position(
                p,
                PaperBar(
                    f"1m-{m_start.isoformat()}",
                    "BTCUSDT",
                    m_start,
                    m_start + timedelta(minutes=1),
                    m_start + timedelta(minutes=1),
                    Decimal("100"),
                    Decimal("102"),
                    Decimal("99"),
                    Decimal("101"),
                ),
                strategy_bar_closed=(minute == 59),
            )
    p_1m = gw_1m.position(id_1m)
    assert p_1m is not None
    assert p_1m.pending_exit_reason == "TIME_STOP"
    res_1m = gw_1m.monitor_position(
        p_1m,
        PaperBar(
            "1m-hour3-0",
            "BTCUSDT",
            hour3_start,
            hour3_start + timedelta(minutes=1),
            hour3_start + timedelta(minutes=1),
            Decimal("102.35"),
            Decimal("103"),
            Decimal("102"),
            Decimal("102.5"),
        ),
        strategy_bar_closed=False,
    )

    assert res_1h.exit_reason == res_1m.exit_reason == "TIME_STOP"
    assert res_1h.exit_reference_price == res_1m.exit_reference_price == Decimal("102.35")
    assert res_1h.position.bars_held == res_1m.position.bars_held == 2
    assert res_1h.position.net_realized_pnl == res_1m.position.net_realized_pnl
    fill_1h = next(iter(gw_1h._close_keys.values())).fill
    fill_1m = next(iter(gw_1m._close_keys.values())).fill
    assert fill_1h is not None
    assert fill_1m is not None
    assert fill_1h.filled_at == fill_1m.filled_at == hour3_start
