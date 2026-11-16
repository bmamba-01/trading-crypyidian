"""SIG-3 lát 2: PAPER chốt lời từng phần theo thang mục tiêu, dời stop về entry sau TP1."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from test_paper_execution import NOW, _bar, _gateway, _plan, _settings

from trading_bot.domain.execution.paper import PaperExecutionGateway, PaperPosition
from trading_bot.domain.execution.position_monitor import monitor_position
from trading_bot.domain.risk.planner import AccountSnapshot, CostModel, TradePlan, TradePlanner
from trading_bot.domain.risk.take_profit import LadderStep, TakeProfitLadder

LADDER = TakeProfitLadder(
    steps=(LadderStep(Decimal("1"), Decimal("0.5")), LadderStep(Decimal("2"), Decimal("0.5"))),
    breakeven_after_first=True,
)


def _laddered_plan() -> TradePlan:
    base = _plan()
    candidate_plan = TradePlanner().plan(
        _candidate_from(base),
        _settings(),
        AccountSnapshot(
            "workspace-a", "paper-a", "settings-v1", NOW, Decimal("10000"), Decimal("1000"), True
        ),
        (),
        CostModel("cost-v1", Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")),
        now=NOW + timedelta(seconds=3),
        ladder=LADDER,
    )
    assert isinstance(candidate_plan, TradePlan)
    return candidate_plan


def _candidate_from(plan: TradePlan):  # type: ignore[no-untyped-def]
    from trading_bot.domain.strategy.contracts import StrategyCandidate

    return StrategyCandidate(
        strategy_id=plan.strategy_id,
        strategy_release_id=plan.strategy_release_id,
        release_content_hash=plan.release_content_hash,
        settings_version_id=plan.settings_version_id,
        snapshot_id=plan.snapshot_id,
        symbol=plan.symbol,
        direction=plan.direction,
        reference_price=plan.entry_reference_price,
        atr_value=2.0,
        signal_at=NOW,
        eligible_after=NOW + timedelta(seconds=2),
        expires_at=NOW + timedelta(minutes=10),
        recipe=plan.exit_recipe,
        evidence_feature_ids=plan.evidence_feature_ids,
        evidence_bar_keys=plan.evidence_bar_keys,
        macro_evidence_ids=plan.macro_evidence_ids,
        geometry_version=None,
        reason_code=plan.candidate_reason_code,
        detail_vi="Tín hiệu kiểm thử có thang chốt lời.",
    )


def _open(gateway: PaperExecutionGateway, plan: TradePlan) -> PaperPosition:
    order = gateway.submit(plan, "intent-ladder", submitted_at=NOW)
    minute = 1
    while True:
        gateway.apply_market_event(
            _bar(f"entry-{minute}", minute), available_quantity=Decimal("10")
        )
        position = gateway.position(order.intent_id)
        if position is not None and position.quantity == plan.quantity:
            return position
        minute += 1
        assert minute < 20, "không khớp đủ lệnh vào"


def test_plan_targets_are_carried_into_position() -> None:
    plan = _laddered_plan()
    gateway = _gateway()
    position = _open(gateway, plan)
    assert position.targets == plan.targets
    assert position.initial_quantity == plan.quantity
    assert position.quantity_step == plan.exchange_rules.quantity_step
    assert plan.targets[0].stop_to_entry_after is True
    assert plan.targets[1].stop_to_entry_after is False


def test_tp1_partial_then_breakeven_stop_closes_remainder() -> None:
    plan = _laddered_plan()
    gateway = _gateway()
    position = _open(gateway, plan)
    tp1, tp2 = (target.price for target in plan.targets)
    hit_tp1 = _bar(
        "tp1", 30, open_price=str(tp1 - 1), high=str(tp1), low=str(tp1 - 1), close=str(tp1)
    )
    result = gateway.monitor_position(position, hit_tp1)
    assert result.exit_reason is None
    assert result.partial_exit is not None
    reduced = result.position
    assert not reduced.is_closed
    assert reduced.targets_hit == 1
    assert reduced.quantity == position.quantity - result.partial_exit.quantity
    assert result.partial_exit.quantity <= position.initial_quantity * Decimal("0.5")
    assert reduced.stop_price == position.entry_execution_price  # dời về hòa vốn
    # Theo dõi lại đúng nến cũ không chốt thêm.
    again = gateway.monitor_position(reduced, hit_tp1)
    assert again.position.quantity == reduced.quantity
    # Nến sau chạm giá vào → stop hòa vốn đóng phần còn lại; TP2 không còn.
    back = _bar(
        "back-to-entry",
        31,
        open_price=str(tp1),
        high=str(tp1),
        low=str(position.entry_execution_price - 1),
        close=str(position.entry_execution_price),
    )
    closed = gateway.monitor_position(reduced, back)
    assert closed.exit_reason == "STOP_LOSS"
    assert closed.position.is_closed
    assert tp2 > tp1


def test_tp1_then_tp2_closes_with_take_profit() -> None:
    plan = _laddered_plan()
    gateway = _gateway()
    position = _open(gateway, plan)
    tp1, tp2 = (target.price for target in plan.targets)
    first = gateway.monitor_position(
        position,
        _bar("tp1", 30, open_price=str(tp1 - 1), high=str(tp1), low=str(tp1 - 1), close=str(tp1)),
    )
    second = gateway.monitor_position(
        first.position,
        _bar("tp2", 31, open_price=str(tp1), high=str(tp2), low=str(tp1), close=str(tp2)),
    )
    assert second.exit_reason == "TAKE_PROFIT"
    assert second.position.is_closed
    assert second.position.targets_hit == 2
    assert second.exit_reference_price == tp2


def test_same_bar_rules_stop_first_and_multi_target_is_conservative() -> None:
    plan = _laddered_plan()
    position = _open(_gateway(), plan)
    tp1, tp2 = (target.price for target in plan.targets)
    both = monitor_position(
        position,
        _bar(
            "both",
            30,
            open_price=str(tp1 - 1),
            high=str(tp2),
            low=str(plan.stop_price - 1),
            close=str(tp1),
        ),
    )
    assert both.exit_reason == "STOP_LOSS"  # stop vẫn ưu tiên khi cùng nến
    sweep = monitor_position(
        position,
        _bar(
            "sweep",
            31,
            open_price=str(tp1 - 1),
            high=str(tp2 + 1),
            low=str(tp1 - 1),
            close=str(tp2),
        ),
    )
    # Chạm cả TP1 và TP2 trong một nến: đóng hết ở giá TP1 (bảo thủ).
    assert sweep.exit_reason == "TAKE_PROFIT"
    assert sweep.exit_reference_price == tp1


def test_legacy_position_without_targets_is_unchanged() -> None:
    position = replace(
        _open(_gateway(), _plan()), stop_price=Decimal("98"), target_price=Decimal("102")
    )
    assert position.targets == ()
    result = monitor_position(position, _bar("hit", 30, high="103", low="99"))
    assert result.exit_reason == "TAKE_PROFIT"
    assert result.partial_exit is None


def test_checkpoint_roundtrip_keeps_ladder_and_partial_state() -> None:
    plan = _laddered_plan()
    gateway = _gateway()
    position = _open(gateway, plan)
    tp1 = plan.targets[0].price
    reduced = gateway.monitor_position(
        position,
        _bar("tp1", 30, open_price=str(tp1 - 1), high=str(tp1), low=str(tp1 - 1), close=str(tp1)),
    ).position
    snapshot = gateway.export_state()
    restored = PaperExecutionGateway.restore(gateway.policy, snapshot)
    assert restored.export_state() == snapshot
    assert restored.position(reduced.intent_id) == reduced
    assert restored.position(reduced.intent_id).targets_hit == 1


def test_legacy_checkpoint_without_ladder_fields_still_restores() -> None:
    import json

    gateway = _gateway()
    position = _open(gateway, _plan())
    snapshot = gateway.export_state()
    body = json.loads(snapshot.payload)
    text = json.dumps(body)
    # Checkpoint cũ không có khóa thang chốt lời trong plan/vị thế.
    assert '"targets"' not in text
    assert '"targets_hit"' not in text
    restored = PaperExecutionGateway.restore(gateway.policy, snapshot)
    assert restored.position(position.intent_id) == gateway.position(position.intent_id)


def test_ledger_plan_payload_matches_checkpoint_plan_json_with_and_without_ladder() -> None:
    """Recovery so `trade_plan.payload` (repo risk) với plan trong checkpoint — phải trùng khít.

    Hồi quy bắt được ở RT1 E2E: payload DB có `"targets": []` cho plan cũ trong khi checkpoint bỏ
    khóa này → mọi lệnh cũ bị `PAPER_COMMAND_REJECTED` khi khớp.
    """
    from trading_bot.adapters.database.repositories.risk_reservations import _canonical_value
    from trading_bot.domain.execution.paper_state import _plan_to_json

    for plan in (_plan(), _laddered_plan()):
        assert _canonical_value(plan) == _plan_to_json(plan)
    assert "targets" not in _canonical_value(_plan())  # type: ignore[operator]
