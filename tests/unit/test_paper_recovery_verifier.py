import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from tests.unit.test_paper_execution import _bar, _gateway, _plan

from trading_bot.application.paper_recovery import (
    PaperRecoveryConflict,
    verify_paper_ledger_consistency,
)
from trading_bot.domain.execution.paper import PaperExecutionGateway, PaperExecutionPolicy

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


def _state():
    plan = _plan()
    gateway = PaperExecutionGateway(
        PaperExecutionPolicy(
            "paper-recovery",
            timedelta(0),
            Decimal("0.001"),
            Decimal("0.001"),
            Decimal("0.001"),
        )
    )
    submitted_at = plan.source_signal_at + timedelta(seconds=3)
    order = gateway.submit(
        plan, "recovery-order", submitted_at=submitted_at, eligible_after=submitted_at
    )
    return gateway.export_state(), order, plan


def test_restore_rejects_checkpoint_order_without_scoped_ledger_intent() -> None:
    snapshot, _, _ = _state()

    with pytest.raises(PaperRecoveryConflict, match="intent ledger"):
        verify_paper_ledger_consistency(
            snapshot,
            intents=(),
            plans=(),
            fills=(),
            position_events=(),
            trades=(),
            costs=(),
            reservations=(),
        )


def test_restore_normalizes_order_direction_to_ledger_side() -> None:
    snapshot, order, plan = _state()
    workspace_id = uuid4()
    account_id = uuid4()
    decision_id = uuid4()
    reservation_id = uuid4()
    body = json.loads(snapshot.payload)
    plan_payload = body["plans"][order.intent_id]
    intent = SimpleNamespace(
        workspace_id=workspace_id,
        account_id=account_id,
        id=uuid4(),
        idempotency_key=order.idempotency_key,
        plan_hash=plan.plan_hash,
        symbol=order.symbol,
        side=order.direction.upper(),
        quantity=order.requested_quantity,
        filled_quantity=Decimal(0),
        status=order.status.value,
        settings_version_id=plan.settings_version_id,
        strategy_release_id=uuid4(),
        strategy_release_hash=plan.release_content_hash,
        feature_snapshot_id="b" * 64,
        candidate_id=plan.candidate_id,
        risk_decision_id=decision_id,
        risk_reservation_id=reservation_id,
        quote_asset="USDT",
        evidence={
            "settings_hash": plan.settings_hash,
            "paper_execution_profile_hash": "d" * 64,
        },
    )
    reservation = SimpleNamespace(
        workspace_id=workspace_id,
        account_id=account_id,
        id=reservation_id,
        risk_decision_id=decision_id,
        plan_hash=plan.plan_hash,
        status="RESERVED",
    )

    verify_paper_ledger_consistency(
        snapshot,
        intents=(intent,),
        plans=(SimpleNamespace(plan_hash=plan.plan_hash, payload=plan_payload),),
        fills=(),
        position_events=(),
        trades=(),
        costs=(),
        reservations=(reservation,),
    )

    orphan_event = SimpleNamespace(
        intent_id=intent.id,
        event_key="orphan-open",
        event_type="OPENED",
        quantity=Decimal("1"),
    )
    with pytest.raises(PaperRecoveryConflict, match="Position/trade"):
        verify_paper_ledger_consistency(
            snapshot,
            intents=(intent,),
            plans=(SimpleNamespace(plan_hash=plan.plan_hash, payload=plan_payload),),
            fills=(),
            position_events=(orphan_event,),
            trades=(),
            costs=(),
            reservations=(reservation,),
        )


def test_restore_checks_open_trade_totals_against_cost_ledger() -> None:
    gateway = _gateway()
    plan = _plan()
    submitted_at = plan.source_signal_at + timedelta(seconds=3)
    order = gateway.submit(
        plan,
        "open-trade-recovery",
        submitted_at=submitted_at,
        eligible_after=submitted_at,
    )
    filled_order = gateway.apply_market_event(
        _bar("recovery-entry", 1), available_quantity=Decimal("0.4")
    )[0]
    position = gateway.position(order.intent_id)
    assert position is not None
    snapshot = gateway.export_state()
    body = json.loads(snapshot.payload)
    profile_hash = "d" * 64
    workspace_id = uuid4()
    account_id = uuid4()
    intent_id = uuid4()
    decision_id = uuid4()
    reservation_id = uuid4()
    trade_id = uuid4()
    lineage = {
        "settings_version_id": str(plan.settings_version_id),
        "strategy_release_id": str(plan.strategy_release_id),
        "strategy_release_hash": str(plan.release_content_hash),
        "feature_snapshot_id": "b" * 64,
        "candidate_id": str(plan.candidate_id),
        "quote_asset": "USDT",
        "risk_decision_id": str(decision_id),
        "risk_reservation_id": str(reservation_id),
        "plan_hash": plan.plan_hash,
        "paper_execution_profile_hash": profile_hash,
    }
    intent = SimpleNamespace(
        workspace_id=workspace_id,
        account_id=account_id,
        id=intent_id,
        idempotency_key=filled_order.idempotency_key,
        plan_hash=plan.plan_hash,
        symbol=filled_order.symbol,
        side=filled_order.direction.upper(),
        quantity=filled_order.requested_quantity,
        filled_quantity=filled_order.filled_quantity,
        status=filled_order.status.value,
        settings_version_id=plan.settings_version_id,
        strategy_release_id=plan.strategy_release_id,
        strategy_release_hash=plan.release_content_hash,
        feature_snapshot_id="b" * 64,
        candidate_id=plan.candidate_id,
        risk_decision_id=decision_id,
        risk_reservation_id=reservation_id,
        quote_asset="USDT",
        evidence={
            "settings_hash": plan.settings_hash,
            "paper_execution_profile_hash": profile_hash,
        },
    )
    ledger_fills = tuple(
        SimpleNamespace(
            intent_id=intent_id,
            fill_role="ENTRY",
            fill_key=fill.fill_id,
            bar_key=fill.bar_key,
            fill_policy_version=fill.fill_policy_version,
            quantity=fill.quantity,
            price=fill.execution_price,
            fee_amount=fill.fee_cost,
            slippage_amount=fill.slippage_cost,
            lineage=lineage,
        )
        for fill in filled_order.fills
    )
    ledger_events = tuple(
        SimpleNamespace(
            intent_id=intent_id,
            event_key=f"entry-fill:{fill.fill_id}",
            event_type="OPENED" if index == 0 else "INCREASED",
            quantity=sum((item.quantity for item in filled_order.fills[: index + 1]), Decimal(0)),
        )
        for index, fill in enumerate(filled_order.fills)
    )
    fee_total = sum((fill.fee_cost for fill in filled_order.fills), Decimal(0))
    costs = tuple(
        cost
        for fill in filled_order.fills
        for cost in (
            SimpleNamespace(
                trade_id=trade_id,
                cost_type="FEE",
                amount=fill.fee_cost,
                source_event_id=fill.fill_id,
            ),
            SimpleNamespace(
                trade_id=trade_id,
                cost_type="SLIPPAGE",
                amount=fill.slippage_cost,
                source_event_id=fill.fill_id,
            ),
        )
    )
    trade = SimpleNamespace(
        id=trade_id,
        intent_id=intent_id,
        status="OPEN",
        lineage=lineage,
        fee_total=Decimal(0),
        funding_total=Decimal(0),
        slippage_total=Decimal(0),
        total_cost=Decimal(0),
        gross_pnl=Decimal(0),
        net_pnl=Decimal(0),
    )
    reservation = SimpleNamespace(
        workspace_id=workspace_id,
        account_id=account_id,
        id=reservation_id,
        risk_decision_id=decision_id,
        plan_hash=plan.plan_hash,
        status="RESERVED",
    )

    ledger_values = {
        "intents": (intent,),
        "plans": (
            SimpleNamespace(plan_hash=plan.plan_hash, payload=body["plans"][order.intent_id]),
        ),
        "fills": ledger_fills,
        "position_events": ledger_events,
        "trades": (trade,),
        "costs": costs,
        "reservations": (reservation,),
    }
    verify_paper_ledger_consistency(snapshot, **ledger_values)

    trade.fee_total = fee_total
    with pytest.raises(PaperRecoveryConflict, match="Trade totals"):
        verify_paper_ledger_consistency(
            snapshot,
            **ledger_values,
        )


def test_restore_rejects_intent_with_mismatched_plan_or_reservation_lineage() -> None:
    snapshot, order, plan = _state()
    account_id = uuid4()
    workspace_id = uuid4()
    intent = SimpleNamespace(
        workspace_id=workspace_id,
        account_id=account_id,
        id=uuid4(),
        idempotency_key=order.idempotency_key,
        plan_hash="f" * 64,
        symbol=order.symbol,
        side=order.direction,
        quantity=order.requested_quantity,
        filled_quantity=Decimal(0),
        status=order.status.value,
        settings_version_id=uuid4(),
        strategy_release_id=uuid4(),
        strategy_release_hash="a" * 64,
        feature_snapshot_id="b" * 64,
        candidate_id="c" * 64,
        risk_decision_id=uuid4(),
        risk_reservation_id=uuid4(),
        quote_asset="USDT",
        evidence={"paper_execution_profile_hash": "d" * 64},
    )
    reservation = SimpleNamespace(
        workspace_id=workspace_id,
        account_id=account_id,
        id=uuid4(),
        risk_decision_id=intent.risk_decision_id,
        plan_hash=plan.plan_hash,
        status="RESERVED",
    )

    with pytest.raises(PaperRecoveryConflict, match="order"):
        verify_paper_ledger_consistency(
            snapshot,
            intents=(intent,),
            plans=(),
            fills=(),
            position_events=(),
            trades=(),
            costs=(),
            reservations=(reservation,),
        )


def test_stop_event_quantity_accepts_any_previously_open_quantity() -> None:
    """Lỗi 15/11/2026: vị thế đã đóng (quantity 0) có STOP_UPDATED ghi khi còn mở 21,76 bị coi là
    không khớp, làm bot kẹt RECOVERY_REQUIRED sau restart."""
    from trading_bot.application.paper_recovery import stop_event_quantity_valid

    opened = {Decimal("21.76")}
    assert stop_event_quantity_valid(
        Decimal("21.76"),
        open_quantities=opened,
        current_quantity=Decimal("0"),
        position_closed=True,
    )
    # Vị thế còn mở: mức hiện tại hợp lệ.
    assert stop_event_quantity_valid(
        Decimal("21.76"),
        open_quantities=opened,
        current_quantity=Decimal("21.76"),
        position_closed=False,
    )
    # Mức chưa từng mở, hoặc 0, vẫn bị từ chối (đối soát không bị nới thành vô điều kiện).
    assert not stop_event_quantity_valid(
        Decimal("5"), open_quantities=opened, current_quantity=Decimal("0"), position_closed=True
    )
    assert not stop_event_quantity_valid(
        Decimal("0"), open_quantities=opened, current_quantity=Decimal("0"), position_closed=True
    )
    # Sau một lần giảm: mức trước khi giảm và mức còn lại đều là mức từng mở.
    reduced = {Decimal("10"), Decimal("4")}
    for quantity in (Decimal("10"), Decimal("4")):
        assert stop_event_quantity_valid(
            quantity, open_quantities=reduced, current_quantity=Decimal("0"), position_closed=True
        )


def test_restore_quantizes_numeric18_for_db_scale_compatibility() -> None:
    """Kiểm tra đối soát lượng hóa scale 18 (ROUND_HALF_UP khớp NUMERIC(38, 18) của Postgres).

    Khi snapshot lưu chuỗi Decimal có hơn 18 chữ số thập phân (ví dụ adverse rate nhân giá ra
    .732500000000), PostgreSQL tự làm tròn sang 18 chữ số (.733). Đối soát phải lượng hóa
    khớp mà không báo lỗi lệch payload hay kẹt RECOVERY_REQUIRED.
    """
    gateway = _gateway()
    plan = _plan()
    submitted_at = plan.source_signal_at + timedelta(seconds=3)
    order = gateway.submit(
        plan,
        "scale18-recovery",
        submitted_at=submitted_at,
        eligible_after=submitted_at,
    )
    filled_order = gateway.apply_market_event(
        _bar("recovery-entry", 1), available_quantity=Decimal("0.4")
    )[0]
    position = gateway.position(order.intent_id)
    assert position is not None
    snapshot = gateway.export_state()
    body = json.loads(snapshot.payload)

    # Giả lập snapshot mang slippage_cost có hơn 18 chữ số
    high_precision_slip = "7.903426426792417732500000000"
    db_rounded_slip = Decimal("7.903426426792417733")
    body["orders"][0]["fills"][0]["slippage_cost"] = high_precision_slip
    body["positions"][0]["slippage_cost"] = high_precision_slip

    from types import SimpleNamespace as SN

    from trading_bot.domain.execution.paper_state import PaperExecutionSnapshot

    modified_snapshot = PaperExecutionSnapshot(
        schema_version=snapshot.schema_version,
        policy_hash=snapshot.policy_hash,
        payload=json.dumps(body),
        content_hash=snapshot.content_hash,
    )

    workspace_id = uuid4()
    account_id = uuid4()
    intent_id = uuid4()
    decision_id = uuid4()
    reservation_id = uuid4()
    trade_id = uuid4()
    profile_hash = "d" * 64

    lineage = {
        "settings_version_id": str(plan.settings_version_id),
        "strategy_release_id": str(plan.strategy_release_id),
        "strategy_release_hash": str(plan.release_content_hash),
        "feature_snapshot_id": "b" * 64,
        "candidate_id": str(plan.candidate_id),
        "quote_asset": "USDT",
        "risk_decision_id": str(decision_id),
        "risk_reservation_id": str(reservation_id),
        "plan_hash": plan.plan_hash,
        "paper_execution_profile_hash": profile_hash,
    }
    intent = SN(
        workspace_id=workspace_id,
        account_id=account_id,
        id=intent_id,
        idempotency_key=filled_order.idempotency_key,
        plan_hash=plan.plan_hash,
        symbol=filled_order.symbol,
        side=filled_order.direction.upper(),
        quantity=filled_order.requested_quantity,
        filled_quantity=filled_order.filled_quantity,
        status=filled_order.status.value,
        settings_version_id=plan.settings_version_id,
        strategy_release_id=plan.strategy_release_id,
        strategy_release_hash=plan.release_content_hash,
        feature_snapshot_id="b" * 64,
        candidate_id=plan.candidate_id,
        risk_decision_id=decision_id,
        risk_reservation_id=reservation_id,
        quote_asset="USDT",
        evidence={
            "settings_hash": plan.settings_hash,
            "paper_execution_profile_hash": profile_hash,
        },
    )
    fill = filled_order.fills[0]
    ledger_fills = (
        SN(
            intent_id=intent_id,
            fill_role="ENTRY",
            fill_key=fill.fill_id,
            bar_key=fill.bar_key,
            fill_policy_version=fill.fill_policy_version,
            quantity=fill.quantity,
            price=fill.execution_price,
            fee_amount=fill.fee_cost,
            slippage_amount=db_rounded_slip,
            lineage=lineage,
        ),
    )
    ledger_events = (
        SN(
            intent_id=intent_id,
            event_key=f"entry-fill:{fill.fill_id}",
            event_type="OPENED",
            quantity=fill.quantity,
        ),
    )
    costs = (
        SN(
            trade_id=trade_id,
            cost_type="FEE",
            amount=fill.fee_cost,
            source_event_id=fill.fill_id,
        ),
        SN(
            trade_id=trade_id,
            cost_type="SLIPPAGE",
            amount=db_rounded_slip,
            source_event_id=fill.fill_id,
        ),
    )
    reservation = SN(
        id=reservation_id,
        workspace_id=workspace_id,
        account_id=account_id,
        risk_decision_id=decision_id,
        plan_hash=plan.plan_hash,
        status="RESERVED",
    )
    trade = SN(
        id=trade_id,
        intent_id=intent_id,
        status="OPEN",
        lineage=lineage,
        fee_total=Decimal(0),
        funding_total=Decimal(0),
        slippage_total=Decimal(0),
        total_cost=Decimal(0),
        gross_pnl=Decimal(0),
        net_pnl=Decimal(0),
    )

    verify_paper_ledger_consistency(
        modified_snapshot,
        intents=(intent,),
        plans=(SN(plan_hash=plan.plan_hash, payload=body["plans"][order.intent_id]),),
        fills=ledger_fills,
        position_events=ledger_events,
        trades=(trade,),
        costs=costs,
        reservations=(reservation,),
    )
