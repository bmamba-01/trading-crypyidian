from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.unit.test_paper_execution import _bar, _gateway, _plan

from trading_bot.domain.execution.paper import IntentConflict, PaperExecutionGateway

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


def test_gateway_snapshot_roundtrip_preserves_partial_position_and_continuation() -> None:
    plan = _plan()
    original = _gateway()
    order = original.submit(plan, "recovery-order", submitted_at=NOW, eligible_after=NOW)
    original.apply_market_event(_bar("recovery-entry", 1), available_quantity=Decimal("0.4"))
    position = original.position(order.intent_id)
    assert position is not None
    original.apply_funding_event(position.position_id, "funding-1", Decimal("0.03"))

    snapshot = original.export_state()
    recovered = PaperExecutionGateway.restore(original.policy, snapshot)

    assert recovered.export_state() == snapshot
    assert recovered.order(order.intent_id) == original.order(order.intent_id)
    assert recovered.position(order.intent_id) == original.position(order.intent_id)
    expected = original.apply_market_event(_bar("recovery-next", 2))
    actual = recovered.apply_market_event(_bar("recovery-next", 2))
    assert actual == expected
    assert recovered.position(order.intent_id) == original.position(order.intent_id)


def test_gateway_restore_rejects_snapshot_with_changed_content() -> None:
    gateway = _gateway()
    gateway.submit(_plan(), "recovery-corrupt", submitted_at=NOW)
    snapshot = gateway.export_state()
    changed = replace(
        snapshot,
        payload=snapshot.payload.replace('"entries_paused":false', '"entries_paused":true'),
    )

    with pytest.raises(IntentConflict, match="hash"):
        PaperExecutionGateway.restore(gateway.policy, changed)


def test_gateway_restore_rejects_a_different_fill_policy() -> None:
    gateway = _gateway()
    gateway.submit(_plan(), "active-policy-drift", submitted_at=NOW)
    snapshot = gateway.export_state()
    changed_policy = replace(gateway.policy, fee_rate=Decimal("0.002"))

    with pytest.raises(IntentConflict, match="policy"):
        PaperExecutionGateway.restore(changed_policy, snapshot)


def test_gateway_allows_new_policy_after_old_orders_are_terminal() -> None:
    gateway = _gateway()
    order = gateway.submit(_plan(), "terminal-policy-drift", submitted_at=NOW)
    gateway.cancel_unfilled(order.intent_id)
    snapshot = gateway.export_state()
    changed_policy = replace(gateway.policy, fee_rate=Decimal("0.002"))

    recovered = PaperExecutionGateway.restore(changed_policy, snapshot)

    assert recovered.order(order.intent_id).fill_policy_version == gateway.policy.version_id


def test_gateway_restore_rejects_unsupported_snapshot_schema() -> None:
    gateway = _gateway()
    snapshot = replace(gateway.export_state(), schema_version=999)

    with pytest.raises(IntentConflict, match="schema"):
        PaperExecutionGateway.restore(gateway.policy, snapshot)


def test_gateway_rejects_same_market_bar_key_with_changed_payload() -> None:
    gateway = _gateway()
    gateway.submit(_plan(), "recovery-event-conflict", submitted_at=NOW)
    first = _bar("same-key", 1)
    gateway.apply_market_event(first)
    changed = replace(first, close=first.close + Decimal("1"))

    with pytest.raises(IntentConflict, match="payload"):
        gateway.apply_market_event(changed)


def test_gateway_rejects_unseen_market_events_out_of_order() -> None:
    gateway = _gateway()
    gateway.submit(_plan(), "recovery-event-order", submitted_at=NOW)
    gateway.apply_market_event(_bar("later", 2))

    with pytest.raises(IntentConflict, match="thứ tự"):
        gateway.apply_market_event(_bar("earlier", 1))


def test_account_snapshot_can_resume_second_intent_after_first_intent_same_bar() -> None:
    gateway = _gateway()
    first_plan = _plan()
    first = gateway.submit(first_plan, "first-idempotency", submitted_at=NOW, eligible_after=NOW)
    second = gateway.submit(first_plan, "second-idempotency", submitted_at=NOW, eligible_after=NOW)
    bar = _bar("shared-market-bar", 1)

    first_result = gateway.apply_market_event(bar, intent_id=first.intent_id)
    after_first = gateway.export_state()
    recovered = PaperExecutionGateway.restore(gateway.policy, after_first)
    second_result = recovered.apply_market_event(bar, intent_id=second.intent_id)

    assert len(first_result) == 1
    assert first_result[0].intent_id == first.intent_id
    assert first_result[0].filled_quantity == Decimal("0.4")
    assert len(second_result) == 1
    assert second_result[0].intent_id == second.intent_id
    assert second_result[0].filled_quantity == Decimal("0.4")
    assert recovered.order(first.intent_id) == first_result[0]


def test_market_event_fingerprint_is_scoped_per_intent() -> None:
    gateway = _gateway()
    first_plan = _plan()
    first = gateway.submit(first_plan, "first-scoped", submitted_at=NOW, eligible_after=NOW)
    second = gateway.submit(first_plan, "second-scoped", submitted_at=NOW, eligible_after=NOW)
    bar = _bar("shared-market-bar", 1)
    changed = replace(bar, close=bar.close + Decimal("1"))

    gateway.apply_market_event(bar, intent_id=first.intent_id)
    gateway.apply_market_event(bar, intent_id=second.intent_id)
    with pytest.raises(IntentConflict, match="payload"):
        gateway.apply_market_event(changed, intent_id=first.intent_id)
