"""Điều phối đối soát venue nhưng không tự tin cậy payload bên ngoài."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from trading_bot.domain.execution.venue_orders import (
    ReconciliationOutcome,
    VenueOrderState,
    VenueReconciliation,
)
from trading_bot.ports.venue_reconciliation import (
    VenueOrderLedgerPort,
    VenueOrderSnapshot,
    VenueOrderTransition,
    VenueReconciliationVerifier,
    VerifiedVenueReconciliation,
)

DEFAULT_EVIDENCE_MAX_AGE = timedelta(seconds=30)
_OUTCOME_STATES = {
    ReconciliationOutcome.ORDER_OPEN: VenueOrderState.ACKNOWLEDGED,
    ReconciliationOutcome.FILLED: VenueOrderState.FILLED,
    ReconciliationOutcome.CANCELED: VenueOrderState.CANCELED,
    ReconciliationOutcome.EXPIRED: VenueOrderState.EXPIRED,
}


class VenueReconciliationStatus(StrEnum):
    APPLIED = "APPLIED"
    HELD = "HELD"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class VenueReconciliationResult:
    status: VenueReconciliationStatus
    reason: str
    intent_id: UUID
    resulting_state: VenueOrderState
    risk_reservation_held: bool


class AtomicVenueReconciliationBridge(Protocol):
    """Cổng liên kết nguyên tử giữa venue ledger và risk reservation khi lệnh vắng mặt/từ chối."""

    async def reconcile_order_absent(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        intent_id: UUID,
        reservation_id: UUID,
        expected_sequence: int,
        command: VenueOrderTransition,
    ) -> None: ...

    async def reconcile_order_rejected(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        intent_id: UUID,
        reservation_id: UUID,
        expected_sequence: int,
        command: VenueOrderTransition,
    ) -> None: ...


class VenueOrderReconciliationService:
    """Áp dụng evidence đã xác minh; giải phóng reservation khi có bridge nguyên tử.

    Verifier là trust boundary: adapter phải xác thực account/mode, tính đầy đủ
    query + trade history và chuẩn hóa response trước khi tạo `VenueReconciliation`.
    Khi có `atomic_bridge`, các kết quả `ORDER_ABSENT` và `REJECTED` được xử lý nguyên tử
    với việc giải phóng rủi ro (risk reservation). Nếu không có bridge, service giữ trạng thái
    fail-closed (HELD) an toàn.
    """

    def __init__(
        self,
        *,
        ledger: VenueOrderLedgerPort,
        verifier: VenueReconciliationVerifier,
        atomic_bridge: AtomicVenueReconciliationBridge | None = None,
        clock: Callable[[], datetime] | None = None,
        max_evidence_age: timedelta = DEFAULT_EVIDENCE_MAX_AGE,
        actor_id: str = "venue-reconciliation-worker",
    ) -> None:
        if max_evidence_age <= timedelta(0):
            raise ValueError("max_evidence_age phải dương.")
        if not actor_id.strip() or len(actor_id) > 128:
            raise ValueError("actor_id phải có từ 1 đến 128 ký tự.")
        self._ledger = ledger
        self._verifier = verifier
        self._atomic_bridge = atomic_bridge
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_evidence_age = max_evidence_age
        self._actor_id = actor_id

    async def reconcile_unknown(
        self, *, workspace_id: UUID, account_id: UUID, intent_id: UUID
    ) -> VenueReconciliationResult:
        """Đọc intent UNKNOWN, xác minh độc lập rồi append đúng một transition."""
        if any(type(value) is not UUID for value in (workspace_id, account_id, intent_id)):
            raise ValueError("workspace_id, account_id và intent_id phải là UUID.")
        intent = await self._ledger.get_intent(
            workspace_id=workspace_id, account_id=account_id, intent_id=intent_id
        )
        if intent is None:
            return VenueReconciliationResult(
                VenueReconciliationStatus.NOT_APPLICABLE,
                "INTENT_NOT_FOUND",
                intent_id,
                VenueOrderState.UNKNOWN,
                True,
            )
        if (
            intent.workspace_id != workspace_id
            or intent.account_id != account_id
            or intent.id != intent_id
        ):
            return self._held(intent_id, "INTENT_SCOPE_MISMATCH", intent)
        if intent.state != VenueOrderState.UNKNOWN.value:
            try:
                state = VenueOrderState(intent.state)
            except ValueError:
                state = VenueOrderState.UNKNOWN
            return VenueReconciliationResult(
                VenueReconciliationStatus.NOT_APPLICABLE,
                "INTENT_NOT_UNKNOWN",
                intent_id,
                state,
                True,
            )
        try:
            verification = await self._verifier.verify(intent=intent)
        except Exception:
            return self._held(intent_id, "VERIFICATION_UNAVAILABLE", intent)
        if verification is None:
            return self._held(intent_id, "EVIDENCE_INCOMPLETE", intent)
        if type(verification) is not VerifiedVenueReconciliation:
            return self._held(intent_id, "VERIFICATION_MALFORMED", intent)
        if (
            verification.intent_id != intent.id
            or verification.workspace_id != intent.workspace_id
            or verification.bot_id != intent.bot_id
            or verification.account_id != intent.account_id
            or verification.reservation_id != intent.reservation_id
        ):
            return self._held(intent_id, "VERIFICATION_SCOPE_MISMATCH", intent)
        evidence = verification.evidence
        reason = self._validate_evidence(intent, evidence, self._clock())
        if reason is not None:
            return self._held(intent_id, reason, intent)
        if evidence.outcome is ReconciliationOutcome.ORDER_ABSENT:
            if self._atomic_bridge is not None:
                command = VenueOrderTransition(
                    event_key=f"reconcile:{intent.id}:{_evidence_hash(evidence, verification)}",
                    actor_id=self._actor_id,
                    source="RECONCILIATION",
                    state=VenueOrderState.CANCELED,
                    attempt_count=intent.attempt_count,
                    last_attempt_at=intent.last_attempt_at,
                    retry_not_before=None,
                    venue_order_id=None,
                    filled_quantity=evidence.cumulative_filled,
                    last_event_at=evidence.checked_at,
                    evidence_hash=_evidence_hash(evidence, verification),
                )
                try:
                    await self._atomic_bridge.reconcile_order_absent(
                        workspace_id=workspace_id,
                        account_id=account_id,
                        intent_id=intent_id,
                        reservation_id=intent.reservation_id,
                        expected_sequence=intent.transition_sequence,
                        command=command,
                    )
                    return VenueReconciliationResult(
                        VenueReconciliationStatus.APPLIED,
                        "ORDER_ABSENT_RESERVATION_RELEASED",
                        intent_id,
                        VenueOrderState.CANCELED,
                        False,
                    )
                except Exception:
                    return self._held(intent_id, "PERSISTENCE_CONFLICT", intent)
            return self._held(intent_id, "RESERVATION_BRIDGE_REQUIRED", intent)
        if evidence.outcome is ReconciliationOutcome.REJECTED:
            if self._atomic_bridge is not None:
                command = VenueOrderTransition(
                    event_key=f"reconcile:{intent.id}:{_evidence_hash(evidence, verification)}",
                    actor_id=self._actor_id,
                    source="RECONCILIATION",
                    state=VenueOrderState.REJECTED,
                    attempt_count=intent.attempt_count,
                    last_attempt_at=intent.last_attempt_at,
                    retry_not_before=None,
                    venue_order_id=evidence.venue_order_id,
                    filled_quantity=evidence.cumulative_filled,
                    last_event_at=evidence.checked_at,
                    evidence_hash=_evidence_hash(evidence, verification),
                )
                try:
                    await self._atomic_bridge.reconcile_order_rejected(
                        workspace_id=workspace_id,
                        account_id=account_id,
                        intent_id=intent_id,
                        reservation_id=intent.reservation_id,
                        expected_sequence=intent.transition_sequence,
                        command=command,
                    )
                    return VenueReconciliationResult(
                        VenueReconciliationStatus.APPLIED,
                        "REJECTED_RESERVATION_RELEASED",
                        intent_id,
                        VenueOrderState.REJECTED,
                        False,
                    )
                except Exception:
                    return self._held(intent_id, "PERSISTENCE_CONFLICT", intent)
            return self._held(intent_id, "REJECTED_SCHEMA_CONFLICT", intent)

        state = _OUTCOME_STATES[evidence.outcome]
        if evidence.outcome is ReconciliationOutcome.ORDER_OPEN and evidence.cumulative_filled:
            state = VenueOrderState.PARTIALLY_FILLED
        command = VenueOrderTransition(
            event_key=f"reconcile:{intent.id}:{_evidence_hash(evidence, verification)}",
            actor_id=self._actor_id,
            source="RECONCILIATION",
            state=state,
            attempt_count=intent.attempt_count,
            last_attempt_at=intent.last_attempt_at,
            retry_not_before=None,
            venue_order_id=evidence.venue_order_id,
            filled_quantity=evidence.cumulative_filled,
            last_event_at=evidence.checked_at,
            evidence_hash=_evidence_hash(evidence, verification),
        )
        try:
            await self._ledger.transition(
                workspace_id=workspace_id,
                account_id=account_id,
                intent_id=intent_id,
                expected_sequence=intent.transition_sequence,
                command=command,
            )
        except Exception:
            # Xung đột sequence, DB trigger hoặc lỗi transaction không được biến thành
            # bằng chứng thành công; worker có thể nạp lại và đối soát lại.
            return self._held(intent_id, "PERSISTENCE_CONFLICT", intent)
        return VenueReconciliationResult(
            VenueReconciliationStatus.APPLIED,
            "VERIFIED_VENUE_STATE_RECORDED",
            intent_id,
            state,
            True,
        )

    def _validate_evidence(
        self, intent: VenueOrderSnapshot, evidence: VenueReconciliation, now: datetime
    ) -> str | None:
        if type(evidence) is not VenueReconciliation:
            return "EVIDENCE_MALFORMED"
        if (
            evidence.workspace_id != str(intent.workspace_id)
            or evidence.account_id != str(intent.account_id)
            or evidence.client_order_id != intent.client_order_id
            or (
                intent.venue_order_id is not None
                and evidence.venue_order_id != intent.venue_order_id
            )
        ):
            return "EVIDENCE_SCOPE_MISMATCH"
        if now.tzinfo is None or now.utcoffset() is None:
            return "CLOCK_INVALID"
        now = now.astimezone(UTC)
        if evidence.checked_at > now or evidence.checked_at < now - self._max_evidence_age:
            return "EVIDENCE_STALE"
        known_times = [intent.created_at]
        if intent.last_attempt_at is not None:
            known_times.append(intent.last_attempt_at)
        if intent.last_event_at is not None:
            known_times.append(intent.last_event_at)
        if evidence.checked_at < max(value.astimezone(UTC) for value in known_times):
            return "EVIDENCE_PRECEDES_KNOWN_STATE"
        if evidence.cumulative_filled < intent.filled_quantity:
            return "FILL_REGRESSION"
        if evidence.cumulative_filled > intent.quantity:
            return "FILL_EXCEEDS_REQUEST"
        if (
            evidence.outcome is ReconciliationOutcome.FILLED
            and evidence.cumulative_filled != intent.quantity
        ):
            return "FILL_STATUS_MISMATCH"
        if (
            evidence.cumulative_filled == intent.quantity
            and evidence.outcome is not ReconciliationOutcome.FILLED
        ):
            return "FILL_STATUS_MISMATCH"
        if (
            evidence.outcome is ReconciliationOutcome.ORDER_OPEN
            and evidence.cumulative_filled >= intent.quantity
        ):
            return "OPEN_STATUS_MISMATCH"
        return None

    @staticmethod
    def _held(
        intent_id: UUID, reason: str, intent: VenueOrderSnapshot
    ) -> VenueReconciliationResult:
        try:
            state = VenueOrderState(intent.state)
        except ValueError:
            state = VenueOrderState.UNKNOWN
        return VenueReconciliationResult(
            VenueReconciliationStatus.HELD,
            reason,
            intent_id,
            state,
            True,
        )


def _evidence_hash(evidence: VenueReconciliation, verification: VerifiedVenueReconciliation) -> str:
    """Hash metadata chuẩn hóa, không giữ response raw hoặc thông tin nhạy cảm."""
    values: dict[str, Any] = {
        "outcome": evidence.outcome.value,
        "source": evidence.source.value,
        "source_ref": evidence.source_ref,
        "workspace_id": evidence.workspace_id,
        "account_id": evidence.account_id,
        "client_order_id": evidence.client_order_id,
        "checked_at": evidence.checked_at.astimezone(UTC).isoformat(),
        "cumulative_filled": str(evidence.cumulative_filled),
        "venue_order_id": evidence.venue_order_id,
        "intent_id": str(verification.intent_id),
        "bot_id": str(verification.bot_id),
        "reservation_id": str(verification.reservation_id),
    }
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class AtomicVenueReconciliationCoordinator:
    """Điều phối đối soát venue nguyên tử bao gồm ledger và risk reservations."""

    def __init__(
        self,
        service: VenueOrderReconciliationService,
    ) -> None:
        self._service = service

    async def reconcile(
        self, *, workspace_id: UUID, account_id: UUID, intent_id: UUID
    ) -> VenueReconciliationResult:
        """Thực thi đối soát và trả về kết quả an toàn fail-closed."""
        return await self._service.reconcile_unknown(
            workspace_id=workspace_id, account_id=account_id, intent_id=intent_id
        )
