"""Cổng cho đối soát venue; adapter phải xác minh nguồn trước khi trả bằng chứng."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from trading_bot.domain.execution.venue_orders import VenueOrderState, VenueReconciliation


@dataclass(frozen=True, slots=True)
class VenueOrderTransition:
    """Command trung lập để append trạng thái đã được xác minh."""

    event_key: str
    actor_id: str
    source: str
    state: VenueOrderState
    attempt_count: int
    last_attempt_at: datetime | None
    retry_not_before: datetime | None
    venue_order_id: str | None
    filled_quantity: Decimal
    last_event_at: datetime | None
    evidence_hash: str | None = None


@dataclass(frozen=True, slots=True)
class VerifiedVenueReconciliation:
    """Envelope verifier ràng buộc evidence với đúng intent và risk reservation."""

    intent_id: UUID
    workspace_id: UUID
    bot_id: UUID
    account_id: UUID
    reservation_id: UUID
    evidence: VenueReconciliation


class VenueOrderSnapshot(Protocol):
    """Projection tối thiểu đọc từ ledger trong phạm vi account đã xác thực."""

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


class VenueOrderLedgerPort(Protocol):
    """Đọc và append transition trong transaction do adapter sở hữu."""

    async def get_intent(
        self, *, workspace_id: UUID, account_id: UUID, intent_id: UUID
    ) -> VenueOrderSnapshot | None: ...

    async def transition(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        intent_id: UUID,
        expected_sequence: int,
        command: VenueOrderTransition,
    ) -> object: ...


class VenueReconciliationVerifier(Protocol):
    """Chỉ trả evidence khi đã xác minh đủ account, môi trường và độ bao phủ."""

    async def verify(self, *, intent: VenueOrderSnapshot) -> VerifiedVenueReconciliation | None: ...
