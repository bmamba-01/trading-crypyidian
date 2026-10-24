from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from tests.unit.bot_scope_fakes import BotScopeSession

from trading_bot.adapters.database.repositories.paper_ledger import (
    PaperLedgerConflict,
    PaperLedgerRepository,
)
from trading_bot.adapters.database.repositories.risk_reservations import (
    RiskReservationRepository,
)
from trading_bot.domain.risk.reservations import (
    ReservationConflict,
    ReservationReleaseReason,
    ReservationStatus,
)

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


def _reservation(workspace_id, account_id, *, status="RESERVED", expires_at=None):
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        account_id=account_id,
        idempotency_key="paper-intent",
        plan_hash="a" * 64,
        reserved_risk=Decimal("10"),
        reserved_notional=Decimal("100"),
        reserved_initial_margin=Decimal("25"),
        status=status,
        expires_at=expires_at or NOW + timedelta(minutes=5),
        symbol="BTCUSDT",
        resolution_reason=None,
    )


class TransitionSession(BotScopeSession):
    def __init__(self, *, reservation, intent, trade=None, fill=None) -> None:
        self.rows = {
            "trading_account": SimpleNamespace(id=reservation.account_id),
            "risk_reservation": reservation,
            "order_intent": intent,
            "trade": trade,
            "fill": fill,
        }
        self.statements: list[str] = []

    async def scalar(self, statement):
        if self.is_bot_query(statement):
            return self.bot_identity(statement)
        sql = str(statement.compile())
        self.statements.append(sql)
        match = re.search(r"\bFROM\s+([a-z_]+)", sql, re.IGNORECASE)
        from_table = match.group(1).lower() if match is not None else ""
        for table in self.rows:
            if from_table == table:
                return self.rows[table]
        raise AssertionError(f"Truy vấn không nhận diện được: {sql}")

    async def flush(self):
        return None


class OrderTransitionSession(BotScopeSession):
    def __init__(self, intent, order) -> None:
        self.rows = {"order_intent": intent, "exchange_order": order}
        self.flushes = 0

    async def scalar(self, statement):
        if self.is_bot_query(statement):
            return self.bot_identity(statement)
        sql = str(statement.compile()).lower()
        from_table = re.search(r"\bfrom\s+([a-z_]+)", sql)
        assert from_table is not None
        return self.rows[from_table.group(1)]

    async def flush(self):
        self.flushes += 1


class LedgerUow:
    def __init__(self, workspace_id, session) -> None:
        self.workspace_id = workspace_id
        self.request_id = uuid4()
        self.session = session
        self.events = []

    def enqueue(self, event) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_first_open_entry_transfers_reservation_out_of_pending_budget() -> None:
    workspace_id, account_id, intent_id, trade_id = (uuid4() for _ in range(4))
    reservation = _reservation(workspace_id, account_id)
    intent = SimpleNamespace(
        id=intent_id,
        workspace_id=workspace_id,
        account_id=account_id,
        risk_reservation_id=reservation.id,
        filled_quantity=Decimal("0.5"),
    )
    trade = SimpleNamespace(
        id=trade_id,
        workspace_id=workspace_id,
        account_id=account_id,
        intent_id=intent_id,
        risk_reservation_id=reservation.id,
        status="OPEN",
    )
    session = TransitionSession(
        reservation=reservation, intent=intent, trade=trade, fill=SimpleNamespace(id=uuid4())
    )
    repository = RiskReservationRepository(session)  # type: ignore[arg-type]

    result = await repository.transfer_to_open_trade(
        workspace_id=workspace_id,
        account_id=account_id,
        reservation_id=reservation.id,
        intent_id=intent_id,
        trade_id=trade_id,
    )

    assert result.status is ReservationStatus.TRANSFERRED
    assert result.resolution_reason is ReservationReleaseReason.OPEN_RISK_RECORDED
    assert reservation.status == "TRANSFERRED"
    assert "risk_reservation" in " ".join(session.statements)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent_status", "reason"),
    [
        ("CANCELLED", ReservationReleaseReason.CANCELLED_NO_FILL),
        ("EXPIRED", ReservationReleaseReason.EXPIRED_NO_FILL),
    ],
)
async def test_terminal_no_fill_releases_only_unfilled_reservation(intent_status, reason) -> None:
    workspace_id, account_id, intent_id = uuid4(), uuid4(), uuid4()
    reservation = _reservation(workspace_id, account_id)
    intent = SimpleNamespace(
        id=intent_id,
        workspace_id=workspace_id,
        account_id=account_id,
        risk_reservation_id=reservation.id,
        status=intent_status,
        filled_quantity=Decimal("0"),
    )
    session = TransitionSession(reservation=reservation, intent=intent)

    result = await RiskReservationRepository(session).release_terminal_no_fill(
        workspace_id=workspace_id,
        account_id=account_id,
        reservation_id=reservation.id,
        intent_id=intent_id,
    )

    assert result.status is ReservationStatus.RELEASED
    assert result.resolution_reason is reason
    assert reservation.status == "RELEASED"


@pytest.mark.asyncio
async def test_unknown_reservation_cannot_release_as_terminal_no_fill() -> None:
    workspace_id, account_id, intent_id = uuid4(), uuid4(), uuid4()
    reservation = _reservation(workspace_id, account_id, status="UNKNOWN")
    intent = SimpleNamespace(
        id=intent_id,
        workspace_id=workspace_id,
        account_id=account_id,
        risk_reservation_id=reservation.id,
        status="CANCELLED",
        filled_quantity=Decimal("0"),
    )
    session = TransitionSession(reservation=reservation, intent=intent)

    with pytest.raises(ReservationConflict):
        await RiskReservationRepository(session).release_terminal_no_fill(
            workspace_id=workspace_id,
            account_id=account_id,
            reservation_id=reservation.id,
            intent_id=intent_id,
        )


@pytest.mark.asyncio
async def test_transfer_fails_when_persisted_trade_is_not_open() -> None:
    workspace_id, account_id, intent_id, trade_id = (uuid4() for _ in range(4))
    reservation = _reservation(workspace_id, account_id)
    intent = SimpleNamespace(
        id=intent_id,
        workspace_id=workspace_id,
        account_id=account_id,
        risk_reservation_id=reservation.id,
        filled_quantity=Decimal("0.5"),
    )
    trade = SimpleNamespace(
        id=trade_id,
        workspace_id=workspace_id,
        account_id=account_id,
        intent_id=intent_id,
        risk_reservation_id=reservation.id,
        status="CLOSED",
    )
    session = TransitionSession(
        reservation=reservation, intent=intent, trade=trade, fill=SimpleNamespace(id=uuid4())
    )

    with pytest.raises(ReservationConflict):
        await RiskReservationRepository(session).transfer_to_open_trade(
            workspace_id=workspace_id,
            account_id=account_id,
            reservation_id=reservation.id,
            intent_id=intent_id,
            trade_id=trade_id,
        )
    assert reservation.status == "RESERVED"


@pytest.mark.asyncio
async def test_partial_fill_cannot_be_released_as_terminal_no_fill() -> None:
    workspace_id, account_id, intent_id = uuid4(), uuid4(), uuid4()
    reservation = _reservation(workspace_id, account_id)
    intent = SimpleNamespace(
        id=intent_id,
        workspace_id=workspace_id,
        account_id=account_id,
        risk_reservation_id=reservation.id,
        status="CANCELLED",
        filled_quantity=Decimal("0.01"),
    )
    session = TransitionSession(reservation=reservation, intent=intent)

    with pytest.raises(ReservationConflict):
        await RiskReservationRepository(session).release_terminal_no_fill(
            workspace_id=workspace_id,
            account_id=account_id,
            reservation_id=reservation.id,
            intent_id=intent_id,
        )
    assert reservation.status == "RESERVED"


@pytest.mark.asyncio
async def test_unknown_persisted_intent_keeps_reservation_after_candidate_expiry() -> None:
    workspace_id, account_id, intent_id = uuid4(), uuid4(), uuid4()
    reservation = _reservation(
        workspace_id,
        account_id,
        expires_at=NOW - timedelta(seconds=1),
    )
    intent = SimpleNamespace(
        id=intent_id,
        workspace_id=workspace_id,
        account_id=account_id,
        risk_reservation_id=reservation.id,
        status="UNKNOWN",
        filled_quantity=Decimal("0"),
    )
    session = TransitionSession(reservation=reservation, intent=intent)

    result = await RiskReservationRepository(session, clock=lambda: NOW).mark_unknown(
        workspace_id=workspace_id,
        account_id=account_id,
        reservation_id=reservation.id,
    )

    assert result.status is ReservationStatus.UNKNOWN
    assert reservation.status == "UNKNOWN"


@pytest.mark.asyncio
async def test_terminal_order_status_updates_intent_and_exchange_order_once() -> None:
    workspace_id, intent_id, account_id = uuid4(), uuid4(), uuid4()
    intent = SimpleNamespace(
        id=intent_id,
        workspace_id=workspace_id,
        account_id=account_id,
        status="ACCEPTED",
        filled_quantity=Decimal("0"),
    )
    order = SimpleNamespace(status="NEW")
    session = OrderTransitionSession(intent, order)
    uow = LedgerUow(workspace_id, session)
    repository = PaperLedgerRepository()

    first = await repository.record_order_transition(
        uow, intent_id=intent_id, status="EXPIRED", occurred_at=NOW
    )
    retry = await repository.record_order_transition(
        uow, intent_id=intent_id, status="EXPIRED", occurred_at=NOW
    )

    assert first is True
    assert retry is False
    assert intent.status == order.status == "EXPIRED"
    assert session.flushes == 1
    assert len(uow.events) == 1


@pytest.mark.asyncio
async def test_unknown_order_status_cannot_be_overwritten_by_cancel_transition() -> None:
    workspace_id, intent_id, account_id = uuid4(), uuid4(), uuid4()
    intent = SimpleNamespace(
        id=intent_id,
        workspace_id=workspace_id,
        account_id=account_id,
        status="UNKNOWN",
        filled_quantity=Decimal("0"),
    )
    order = SimpleNamespace(status="UNKNOWN")
    uow = LedgerUow(workspace_id, OrderTransitionSession(intent, order))

    with pytest.raises(PaperLedgerConflict):
        await PaperLedgerRepository().record_order_transition(
            uow, intent_id=intent_id, status="CANCELLED", occurred_at=NOW
        )

    assert intent.status == order.status == "UNKNOWN"
