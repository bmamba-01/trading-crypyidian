"""Unit tests cho cổng readiness startup/reconnect venue (WP-03D / CRY-294)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from trading_bot.application.venue_readiness_gate import VenueStartupReadinessGate
from trading_bot.domain.execution.venue_reconciliation import (
    VenueAuthoritativeSnapshot,
    VenueReconcileOutcome,
    reconcile_venue_startup,
)
from trading_bot.ports.account_activity import KnownAccountState

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
WS_ID = uuid4()
ACC_ID = uuid4()


def test_startup_readiness_gate_closed_by_default_and_requires_all_preconditions() -> None:
    """Gate đóng mặc định; chỉ mở khi reconcile MATCHED + risk policy duyệt + 0 incident mở."""
    gate = VenueStartupReadinessGate()
    assert gate.is_open is False

    from decimal import Decimal

    clean_snap = VenueAuthoritativeSnapshot(
        workspace_id=WS_ID,
        account_id=ACC_ID,
        environment="DEMO",
        observed_at=NOW,
        complete=True,
        cursor="c1",
        wallet_balance=Decimal("5000"),
        unrealized_pnl=Decimal("0"),
    )
    matched = reconcile_venue_startup(
        clean_snap,
        expected_workspace_id=WS_ID,
        expected_account_id=ACC_ID,
        expected_environment="DEMO",
        known_state=KnownAccountState(WS_ID, ACC_ID, frozenset(), ()),
        protected_symbols=frozenset(),
        as_of=NOW,
    )
    assert matched.outcome is VenueReconcileOutcome.MATCHED

    # Thiếu risk policy -> vẫn đóng
    assert (
        gate.evaluate(reconcile=matched, risk_policy_approved=False, open_incidents_count=0)
        is False
    )
    assert gate.reason_code == "RISK_POLICY_NOT_APPROVED"

    # Còn incident mở -> vẫn đóng
    assert (
        gate.evaluate(reconcile=matched, risk_policy_approved=True, open_incidents_count=1) is False
    )
    assert gate.reason_code == "OPEN_ACTIVITY_INCIDENTS_EXIST"

    # Đủ mọi điều kiện -> mở cổng
    assert (
        gate.evaluate(reconcile=matched, risk_policy_approved=True, open_incidents_count=0) is True
    )
    assert gate.is_open is True
    assert gate.reason_code == "VENUE_READY"
