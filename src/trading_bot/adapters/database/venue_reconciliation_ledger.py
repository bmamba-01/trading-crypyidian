"""Adapter DB thực thi port ledger đối soát trong scope một bot/account."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import VenueOrderIntentRecord
from trading_bot.adapters.database.repositories.venue_order_ledger import (
    VenueOrderLedgerRepository,
    VenueOrderTransitionCommand,
)
from trading_bot.domain.execution.venue_orders import VenueOrderState
from trading_bot.ports.venue_reconciliation import (
    VenueOrderLedgerPort,
    VenueOrderSnapshot,
    VenueOrderTransition,
)


@dataclass(slots=True)
class _VenueOrderSnapshot:
    """Projection tối thiểu để verifier không cần nhận ORM record."""

    id: UUID
    workspace_id: UUID
    bot_id: UUID
    account_id: UUID
    reservation_id: UUID
    client_order_id: str
    state: str
    quantity: Decimal
    filled_quantity: Decimal
    venue_order_id: str | None
    attempt_count: int
    last_attempt_at: datetime | None
    last_event_at: datetime | None
    created_at: datetime
    transition_sequence: int


class DatabaseVenueOrderLedgerPort(VenueOrderLedgerPort):
    """Mở transaction riêng cho mỗi thao tác và buộc scope runtime đã tin cậy.

    Bot ID được bind lúc dựng adapter từ composition runtime; caller không thể đổi
    sang workspace/account khác để vượt Row-Level Security hoặc khóa bot.
    """

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        workspace_id: UUID,
        bot_id: UUID,
        account_id: UUID,
    ) -> None:
        if any(type(value) is not UUID for value in (workspace_id, bot_id, account_id)):
            raise ValueError("workspace_id, bot_id và account_id phải là UUID.")
        self._sessions = sessions
        self._workspace_id = workspace_id
        self._bot_id = bot_id
        self._account_id = account_id

    async def get_intent(
        self, *, workspace_id: UUID, account_id: UUID, intent_id: UUID
    ) -> VenueOrderSnapshot | None:
        if (workspace_id, account_id) != (self._workspace_id, self._account_id):
            return None
        async with self._sessions() as session, session.begin():
            session.info["bot_id"] = self._bot_id
            repository = VenueOrderLedgerRepository(session)
            record = await repository.get_intent(
                workspace_id=workspace_id,
                account_id=account_id,
                intent_id=intent_id,
            )
            if record is None or record.bot_id != self._bot_id:
                return None
            return _snapshot(record)

    async def transition(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        intent_id: UUID,
        expected_sequence: int,
        command: VenueOrderTransition,
    ) -> object:
        if (workspace_id, account_id) != (self._workspace_id, self._account_id):
            raise PermissionError("Đối soát không khớp bot/account đã bind.")
        if type(command.state) is not VenueOrderState:
            raise ValueError("state phải là VenueOrderState.")
        repository_command = VenueOrderTransitionCommand(
            event_key=command.event_key,
            actor_id=command.actor_id,
            source=command.source,
            state=command.state,
            attempt_count=command.attempt_count,
            last_attempt_at=command.last_attempt_at,
            retry_not_before=command.retry_not_before,
            venue_order_id=command.venue_order_id,
            filled_quantity=command.filled_quantity,
            last_event_at=command.last_event_at,
            evidence_hash=command.evidence_hash,
        )
        async with self._sessions() as session, session.begin():
            session.info["bot_id"] = self._bot_id
            repository = VenueOrderLedgerRepository(session)
            return await repository.transition(
                workspace_id=workspace_id,
                account_id=account_id,
                intent_id=intent_id,
                expected_sequence=expected_sequence,
                command=repository_command,
            )


def _snapshot(record: VenueOrderIntentRecord) -> VenueOrderSnapshot:
    return _VenueOrderSnapshot(
        id=record.id,
        workspace_id=record.workspace_id,
        bot_id=record.bot_id,
        account_id=record.account_id,
        reservation_id=record.reservation_id,
        client_order_id=record.client_order_id,
        state=record.state,
        quantity=record.quantity,
        filled_quantity=record.filled_quantity,
        venue_order_id=record.venue_order_id,
        attempt_count=record.attempt_count,
        last_attempt_at=record.last_attempt_at,
        last_event_at=record.last_event_at,
        created_at=record.created_at,
        transition_sequence=record.transition_sequence,
    )
