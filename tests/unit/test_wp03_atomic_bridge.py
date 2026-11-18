"""Unit tests cho PostgresAtomicVenueReconciliationBridge (WP-03B.1 / CRY-290)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from trading_bot.adapters.database.venue_reconciliation_ledger import (
    PostgresAtomicVenueReconciliationBridge,
)
from trading_bot.domain.execution.venue_orders import VenueOrderState
from trading_bot.domain.risk.reservations import ReservationReleaseReason
from trading_bot.ports.venue_reconciliation import VenueOrderTransition


class _FakeSession:
    def __init__(self) -> None:
        self.info: dict[str, object] = {}
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, exc_type: object, _exc: object, _tb: object) -> None:
        if exc_type is not None:
            self.rolled_back = True
        else:
            self.committed = True

    def begin(self) -> _FakeSession:
        return self


@pytest.mark.asyncio
async def test_atomic_bridge_commits_transition_and_release_in_single_tx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = uuid4()
    bot_id = uuid4()
    acc_id = uuid4()
    intent_id = uuid4()
    res_id = uuid4()

    sessions_created: list[_FakeSession] = []

    class _FakeSessionFactory:
        def __call__(self) -> _FakeSession:
            s = _FakeSession()
            sessions_created.append(s)
            return s

    calls: list[tuple[str, object]] = []

    class _FakeLedgerRepo:
        def __init__(self, session: _FakeSession) -> None:
            assert session.info.get("bot_id") == bot_id

        async def transition(self, **kwargs: object) -> object:
            calls.append(("ledger.transition", kwargs["expected_sequence"]))
            return object()

    class _FakeRiskRepo:
        def __init__(self, session: _FakeSession) -> None:
            assert session.info.get("bot_id") == bot_id

        async def release_for_venue_reconciliation(self, **kwargs: object) -> object:
            calls.append(("risk.release", kwargs["reason"]))
            return object()

    monkeypatch.setattr(
        "trading_bot.adapters.database.venue_reconciliation_ledger.VenueOrderLedgerRepository",
        _FakeLedgerRepo,
    )
    monkeypatch.setattr(
        "trading_bot.adapters.database.venue_reconciliation_ledger.RiskReservationRepository",
        _FakeRiskRepo,
    )

    bridge = PostgresAtomicVenueReconciliationBridge(
        sessions=_FakeSessionFactory(),  # type: ignore[arg-type]
        workspace_id=ws_id,
        bot_id=bot_id,
        account_id=acc_id,
    )
    cmd = VenueOrderTransition(
        event_key="reconcile:1",
        actor_id="worker-1",
        source="RECONCILIATION",
        state=VenueOrderState.CANCELED,
        attempt_count=1,
        last_attempt_at=datetime.now(UTC),
        retry_not_before=None,
        venue_order_id=None,
        filled_quantity=Decimal("0"),
        last_event_at=datetime.now(UTC),
        evidence_hash="hash-1",
    )
    await bridge.reconcile_order_absent(
        workspace_id=ws_id,
        account_id=acc_id,
        intent_id=intent_id,
        reservation_id=res_id,
        expected_sequence=2,
        command=cmd,
    )
    assert len(sessions_created) == 1
    assert sessions_created[0].committed is True
    assert sessions_created[0].rolled_back is False
    assert calls == [
        ("ledger.transition", 2),
        ("risk.release", ReservationReleaseReason.ORDER_ABSENT),
    ]


@pytest.mark.asyncio
async def test_atomic_bridge_rolls_back_when_risk_release_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = uuid4()
    bot_id = uuid4()
    acc_id = uuid4()
    sessions_created: list[_FakeSession] = []

    class _FakeSessionFactory:
        def __call__(self) -> _FakeSession:
            s = _FakeSession()
            sessions_created.append(s)
            return s

    class _FakeLedgerRepo:
        def __init__(self, _session: _FakeSession) -> None:
            pass

        async def transition(self, **_kwargs: object) -> object:
            return object()

    class _FailingRiskRepo:
        def __init__(self, _session: _FakeSession) -> None:
            pass

        async def release_for_venue_reconciliation(self, **_kwargs: object) -> object:
            raise RuntimeError("DB lock conflict")

    monkeypatch.setattr(
        "trading_bot.adapters.database.venue_reconciliation_ledger.VenueOrderLedgerRepository",
        _FakeLedgerRepo,
    )
    monkeypatch.setattr(
        "trading_bot.adapters.database.venue_reconciliation_ledger.RiskReservationRepository",
        _FailingRiskRepo,
    )

    bridge = PostgresAtomicVenueReconciliationBridge(
        sessions=_FakeSessionFactory(),  # type: ignore[arg-type]
        workspace_id=ws_id,
        bot_id=bot_id,
        account_id=acc_id,
    )
    cmd = VenueOrderTransition(
        event_key="reconcile:2",
        actor_id="worker-1",
        source="RECONCILIATION",
        state=VenueOrderState.REJECTED,
        attempt_count=1,
        last_attempt_at=datetime.now(UTC),
        retry_not_before=None,
        venue_order_id=None,
        filled_quantity=Decimal("0"),
        last_event_at=datetime.now(UTC),
    )
    with pytest.raises(RuntimeError, match="DB lock conflict"):
        await bridge.reconcile_order_rejected(
            workspace_id=ws_id,
            account_id=acc_id,
            intent_id=uuid4(),
            reservation_id=uuid4(),
            expected_sequence=1,
            command=cmd,
        )
    assert sessions_created[0].committed is False
    assert sessions_created[0].rolled_back is True


@pytest.mark.asyncio
async def test_atomic_bridge_rejects_cross_account_scope() -> None:
    bridge = PostgresAtomicVenueReconciliationBridge(
        sessions=AsyncMock(),
        workspace_id=uuid4(),
        bot_id=uuid4(),
        account_id=uuid4(),
    )
    cmd = VenueOrderTransition(
        event_key="reconcile:3",
        actor_id="worker-1",
        source="RECONCILIATION",
        state=VenueOrderState.CANCELED,
        attempt_count=1,
        last_attempt_at=datetime.now(UTC),
        retry_not_before=None,
        venue_order_id=None,
        filled_quantity=Decimal("0"),
        last_event_at=datetime.now(UTC),
    )
    with pytest.raises(PermissionError):
        await bridge.reconcile_order_absent(
            workspace_id=uuid4(),
            account_id=uuid4(),
            intent_id=uuid4(),
            reservation_id=uuid4(),
            expected_sequence=1,
            command=cmd,
        )
