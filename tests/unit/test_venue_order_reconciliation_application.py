from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from trading_bot.application.venue_order_reconciliation import (
    VenueOrderReconciliationService,
    VenueReconciliationStatus,
)
from trading_bot.domain.execution.venue_orders import (
    ReconciliationOutcome,
    ReconciliationSource,
    VenueOrderState,
    VenueReconciliation,
)
from trading_bot.ports.venue_reconciliation import VerifiedVenueReconciliation

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
WORKSPACE_ID = uuid4()
ACCOUNT_ID = uuid4()
INTENT_ID = uuid4()
BOT_ID = uuid4()
RESERVATION_ID = uuid4()


class FakeLedger:
    def __init__(self) -> None:
        self.intent = SimpleNamespace(
            id=INTENT_ID,
            workspace_id=WORKSPACE_ID,
            bot_id=BOT_ID,
            account_id=ACCOUNT_ID,
            reservation_id=RESERVATION_ID,
            client_order_id="client-123",
            quantity=Decimal("2"),
            filled_quantity=Decimal("0"),
            venue_order_id=None,
            state=VenueOrderState.UNKNOWN.value,
            attempt_count=1,
            created_at=NOW - timedelta(seconds=20),
            last_attempt_at=NOW - timedelta(seconds=10),
            last_event_at=None,
            transition_sequence=4,
        )
        self.commands = []

    async def get_intent(self, *, workspace_id, account_id, intent_id):
        if (workspace_id, account_id, intent_id) != (WORKSPACE_ID, ACCOUNT_ID, INTENT_ID):
            return None
        return self.intent

    async def transition(self, *, workspace_id, account_id, intent_id, expected_sequence, command):
        assert expected_sequence == self.intent.transition_sequence
        self.commands.append(command)
        return SimpleNamespace(record=self.intent, is_duplicate=False)


class FakeVerifier:
    def __init__(self, evidence):
        self.evidence = evidence

    async def verify(self, *, intent):
        return VerifiedVenueReconciliation(
            intent_id=intent.id,
            workspace_id=intent.workspace_id,
            bot_id=intent.bot_id,
            account_id=intent.account_id,
            reservation_id=intent.reservation_id,
            evidence=self.evidence,
        )


def evidence(
    *,
    outcome=ReconciliationOutcome.ORDER_OPEN,
    checked_at=NOW,
    source=ReconciliationSource.REST_ORDER_QUERY,
    venue_order_id="venue-456",
    cumulative_filled=Decimal("0"),
):
    if outcome is ReconciliationOutcome.ORDER_ABSENT:
        source = ReconciliationSource.ORDER_QUERY_AND_TRADE_HISTORY
        venue_order_id = None
    return VenueReconciliation(
        outcome=outcome,
        source=source,
        source_ref="snapshot:123",
        workspace_id=str(WORKSPACE_ID),
        account_id=str(ACCOUNT_ID),
        client_order_id="client-123",
        checked_at=checked_at,
        cumulative_filled=cumulative_filled,
        venue_order_id=venue_order_id,
    )


@pytest.mark.asyncio
async def test_verified_open_order_advances_ledger_but_keeps_risk_held() -> None:
    ledger = FakeLedger()
    service = VenueOrderReconciliationService(
        ledger=ledger,
        verifier=FakeVerifier(evidence()),
        clock=lambda: NOW,
    )

    result = await service.reconcile_unknown(
        workspace_id=WORKSPACE_ID, account_id=ACCOUNT_ID, intent_id=INTENT_ID
    )

    assert result.status is VenueReconciliationStatus.APPLIED
    assert len(ledger.commands) == 1
    command = ledger.commands[0]
    assert command.state is VenueOrderState.ACKNOWLEDGED
    assert command.venue_order_id == "venue-456"
    assert command.source == "RECONCILIATION"
    assert command.evidence_hash is not None
    assert command.last_event_at == NOW


@pytest.mark.asyncio
async def test_verified_partial_fill_is_monotonic_and_keeps_risk_held() -> None:
    ledger = FakeLedger()
    service = VenueOrderReconciliationService(
        ledger=ledger,
        verifier=FakeVerifier(
            evidence(
                outcome=ReconciliationOutcome.ORDER_OPEN,
                cumulative_filled=Decimal("0.5"),
            )
        ),
        clock=lambda: NOW,
    )

    result = await service.reconcile_unknown(
        workspace_id=WORKSPACE_ID, account_id=ACCOUNT_ID, intent_id=INTENT_ID
    )

    assert result.status is VenueReconciliationStatus.APPLIED
    assert result.resulting_state is VenueOrderState.PARTIALLY_FILLED
    assert result.risk_reservation_held
    assert ledger.commands[0].filled_quantity == Decimal("0.5")


@pytest.mark.asyncio
async def test_stale_evidence_keeps_unknown_and_does_not_write() -> None:
    ledger = FakeLedger()
    service = VenueOrderReconciliationService(
        ledger=ledger,
        verifier=FakeVerifier(evidence(checked_at=NOW - timedelta(seconds=11))),
        clock=lambda: NOW,
    )

    result = await service.reconcile_unknown(
        workspace_id=WORKSPACE_ID, account_id=ACCOUNT_ID, intent_id=INTENT_ID
    )

    assert result.status is VenueReconciliationStatus.HELD
    assert not ledger.commands


@pytest.mark.asyncio
async def test_order_absent_is_held_until_reservation_bridge_exists() -> None:
    ledger = FakeLedger()
    service = VenueOrderReconciliationService(
        ledger=ledger,
        verifier=FakeVerifier(evidence(outcome=ReconciliationOutcome.ORDER_ABSENT)),
        clock=lambda: NOW,
    )

    result = await service.reconcile_unknown(
        workspace_id=WORKSPACE_ID, account_id=ACCOUNT_ID, intent_id=INTENT_ID
    )

    assert result.status is VenueReconciliationStatus.HELD
    assert result.reason == "RESERVATION_BRIDGE_REQUIRED"
    assert not ledger.commands


@pytest.mark.asyncio
async def test_verifier_failure_keeps_unknown_and_does_not_write() -> None:
    ledger = FakeLedger()

    class FailingVerifier:
        async def verify(self, *, intent):
            raise TimeoutError("private payload must not escape")

    service = VenueOrderReconciliationService(
        ledger=ledger, verifier=FailingVerifier(), clock=lambda: NOW
    )

    result = await service.reconcile_unknown(
        workspace_id=WORKSPACE_ID, account_id=ACCOUNT_ID, intent_id=INTENT_ID
    )

    assert result.status is VenueReconciliationStatus.HELD
    assert result.reason == "VERIFICATION_UNAVAILABLE"
    assert not ledger.commands


@pytest.mark.asyncio
async def test_wrong_scope_or_impossible_fill_is_held() -> None:
    ledger = FakeLedger()
    wrong_scope = evidence()
    object.__setattr__(wrong_scope, "account_id", str(uuid4()))
    service = VenueOrderReconciliationService(
        ledger=ledger, verifier=FakeVerifier(wrong_scope), clock=lambda: NOW
    )

    result = await service.reconcile_unknown(
        workspace_id=WORKSPACE_ID, account_id=ACCOUNT_ID, intent_id=INTENT_ID
    )

    assert result.status is VenueReconciliationStatus.HELD
    assert not ledger.commands


@pytest.mark.asyncio
async def test_verification_bound_to_another_reservation_is_held() -> None:
    ledger = FakeLedger()

    class WrongReservationVerifier:
        async def verify(self, *, intent):
            return VerifiedVenueReconciliation(
                intent_id=intent.id,
                workspace_id=intent.workspace_id,
                bot_id=intent.bot_id,
                account_id=intent.account_id,
                reservation_id=uuid4(),
                evidence=evidence(),
            )

    service = VenueOrderReconciliationService(
        ledger=ledger, verifier=WrongReservationVerifier(), clock=lambda: NOW
    )

    result = await service.reconcile_unknown(
        workspace_id=WORKSPACE_ID, account_id=ACCOUNT_ID, intent_id=INTENT_ID
    )

    assert result.status is VenueReconciliationStatus.HELD
    assert result.reason == "VERIFICATION_SCOPE_MISMATCH"
    assert not ledger.commands
