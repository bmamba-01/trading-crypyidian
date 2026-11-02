"""Repository PostgreSQL cho venue order intent và transition audit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    BotInstance,
    RiskReservationRecord,
    VenueOrderIntentRecord,
    VenueOrderTransitionRecord,
)
from trading_bot.domain.execution.venue_orders import VenueOrderIntent, VenueOrderState

_SOURCES = frozenset({"APPLICATION", "SUBMISSION", "RECONCILIATION", "USER_STREAM", "OPERATOR"})


class VenueOrderLedgerConflict(RuntimeError):
    """Từ chối idempotency, sequence hoặc scope venue intent không khớp."""


@dataclass(frozen=True, slots=True)
class VenueOrderTransitionCommand:
    """Dữ liệu transition đã chuẩn hóa; evidence chỉ lưu hash, không lưu payload raw."""

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
class VenueOrderWriteResult:
    """Kết quả ghi intent/transition; không dựng object domain đã ký lại."""

    record: VenueOrderIntentRecord
    is_duplicate: bool


class VenueOrderLedgerRepository:
    """Lưu ledger trong transaction caller; không kết nối transport venue."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_intent(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        reservation_id: UUID,
        intent: VenueOrderIntent,
        actor_id: str,
    ) -> VenueOrderWriteResult:
        """Tạo intent CREATED trước external side effect; retry cùng payload trả cùng row."""
        if type(workspace_id) is not UUID or type(account_id) is not UUID:
            raise VenueOrderLedgerConflict("workspace_id và account_id phải là UUID.")
        if intent.state is not VenueOrderState.CREATED:
            raise VenueOrderLedgerConflict("Chỉ được persist intent CREATED lần đầu.")
        if str(workspace_id) != intent.workspace_id or str(account_id) != intent.account_id:
            raise VenueOrderLedgerConflict("Intent scope khác workspace/account của transaction.")
        if reservation_id != intent.reservation_id:
            raise VenueOrderLedgerConflict("Reservation ID không khớp intent đã ký.")
        try:
            settings_version_id = UUID(intent.settings_version)
        except ValueError, TypeError, AttributeError:
            raise VenueOrderLedgerConflict(
                "settings_version phải là UUID settings đã publish."
            ) from None
        _validate_text(actor_id, "actor_id", 128)
        bot_id = await self._bind_workspace(workspace_id, account_id)
        await self._lock_risk_scope(workspace_id, account_id)
        await self._lock_idempotency(workspace_id, account_id, intent.idempotency_key)
        reservation = await self._session.scalar(
            select(RiskReservationRecord)
            .where(
                RiskReservationRecord.workspace_id == workspace_id,
                RiskReservationRecord.account_id == account_id,
                RiskReservationRecord.id == reservation_id,
            )
            .with_for_update()
        )
        if reservation is None:
            raise VenueOrderLedgerConflict("Risk reservation không thuộc scope được phép.")
        existing = await self._session.scalar(
            select(VenueOrderIntentRecord).where(
                VenueOrderIntentRecord.workspace_id == workspace_id,
                VenueOrderIntentRecord.account_id == account_id,
                VenueOrderIntentRecord.idempotency_key == intent.idempotency_key,
            )
        )
        if existing is not None:
            if existing.request_fingerprint != intent.request_fingerprint:
                raise VenueOrderLedgerConflict("idempotency key đã dùng với payload khác.")
            if existing.client_order_id != intent.client_order_id:
                raise VenueOrderLedgerConflict("idempotency retry có client order ID khác.")
            if existing.reservation_id != reservation_id:
                raise VenueOrderLedgerConflict("idempotency retry có reservation khác.")
            return VenueOrderWriteResult(existing, True)

        record = VenueOrderIntentRecord(
            id=intent.intent_id,
            workspace_id=workspace_id,
            bot_id=bot_id,
            account_id=account_id,
            reservation_id=reservation_id,
            mode=intent.mode.value,
            idempotency_key=intent.idempotency_key,
            request_fingerprint=intent.request_fingerprint,
            plan_hash=intent.plan_hash,
            settings_version_id=settings_version_id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            quantity=intent.quantity,
            limit_price=intent.limit_price,
            client_order_id=intent.client_order_id,
            state=VenueOrderState.CREATED.value,
            attempt_count=0,
            filled_quantity=Decimal(0),
            transition_sequence=1,
            transition_key=f"created:{intent.intent_id}",
            transition_actor=actor_id,
            transition_source="APPLICATION",
        )
        self._session.add(record)
        await self._session.flush()
        return VenueOrderWriteResult(record, False)

    async def get_intent(
        self, *, workspace_id: UUID, account_id: UUID, intent_id: UUID
    ) -> VenueOrderIntentRecord | None:
        """Đọc record trong phạm vi workspace/account; không tái tạo proof domain."""
        await self._bind_workspace(workspace_id, account_id)
        return await self._session.scalar(
            select(VenueOrderIntentRecord).where(
                VenueOrderIntentRecord.workspace_id == workspace_id,
                VenueOrderIntentRecord.account_id == account_id,
                VenueOrderIntentRecord.id == intent_id,
            )
        )

    async def transition(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        intent_id: UUID,
        expected_sequence: int,
        command: VenueOrderTransitionCommand,
    ) -> VenueOrderWriteResult:
        """Khóa row, kiểm sequence rồi để DB trigger áp dụng graph và ghi audit."""
        _validate_text(command.event_key, "event_key", 240)
        _validate_text(command.actor_id, "actor_id", 128)
        if not isinstance(command.source, str) or command.source not in _SOURCES:
            raise VenueOrderLedgerConflict("source transition không được hỗ trợ.")
        if type(command.state) is not VenueOrderState:
            raise VenueOrderLedgerConflict("state phải là VenueOrderState tường minh.")
        if type(expected_sequence) is not int or expected_sequence < 1:
            raise VenueOrderLedgerConflict("expected_sequence phải là số dương.")
        if type(command.attempt_count) is not int or not 0 <= command.attempt_count <= 255:
            raise VenueOrderLedgerConflict("attempt_count ngoài giới hạn.")
        if (
            not isinstance(command.filled_quantity, Decimal)
            or not command.filled_quantity.is_finite()
            or command.filled_quantity < 0
        ):
            raise VenueOrderLedgerConflict("filled_quantity không được âm.")
        for name in ("last_attempt_at", "retry_not_before", "last_event_at"):
            value = getattr(command, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise VenueOrderLedgerConflict(f"{name} phải có múi giờ.")
        if command.evidence_hash is not None and not _is_hash(command.evidence_hash):
            raise VenueOrderLedgerConflict("evidence_hash phải là SHA-256 hex.")
        await self._bind_workspace(workspace_id, account_id)
        prior_event = await self._session.scalar(
            select(VenueOrderTransitionRecord).where(
                VenueOrderTransitionRecord.workspace_id == workspace_id,
                VenueOrderTransitionRecord.event_key == command.event_key,
            )
        )
        if prior_event is not None:
            if (
                prior_event.intent_id != intent_id
                or prior_event.to_state != command.state.value
                or prior_event.actor_id != command.actor_id
                or prior_event.source != command.source
                or prior_event.evidence_hash != command.evidence_hash
                or prior_event.attempt_count != command.attempt_count
                or prior_event.filled_quantity != command.filled_quantity
                or prior_event.retry_not_before != command.retry_not_before
                or prior_event.venue_order_id != command.venue_order_id
                or prior_event.venue_event_at != command.last_event_at
            ):
                raise VenueOrderLedgerConflict("event_key đã có transition payload khác.")
            record = await self.get_intent(
                workspace_id=workspace_id, account_id=account_id, intent_id=intent_id
            )
            if record is None:
                raise VenueOrderLedgerConflict("Intent không thuộc scope được phép.")
            return VenueOrderWriteResult(record, True)

        reservation_id_result = await self._session.scalar(
            select(VenueOrderIntentRecord.reservation_id).where(
                VenueOrderIntentRecord.workspace_id == workspace_id,
                VenueOrderIntentRecord.account_id == account_id,
                VenueOrderIntentRecord.id == intent_id,
            )
        )
        if reservation_id_result is None:
            raise VenueOrderLedgerConflict("Intent không thuộc scope được phép.")
        if command.state is VenueOrderState.SUBMITTING:
            await self._lock_risk_scope(workspace_id, account_id)
            reservation = await self._session.scalar(
                select(RiskReservationRecord)
                .where(
                    RiskReservationRecord.workspace_id == workspace_id,
                    RiskReservationRecord.account_id == account_id,
                    RiskReservationRecord.id == reservation_id_result,
                )
                .with_for_update()
            )
            if reservation is None:
                raise VenueOrderLedgerConflict("Risk reservation không thuộc scope được phép.")

        record = await self._session.scalar(
            select(VenueOrderIntentRecord)
            .where(
                VenueOrderIntentRecord.workspace_id == workspace_id,
                VenueOrderIntentRecord.account_id == account_id,
                VenueOrderIntentRecord.id == intent_id,
            )
            .with_for_update()
        )
        if record is None:
            raise VenueOrderLedgerConflict("Intent không thuộc scope được phép.")
        if record.transition_sequence != expected_sequence:
            raise VenueOrderLedgerConflict(
                "transition_sequence stale; nạp lại intent trước khi ghi."
            )
        record.state = command.state.value
        record.attempt_count = command.attempt_count
        record.last_attempt_at = command.last_attempt_at
        record.retry_not_before = command.retry_not_before
        record.venue_order_id = command.venue_order_id
        record.filled_quantity = command.filled_quantity
        record.last_event_at = command.last_event_at
        record.transition_sequence = expected_sequence + 1
        record.transition_key = command.event_key
        record.transition_actor = command.actor_id
        record.transition_source = command.source
        record.transition_evidence_hash = command.evidence_hash
        await self._session.flush()
        return VenueOrderWriteResult(record, False)

    async def list_transitions(
        self, *, workspace_id: UUID, account_id: UUID, intent_id: UUID
    ) -> list[VenueOrderTransitionRecord]:
        """Đọc audit theo thứ tự DB sequence, luôn giữ scope rõ ràng."""
        await self._bind_workspace(workspace_id, account_id)
        rows = await self._session.scalars(
            select(VenueOrderTransitionRecord)
            .where(
                VenueOrderTransitionRecord.workspace_id == workspace_id,
                VenueOrderTransitionRecord.account_id == account_id,
                VenueOrderTransitionRecord.intent_id == intent_id,
            )
            .order_by(VenueOrderTransitionRecord.sequence)
        )
        return list(rows.all())

    async def _bind_workspace(self, workspace_id: UUID, account_id: UUID) -> UUID:
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        supplied_bot_id = self._session.info.get("bot_id")
        if supplied_bot_id is None:
            raise VenueOrderLedgerConflict("Runtime cần bot_id tin cậy trong session.info.")
        try:
            bot_id = UUID(str(supplied_bot_id))
        except ValueError, TypeError, AttributeError:
            raise VenueOrderLedgerConflict("bot_id runtime phải là UUID.") from None
        matching_bot = await self._session.scalar(
            select(BotInstance.id).where(
                BotInstance.workspace_id == workspace_id,
                BotInstance.trading_account_id == account_id,
                BotInstance.id == bot_id,
            )
        )
        if matching_bot is None:
            raise VenueOrderLedgerConflict("Bot runtime không thuộc scope workspace/account.")
        await self._session.execute(
            text("SELECT set_config('app.bot_id', :bot_id, true)"),
            {"bot_id": str(bot_id)},
        )
        return bot_id

    async def _lock_idempotency(
        self, workspace_id: UUID, account_id: UUID, idempotency_key: str
    ) -> None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": (f"venue-order:{workspace_id}:{account_id}:{idempotency_key}")},
        )

    async def _lock_risk_scope(self, workspace_id: UUID, account_id: UUID) -> None:
        """Dùng cùng thứ tự advisory lock với RiskReservationRepository."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:risk_lock, 0))"),
            {"risk_lock": f"workspace-risk:{workspace_id}"},
        )
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:risk_lock, 0))"),
            {"risk_lock": f"account-risk:{workspace_id}:{account_id}"},
        )


def _validate_text(value: str, name: str, max_length: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise VenueOrderLedgerConflict(f"{name} phải có từ 1 đến {max_length} ký tự.")


def _is_hash(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
