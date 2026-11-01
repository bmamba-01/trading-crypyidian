from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from trading_bot.domain.execution.venue_orders import (
    ReconciliationOutcome,
    ReconciliationSource,
    SubmissionOutcome,
    VenueOrderBook,
    VenueOrderConflict,
    VenueOrderIntent,
    VenueOrderState,
    VenueOrderUpdate,
    VenueReconciliation,
)

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)


def intent(**overrides: object) -> VenueOrderIntent:
    values: dict[str, object] = {
        "intent_id": uuid4(),
        "workspace_id": "workspace-a",
        "account_id": "account-a",
        "idempotency_key": "plan-123-entry",
        "reservation_id": uuid4(),
        "plan_hash": "a" * 64,
        "settings_version": "settings-v4",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "quantity": Decimal("0.01"),
        "client_order_id": "crypidian-123-entry",
        "created_at": NOW,
    }
    values.update(overrides)
    return VenueOrderIntent(**values)  # type: ignore[arg-type]


def test_idempotency_returns_existing_intent_and_rejects_different_payload() -> None:
    book = VenueOrderBook()
    original = intent()

    assert book.prepare(original) is original
    duplicate = book.prepare(
        replace(
            original,
            intent_id=uuid4(),
            reservation_id=uuid4(),
            request_fingerprint="",
        )
    )
    assert duplicate is original
    decimal_equivalent = book.prepare(
        replace(
            original,
            intent_id=uuid4(),
            quantity=Decimal("0.010"),
            request_fingerprint="",
        )
    )
    assert decimal_equivalent is original

    with pytest.raises(VenueOrderConflict, match="fingerprint"):
        book.prepare(
            replace(
                original,
                intent_id=uuid4(),
                quantity=Decimal("0.02"),
                request_fingerprint="",
            )
        )


def test_ambiguous_submission_becomes_unknown_and_keeps_risk_reserved() -> None:
    order = intent().begin_submission(at=NOW + timedelta(seconds=1))
    unknown = order.resolve_submission(
        outcome=SubmissionOutcome.UNKNOWN,
        observed_at=NOW + timedelta(seconds=2),
    )

    assert unknown.state is VenueOrderState.UNKNOWN
    assert unknown.reservation_action == "RETAIN"
    with pytest.raises(VenueOrderConflict, match="CREATED/RETRYABLE"):
        unknown.begin_submission(at=NOW + timedelta(seconds=3))


def test_only_authoritative_absence_allows_retry_with_same_intent() -> None:
    order = intent().begin_submission(at=NOW + timedelta(seconds=1))
    unknown = order.resolve_submission(
        outcome=SubmissionOutcome.UNKNOWN,
        observed_at=NOW + timedelta(seconds=2),
    )
    absent = VenueReconciliation(
        outcome=ReconciliationOutcome.ORDER_ABSENT,
        source=ReconciliationSource.ORDER_QUERY_AND_TRADE_HISTORY,
        source_ref="reconcile-1",
        workspace_id=order.workspace_id,
        account_id=order.account_id,
        client_order_id=order.client_order_id,
        checked_at=NOW + timedelta(seconds=5),
        cumulative_filled=Decimal("0"),
    )
    retryable = unknown.resolve_unknown(absent)

    assert retryable.state is VenueOrderState.RETRYABLE
    assert retryable.client_order_id == order.client_order_id
    assert retryable.reservation_action == "RETAIN"
    with pytest.raises(VenueOrderConflict, match="Chưa đến thời điểm"):
        retryable.begin_submission(at=NOW + timedelta(seconds=4))
    assert retryable.begin_submission(at=NOW + timedelta(seconds=6)).attempt_count == 2


def test_submission_outcomes_distinguish_rejection_not_sent_and_unknown() -> None:
    base = intent().begin_submission(at=NOW + timedelta(seconds=1))
    not_sent = base.resolve_submission(
        outcome=SubmissionOutcome.NOT_SENT,
        observed_at=NOW + timedelta(seconds=2),
    )
    rejected = base.resolve_submission(
        outcome=SubmissionOutcome.DEFINITIVE_REJECTION,
        observed_at=NOW + timedelta(seconds=2),
    )

    assert not_sent.state is VenueOrderState.RETRYABLE
    assert rejected.state is VenueOrderState.REJECTED
    assert rejected.reservation_action == "RELEASE"


def test_retryable_failure_requires_a_future_backoff_deadline() -> None:
    base = intent().begin_submission(at=NOW + timedelta(seconds=1))

    with pytest.raises(VenueOrderConflict, match="retry_not_before bắt buộc"):
        base.resolve_submission(
            outcome=SubmissionOutcome.RETRYABLE_FAILURE,
            observed_at=NOW + timedelta(seconds=2),
        )
    with pytest.raises(VenueOrderConflict, match="sau observed_at"):
        base.resolve_submission(
            outcome=SubmissionOutcome.RETRYABLE_FAILURE,
            observed_at=NOW + timedelta(seconds=2),
            retry_not_before=NOW + timedelta(seconds=2),
        )

    delayed = base.resolve_submission(
        outcome=SubmissionOutcome.RETRYABLE_FAILURE,
        observed_at=NOW + timedelta(seconds=2),
        retry_not_before=NOW + timedelta(seconds=3),
    )
    assert delayed.state is VenueOrderState.RETRYABLE
    with pytest.raises(VenueOrderConflict, match="Chưa đến thời điểm"):
        delayed.begin_submission(at=NOW + timedelta(seconds=2, milliseconds=500))


def test_duplicate_and_out_of_order_updates_do_not_rollback_or_lose_fills() -> None:
    order = (
        intent()
        .begin_submission(at=NOW + timedelta(seconds=1))
        .resolve_submission(
            outcome=SubmissionOutcome.ACCEPTED,
            observed_at=NOW + timedelta(seconds=2),
            venue_order_id="venue-1",
        )
    )
    fill = VenueOrderUpdate(
        event_id="trade-1",
        source=ReconciliationSource.USER_STREAM,
        observed_at=NOW + timedelta(seconds=4),
        venue_order_id="venue-1",
        cumulative_filled=Decimal("0.004"),
        status=VenueOrderState.PARTIALLY_FILLED,
    )
    partial = order.apply_update(fill)
    assert partial.apply_update(fill) is partial

    stale_open = VenueOrderUpdate(
        event_id="event-old",
        source=ReconciliationSource.USER_STREAM,
        observed_at=NOW + timedelta(seconds=3),
        venue_order_id="venue-1",
        cumulative_filled=Decimal("0"),
        status=VenueOrderState.ACKNOWLEDGED,
    )
    kept = partial.apply_update(stale_open)
    assert kept.state is VenueOrderState.PARTIALLY_FILLED
    assert kept.filled_quantity == Decimal("0.004")


def test_reused_event_id_with_different_facts_is_a_conflict() -> None:
    order = (
        intent()
        .begin_submission(at=NOW + timedelta(seconds=1))
        .resolve_submission(
            outcome=SubmissionOutcome.ACCEPTED,
            observed_at=NOW + timedelta(seconds=2),
            venue_order_id="venue-1",
        )
    )
    first = VenueOrderUpdate(
        event_id="event-1",
        source=ReconciliationSource.USER_STREAM,
        observed_at=NOW + timedelta(seconds=3),
        venue_order_id="venue-1",
        cumulative_filled=Decimal("0"),
        status=VenueOrderState.ACKNOWLEDGED,
    )
    changed = replace(
        first, cumulative_filled=Decimal("0.001"), status=VenueOrderState.PARTIALLY_FILLED
    )

    applied = order.apply_update(first)
    with pytest.raises(VenueOrderConflict, match="payload khác"):
        applied.apply_update(changed)


def test_unknown_remains_unknown_after_stream_update_until_reconciliation() -> None:
    unknown = (
        intent()
        .begin_submission(at=NOW + timedelta(seconds=1))
        .resolve_submission(
            outcome=SubmissionOutcome.UNKNOWN,
            observed_at=NOW + timedelta(seconds=2),
        )
    )
    updated = unknown.apply_update(
        VenueOrderUpdate(
            event_id="trade-1",
            source=ReconciliationSource.USER_STREAM,
            observed_at=NOW + timedelta(seconds=3),
            venue_order_id="venue-1",
            cumulative_filled=Decimal("0.004"),
            status=VenueOrderState.PARTIALLY_FILLED,
        )
    )

    assert updated.state is VenueOrderState.UNKNOWN
    assert updated.filled_quantity == Decimal("0.004")
    assert updated.reservation_action == "TRANSFER_FILLED_AND_RETAIN_REMAINDER"
    absent = VenueReconciliation(
        outcome=ReconciliationOutcome.ORDER_ABSENT,
        source=ReconciliationSource.ORDER_QUERY_AND_TRADE_HISTORY,
        source_ref="reconcile-absent",
        workspace_id=unknown.workspace_id,
        account_id=unknown.account_id,
        client_order_id=unknown.client_order_id,
        checked_at=NOW + timedelta(seconds=4),
        cumulative_filled=Decimal("0"),
    )
    with pytest.raises(VenueOrderConflict, match="venue order ID"):
        updated.resolve_unknown(absent)


def test_update_ordering_uses_absolute_utc_time_across_repeated_dst_hour() -> None:
    order = (
        intent()
        .begin_submission(at=NOW + timedelta(seconds=1))
        .resolve_submission(
            outcome=SubmissionOutcome.ACCEPTED,
            observed_at=NOW + timedelta(seconds=2),
            venue_order_id="venue-1",
        )
    )
    zone = ZoneInfo("America/New_York")
    later = VenueOrderUpdate(
        event_id="event-later",
        source=ReconciliationSource.USER_STREAM,
        observed_at=datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1),
        venue_order_id="venue-1",
        cumulative_filled=Decimal("0"),
        status=VenueOrderState.CANCELED,
    )
    earlier = VenueOrderUpdate(
        event_id="event-earlier",
        source=ReconciliationSource.USER_STREAM,
        observed_at=datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=0),
        venue_order_id="venue-1",
        cumulative_filled=Decimal("0"),
        status=VenueOrderState.ACKNOWLEDGED,
    )

    assert order.apply_update(later).apply_update(earlier).state is VenueOrderState.CANCELED


def test_late_distinct_fill_updates_quantity_without_reopening_canceled_order() -> None:
    order = (
        intent()
        .begin_submission(at=NOW + timedelta(seconds=1))
        .resolve_submission(
            outcome=SubmissionOutcome.ACCEPTED,
            observed_at=NOW + timedelta(seconds=2),
            venue_order_id="venue-1",
        )
    )
    canceled = order.apply_update(
        VenueOrderUpdate(
            event_id="cancel-event",
            source=ReconciliationSource.USER_STREAM,
            observed_at=NOW + timedelta(seconds=5),
            venue_order_id="venue-1",
            cumulative_filled=Decimal("0"),
            status=VenueOrderState.CANCELED,
        )
    )
    late_fill = canceled.apply_update(
        VenueOrderUpdate(
            event_id="late-trade-1",
            source=ReconciliationSource.USER_STREAM,
            observed_at=NOW + timedelta(seconds=4),
            venue_order_id="venue-1",
            cumulative_filled=Decimal("0.003"),
            status=VenueOrderState.PARTIALLY_FILLED,
        )
    )

    assert late_fill.state is VenueOrderState.CANCELED
    assert late_fill.filled_quantity == Decimal("0.003")
    assert late_fill.reservation_action == "TRANSFER_TO_EXPOSURE"


def test_unknown_can_only_resolve_from_typed_reconciliation() -> None:
    order = (
        intent()
        .begin_submission(at=NOW + timedelta(seconds=1))
        .resolve_submission(
            outcome=SubmissionOutcome.UNKNOWN,
            observed_at=NOW + timedelta(seconds=2),
        )
    )
    evidence = VenueReconciliation(
        outcome=ReconciliationOutcome.ORDER_OPEN,
        source=ReconciliationSource.REST_ORDER_QUERY,
        source_ref="query-1",
        workspace_id=order.workspace_id,
        account_id=order.account_id,
        client_order_id=order.client_order_id,
        checked_at=NOW + timedelta(seconds=3),
        cumulative_filled=Decimal("0"),
        venue_order_id="venue-1",
    )

    resolved = order.resolve_unknown(evidence)
    assert resolved.state is VenueOrderState.ACKNOWLEDGED
    assert resolved.venue_order_id == "venue-1"


def test_venue_intent_rejects_non_demo_mode_and_invalid_fill_amounts() -> None:
    with pytest.raises(VenueOrderConflict, match="DEMO"):
        intent(mode="LIVE")
    with pytest.raises(VenueOrderConflict, match="quantity"):
        intent(quantity=Decimal("NaN"))
    with pytest.raises(VenueOrderConflict, match="limit_price"):
        intent(order_type="LIMIT")


def test_filled_reconciliation_requires_exact_requested_quantity() -> None:
    unknown = (
        intent()
        .begin_submission(at=NOW + timedelta(seconds=1))
        .resolve_submission(
            outcome=SubmissionOutcome.UNKNOWN,
            observed_at=NOW + timedelta(seconds=2),
        )
    )
    evidence = VenueReconciliation(
        outcome=ReconciliationOutcome.FILLED,
        source=ReconciliationSource.REST_ORDER_QUERY,
        source_ref="query-filled",
        workspace_id=unknown.workspace_id,
        account_id=unknown.account_id,
        client_order_id=unknown.client_order_id,
        checked_at=NOW + timedelta(seconds=3),
        cumulative_filled=Decimal("0.004"),
        venue_order_id="venue-1",
    )

    with pytest.raises(VenueOrderConflict, match="requested quantity"):
        unknown.resolve_unknown(evidence)


@pytest.mark.parametrize(
    "scope",
    [
        {"workspace_id": "workspace-b"},
        {"account_id": "account-b"},
        {"client_order_id": "other-client-order"},
    ],
)
def test_absence_evidence_must_bind_to_exact_intent_scope(scope: dict[str, str]) -> None:
    unknown = (
        intent()
        .begin_submission(at=NOW + timedelta(seconds=1))
        .resolve_submission(
            outcome=SubmissionOutcome.UNKNOWN,
            observed_at=NOW + timedelta(seconds=2),
        )
    )
    binding = {
        "workspace_id": unknown.workspace_id,
        "account_id": unknown.account_id,
        "client_order_id": unknown.client_order_id,
    }
    binding.update(scope)
    evidence = VenueReconciliation(
        outcome=ReconciliationOutcome.ORDER_ABSENT,
        source=ReconciliationSource.ORDER_QUERY_AND_TRADE_HISTORY,
        source_ref="query-absent",
        checked_at=NOW + timedelta(seconds=3),
        cumulative_filled=Decimal("0"),
        **binding,
    )

    with pytest.raises(VenueOrderConflict, match="intent scope"):
        unknown.resolve_unknown(evidence)


def test_order_book_only_prepares_new_created_intents() -> None:
    submitting = intent().begin_submission(at=NOW + timedelta(seconds=1))

    with pytest.raises(VenueOrderConflict, match="CREATED"):
        VenueOrderBook().prepare(submitting)


def test_intent_constructor_rejects_impossible_state_attempt_combinations() -> None:
    with pytest.raises(VenueOrderConflict, match="CREATED"):
        intent(state=VenueOrderState.CREATED, attempt_count=1)
    with pytest.raises(VenueOrderConflict, match="attempt"):
        intent(state=VenueOrderState.SUBMITTING, attempt_count=0)


@pytest.mark.parametrize(
    ("state", "venue_order_id"),
    [
        (VenueOrderState.REJECTED, None),
        (VenueOrderState.CANCELED, "seeded-order-id"),
        (VenueOrderState.EXPIRED, "seeded-order-id"),
    ],
)
def test_intent_constructor_rejects_seeded_terminal_states(
    state: VenueOrderState, venue_order_id: str | None
) -> None:
    with pytest.raises(VenueOrderConflict, match="chuyển trạng thái"):
        intent(
            state=state,
            attempt_count=1,
            last_attempt_at=NOW + timedelta(seconds=1),
            state_changed_at=NOW + timedelta(seconds=2),
            venue_order_id=venue_order_id,
        )


@pytest.mark.parametrize(
    "state",
    [VenueOrderState.REJECTED, VenueOrderState.CANCELED, VenueOrderState.EXPIRED],
)
def test_dataclass_copy_cannot_forge_reservation_releasing_transition(
    state: VenueOrderState,
) -> None:
    acknowledged = (
        intent()
        .begin_submission(at=NOW + timedelta(seconds=1))
        .resolve_submission(
            outcome=SubmissionOutcome.ACCEPTED,
            observed_at=NOW + timedelta(seconds=2),
            venue_order_id="venue-1",
        )
    )

    with pytest.raises(VenueOrderConflict, match="chuyển trạng thái"):
        replace(acknowledged, state=state, state_changed_at=NOW + timedelta(seconds=3))
