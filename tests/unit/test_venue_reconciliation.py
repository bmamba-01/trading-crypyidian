"""Unit tests cho đối soát startup/reconnect venue WP-03D (CRY-294)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from trading_bot.domain.execution.live_preflight import (
    VenueOrderSummary,
    VenuePositionSnapshot,
)
from trading_bot.domain.execution.venue_reconciliation import (
    PendingUnknownIntent,
    VenueAuthoritativeSnapshot,
    VenueFillEvent,
    VenueReconcileOutcome,
    reconcile_venue_reconnect,
    reconcile_venue_startup,
)
from trading_bot.ports.account_activity import KnownAccountState, ObservedPosition

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
WS_ID = uuid4()
ACC_ID = uuid4()


def _snap(**overrides) -> VenueAuthoritativeSnapshot:
    base = {
        "workspace_id": WS_ID,
        "account_id": ACC_ID,
        "environment": "DEMO",
        "observed_at": NOW - timedelta(seconds=2),
        "complete": True,
        "cursor": "cur-100",
        "wallet_balance": Decimal("10000"),
        "unrealized_pnl": Decimal("50"),
        "positions": (),
        "open_orders": (),
        "algo_orders": (),
        "recent_fills": (),
        "history_coverage_from": NOW - timedelta(hours=2),
        "history_coverage_to": NOW,
    }
    base.update(overrides)
    return VenueAuthoritativeSnapshot(**base)


def _known(**overrides) -> KnownAccountState:
    base = {
        "workspace_id": WS_ID,
        "account_id": ACC_ID,
        "known_client_order_ids": frozenset(),
        "open_positions": (),
    }
    base.update(overrides)
    return KnownAccountState(**base)


def test_missing_stale_future_incomplete_and_scope_mismatch() -> None:
    """Snapshot thiếu, cũ, tương lai, incomplete hoặc sai scope -> STALE_INCOMPLETE."""
    for snap_arg in (
        None,
        _snap(complete=False),
        _snap(wallet_balance=None),
        _snap(observed_at=NOW - timedelta(seconds=45)),
        _snap(observed_at=NOW + timedelta(seconds=5)),
        _snap(account_id=uuid4()),
        _snap(environment="LIVE"),
    ):
        res = reconcile_venue_startup(
            snap_arg,
            expected_workspace_id=WS_ID,
            expected_account_id=ACC_ID,
            expected_environment="DEMO",
            known_state=_known(),
            protected_symbols=frozenset(),
            as_of=NOW,
        )
        assert res.outcome is VenueReconcileOutcome.STALE_INCOMPLETE
        assert res.entry_allowed is False
        assert res.can_transition_reservation is False


def test_foreign_position_foreign_order_quantity_mismatch_and_missing_protection() -> None:
    """Kiểm foreign position, foreign order, quantity mismatch và thiếu protective order."""
    pos = VenuePositionSnapshot(
        "BTCUSDT", "BOTH", Decimal("0.5"), Decimal("90000"), Decimal("90100"), Decimal("50")
    )
    # 1. Foreign/orphan position
    r_fpos = reconcile_venue_startup(
        _snap(positions=(pos,)),
        expected_workspace_id=WS_ID,
        expected_account_id=ACC_ID,
        expected_environment="DEMO",
        known_state=_known(),
        protected_symbols=frozenset({"BTCUSDT"}),
        as_of=NOW,
    )
    assert r_fpos.outcome is VenueReconcileOutcome.FOREIGN_ACTIVITY
    assert r_fpos.activity_classification is not None

    # 2. Foreign order
    ford = VenueOrderSummary(
        "BTCUSDT",
        "o-1",
        "manual-cid-1",
        "BUY",
        "LIMIT",
        Decimal("0.1"),
        Decimal("0"),
        Decimal("89000"),
        "NEW",
    )
    r_ford = reconcile_venue_startup(
        _snap(open_orders=(ford,)),
        expected_workspace_id=WS_ID,
        expected_account_id=ACC_ID,
        expected_environment="DEMO",
        known_state=_known(),
        protected_symbols=frozenset(),
        as_of=NOW,
    )
    assert r_ford.outcome is VenueReconcileOutcome.FOREIGN_ACTIVITY

    # 3. Quantity mismatch
    r_mis = reconcile_venue_startup(
        _snap(positions=(pos,)),
        expected_workspace_id=WS_ID,
        expected_account_id=ACC_ID,
        expected_environment="DEMO",
        known_state=_known(open_positions=(ObservedPosition("BTCUSDT", "LONG", Decimal("0.3")),)),
        protected_symbols=frozenset({"BTCUSDT"}),
        as_of=NOW,
    )
    assert r_mis.outcome is VenueReconcileOutcome.MISMATCH

    # 4. Missing protection
    r_prot = reconcile_venue_startup(
        _snap(positions=(pos,)),
        expected_workspace_id=WS_ID,
        expected_account_id=ACC_ID,
        expected_environment="DEMO",
        known_state=_known(open_positions=(ObservedPosition("BTCUSDT", "LONG", Decimal("0.5")),)),
        protected_symbols=frozenset(),
        as_of=NOW,
    )
    assert r_prot.outcome is VenueReconcileOutcome.PROTECTION_MISSING


def test_unknown_intent_absent_open_exposure_duplicate_fill_and_reconnect_gap() -> None:
    """Kiểm dedupe fill, ORDER_ABSENT, OPEN_EXPOSURE_RECORDED và reconnect stream gap."""
    pending = PendingUnknownIntent(uuid4(), "bot-cid-1", "BTCUSDT", NOW - timedelta(minutes=10))
    # 1. ORDER_ABSENT khi lịch sử phủ tới created_at và không thấy trên sàn
    r_abs = reconcile_venue_startup(
        _snap(),
        expected_workspace_id=WS_ID,
        expected_account_id=ACC_ID,
        expected_environment="DEMO",
        known_state=_known(),
        protected_symbols=frozenset(),
        unknown_intents=[pending],
        as_of=NOW,
    )
    assert r_abs.outcome is VenueReconcileOutcome.ORDER_ABSENT
    assert r_abs.can_transition_reservation is True

    # 2. Duplicate fill dedupe & OPEN_EXPOSURE_RECORDED
    fill = VenueFillEvent(
        "tr-1",
        "o-9",
        "bot-cid-1",
        "BTCUSDT",
        "BUY",
        Decimal("0.5"),
        Decimal("90000"),
        Decimal("1"),
        NOW - timedelta(minutes=5),
    )
    r_open = reconcile_venue_startup(
        _snap(recent_fills=(fill, fill)),
        expected_workspace_id=WS_ID,
        expected_account_id=ACC_ID,
        expected_environment="DEMO",
        known_state=_known(),
        protected_symbols=frozenset(),
        unknown_intents=[pending],
        as_of=NOW,
    )
    assert r_open.outcome is VenueReconcileOutcome.OPEN_EXPOSURE_RECORDED
    assert r_open.deduped_fill_count == 1
    assert r_open.can_transition_reservation is True

    # 3. Reconnect không khép được stream gap (history_coverage_from > last_stream_event_at)
    r_gap = reconcile_venue_reconnect(
        _snap(history_coverage_from=NOW - timedelta(minutes=5)),
        last_stream_event_at=NOW - timedelta(hours=1),
        expected_workspace_id=WS_ID,
        expected_account_id=ACC_ID,
        expected_environment="DEMO",
        known_state=_known(),
        protected_symbols=frozenset(),
        as_of=NOW,
    )
    assert r_gap.outcome is VenueReconcileOutcome.STALE_INCOMPLETE
    assert r_gap.reason_code == "STREAM_GAP_UNCLOSABLE"
    assert r_gap.entry_allowed is False
