from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier

import pytest

from trading_bot.domain.risk.engine import RiskDecision
from trading_bot.domain.risk.reservations import (
    ReservationConflict,
    ReservationOutcome,
    ReservationReleaseReason,
    ReservationStatus,
    RiskReservationBook,
)

# Đồng hồ cố định: candidate hết hạn lúc 13:05 UTC; đồng hồ thật làm test hỏng sau mốc đó.
_FIXED_NOW = datetime(2026, 10, 24, 13, tzinfo=UTC)


def _decision(
    *,
    approved: bool = True,
    risk: str = "60",
    baseline: str = "20",
    ceiling: str = "100",
    plan_hash: str = "plan-a",
    account: str = "account-a",
) -> RiskDecision:
    return RiskDecision(
        approved=approved,
        reason_code="APPROVED" if approved else "RISK_REJECTED",
        detail_vi="Đã kiểm tra giới hạn rủi ro." if approved else "Bị từ chối.",
        plan_hash=plan_hash,
        candidate_expires_at=_FIXED_NOW + timedelta(minutes=5),
        workspace_id="workspace-a",
        account_id=account,
        settings_version_id="settings-v1",
        risk_profile_version_id="risk-v1",
        requested_risk=Decimal(risk),
        symbol="BTCUSDT",
        requested_notional=Decimal("100"),
        open_notional=Decimal("0"),
        symbol_open_notional=Decimal("0"),
        max_notional=Decimal("1000"),
        max_symbol_notional=Decimal("1000"),
        open_risk=Decimal(baseline),
        max_aggregate_open_risk=Decimal(ceiling),
        open_position_count=0,
        max_positions=10,
        pending_reservation_count=0,
        available_margin=Decimal("1000"),
        requested_initial_margin=Decimal("100"),
        pending_reservation_initial_margin=Decimal("0"),
    )


def test_reservation_is_idempotent_and_rejects_key_reuse_for_another_plan() -> None:
    book = RiskReservationBook(clock=lambda: _FIXED_NOW)
    first = book.reserve(_decision(), "intent-1")
    repeated = book.reserve(_decision(), "intent-1")

    assert first == repeated
    assert first.status is ReservationStatus.RESERVED
    assert first.reserved_risk == Decimal("60")
    with pytest.raises(ReservationConflict, match="idempotency"):
        book.reserve(_decision(plan_hash="plan-b"), "intent-1")


def test_unknown_order_keeps_reserved_risk_until_reconciliation() -> None:
    book = RiskReservationBook(clock=lambda: _FIXED_NOW)
    first = book.reserve(_decision(), "intent-1")
    unknown = book.mark_unknown(first.reservation_id)

    assert unknown.status is ReservationStatus.UNKNOWN
    assert book.active_risk("workspace-a", "account-a") == Decimal("60")
    assert (
        book.reconcile(first.reservation_id, outcome=ReservationOutcome.UNRESOLVED).status
        is ReservationStatus.UNKNOWN
    )
    assert book.active_risk("workspace-a", "account-a") == Decimal("60")
    assert (
        book.reconcile(first.reservation_id, outcome=ReservationOutcome.ORDER_ABSENT).status
        is ReservationStatus.RELEASED
    )
    assert book.active_risk("workspace-a", "account-a") == Decimal("0")


def test_reconciled_fill_transfers_reservation_after_open_risk_is_recorded() -> None:
    book = RiskReservationBook(clock=lambda: _FIXED_NOW)
    reservation = book.reserve(_decision(), "intent-filled")
    book.mark_unknown(reservation.reservation_id)

    transferred = book.reconcile(
        reservation.reservation_id,
        outcome=ReservationOutcome.OPEN_RISK_RECORDED,
    )

    assert transferred.status is ReservationStatus.TRANSFERRED
    assert book.active_risk("workspace-a", "account-a") == Decimal("0")
    assert book.active_count("workspace-a", "account-a") == 0


def test_rejected_decision_cannot_reserve_and_scope_isolated() -> None:
    book = RiskReservationBook(clock=lambda: _FIXED_NOW)
    with pytest.raises(ReservationConflict, match="approved"):
        book.reserve(_decision(approved=False), "intent-rejected")
    book.reserve(_decision(account="account-a"), "a")
    book.reserve(_decision(account="account-b"), "b")
    assert book.active_risk("workspace-a", "account-a") == Decimal("60")
    assert book.active_risk("workspace-a", "account-b") == Decimal("60")


def test_concurrent_reservations_never_exceed_remaining_aggregate_ceiling() -> None:
    book = RiskReservationBook(clock=lambda: _FIXED_NOW)
    barrier = Barrier(2)

    def reserve(key: str) -> bool:
        barrier.wait()
        try:
            book.reserve(_decision(), key)
            return True
        except ReservationConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(reserve, ("intent-a", "intent-b")))
    assert sum(outcomes) == 1
    assert book.active_risk("workspace-a", "account-a") == Decimal("60")


def test_reservation_also_enforces_position_count_concurrently() -> None:
    book = RiskReservationBook(clock=lambda: _FIXED_NOW)
    one_slot_left = replace(
        _decision(risk="10", baseline="0", ceiling="100"),
        open_position_count=1,
        max_positions=2,
    )
    book.reserve(one_slot_left, "first")
    with pytest.raises(ReservationConflict, match="Số vị thế"):
        book.reserve(one_slot_left, "second")


def test_reserved_can_release_only_after_confirmed_pre_submit_cancel_or_expiry() -> None:
    expiry = _decision().candidate_expires_at
    current = [expiry - timedelta(microseconds=1)]
    book = RiskReservationBook(clock=lambda: current[0])
    reservation = book.reserve(replace(_decision(), candidate_expires_at=expiry), "intent-expiring")

    with pytest.raises(ReservationConflict, match="đã được submit"):
        book.release_before_submit(
            reservation.reservation_id,
            reason=ReservationReleaseReason.EXPIRED_BEFORE_SUBMIT,
            order_was_submitted=True,
        )

    current[0] = expiry
    released = book.release_before_submit(
        reservation.reservation_id,
        reason=ReservationReleaseReason.EXPIRED_BEFORE_SUBMIT,
        order_was_submitted=False,
    )

    assert released.status is ReservationStatus.RELEASED
    assert released.resolution_reason is ReservationReleaseReason.EXPIRED_BEFORE_SUBMIT
    assert book.active_risk("workspace-a", "account-a") == Decimal("0")


def test_expiry_is_persisted_and_releases_only_reserved_items_at_exact_boundary() -> None:
    expiry = datetime(2026, 10, 22, 13, 5, tzinfo=UTC)
    book = RiskReservationBook(clock=lambda: expiry - timedelta(microseconds=1))
    decision = replace(_decision(), candidate_expires_at=expiry)
    reservation = book.reserve(decision, "intent-expiring")

    assert reservation.expires_at == expiry
    assert book.expire_due(now=expiry) == 1
    assert book.active_risk("workspace-a", "account-a") == Decimal("0")
    with pytest.raises(ReservationConflict, match="terminal"):
        book.reserve(decision, "intent-expiring")


def test_unknown_reservation_does_not_expire_and_keeps_held_risk() -> None:
    expiry = datetime(2026, 10, 22, 13, 5, tzinfo=UTC)
    book = RiskReservationBook(clock=lambda: expiry - timedelta(seconds=1))
    reservation = book.reserve(replace(_decision(), candidate_expires_at=expiry), "intent-unknown")
    book.mark_unknown(reservation.reservation_id)

    assert book.expire_due(now=expiry + timedelta(days=1)) == 0
    assert book.active_risk("workspace-a", "account-a") == Decimal("60")


def test_reservation_book_enforces_available_margin_including_held_reservations() -> None:
    book = RiskReservationBook(clock=lambda: _FIXED_NOW)
    decision = replace(
        _decision(risk="1", baseline="0", ceiling="100"),
        available_margin=Decimal("100"),
        requested_initial_margin=Decimal("60"),
    )
    book.reserve(decision, "first")
    with pytest.raises(ReservationConflict, match="margin khả dụng"):
        book.reserve(replace(decision, requested_initial_margin=Decimal("41")), "second")


def test_reconcile_is_idempotent_only_for_the_same_terminal_outcome() -> None:
    book = RiskReservationBook(clock=lambda: _FIXED_NOW)
    reservation = book.reserve(_decision(), "intent-terminal")
    book.mark_unknown(reservation.reservation_id)

    released = book.reconcile(reservation.reservation_id, outcome=ReservationOutcome.ORDER_ABSENT)
    assert (
        book.reconcile(reservation.reservation_id, outcome=ReservationOutcome.ORDER_ABSENT)
        == released
    )
    with pytest.raises(ReservationConflict, match="mâu thuẫn"):
        book.reconcile(reservation.reservation_id, outcome=ReservationOutcome.OPEN_RISK_RECORDED)


def test_release_rejects_submitted_flag_even_after_prior_release() -> None:
    book = RiskReservationBook(clock=lambda: _FIXED_NOW)
    reservation = book.reserve(_decision(), "intent-released")
    book.release_before_submit(
        reservation.reservation_id,
        reason=ReservationReleaseReason.CANCELLED_BEFORE_SUBMIT,
        order_was_submitted=False,
    )

    with pytest.raises(ReservationConflict, match="đã được submit"):
        book.release_before_submit(
            reservation.reservation_id,
            reason=ReservationReleaseReason.CANCELLED_BEFORE_SUBMIT,
            order_was_submitted=True,
        )
