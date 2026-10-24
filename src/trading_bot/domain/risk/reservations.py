"""Hợp đồng giữ ngân sách risk chống vượt trần khi nhiều yêu cầu đồng thời."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from threading import RLock
from uuid import UUID, uuid4

from trading_bot.domain.risk.engine import RiskDecision


class ReservationConflict(RuntimeError):
    """Yêu cầu giữ risk không hợp lệ, trùng khóa sai plan hoặc vượt trần."""


class ReservationStatus(StrEnum):
    RESERVED = "RESERVED"
    UNKNOWN = "UNKNOWN"
    RELEASED = "RELEASED"
    TRANSFERRED = "TRANSFERRED"


class ReservationOutcome(StrEnum):
    """Kết quả reconcile từ nguồn order/exposure đáng tin cậy."""

    UNRESOLVED = "UNRESOLVED"
    ORDER_ABSENT = "ORDER_ABSENT"
    OPEN_RISK_RECORDED = "OPEN_RISK_RECORDED"


class ReservationReleaseReason(StrEnum):
    """Lý do giải phóng ngân sách trước khi có lệnh hoặc sau reconcile."""

    EXPIRED_BEFORE_SUBMIT = "EXPIRED_BEFORE_SUBMIT"
    CANCELLED_BEFORE_SUBMIT = "CANCELLED_BEFORE_SUBMIT"
    EXPIRED_NO_FILL = "EXPIRED_NO_FILL"
    CANCELLED_NO_FILL = "CANCELLED_NO_FILL"
    ORDER_ABSENT = "ORDER_ABSENT"
    OPEN_RISK_RECORDED = "OPEN_RISK_RECORDED"


@dataclass(frozen=True, slots=True)
class RiskReservation:
    """Bản ghi bất biến của risk đang được giữ cho một order intent."""

    reservation_id: UUID
    workspace_id: str
    account_id: str
    idempotency_key: str
    plan_hash: str
    reserved_risk: Decimal
    status: ReservationStatus
    expires_at: datetime
    symbol: str = ""
    reserved_notional: Decimal = Decimal("0")
    resolution_reason: ReservationReleaseReason | None = None
    reserved_initial_margin: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ReservationConflict("Reservation expiry cần timezone.")


class RiskReservationBook:
    """Kho domain đồng bộ; adapter DB cần thay khóa trong tiến trình bằng row lock.

    RiskDecision.open_risk/count là baseline đã lưu, không gồm reservation mới
    do chính instance book này nắm giữ; adapter phải giữ cùng semantics.
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._lock = RLock()
        self._items: dict[UUID, RiskReservation] = {}
        self._keys: dict[tuple[str, str, str], UUID] = {}
        self._clock = clock or (lambda: datetime.now(UTC))

    def reserve(self, decision: RiskDecision, idempotency_key: str) -> RiskReservation:
        """Giữ risk nếu decision được duyệt và tổng risk sau giữ không vượt cap."""
        if not decision.approved:
            raise ReservationConflict("Chỉ RiskDecision đã approved mới được reserve.")
        now = self._clock()
        _require_aware(now, "clock")
        _require_aware(decision.candidate_expires_at, "candidate_expires_at")
        if now >= decision.candidate_expires_at:
            raise ReservationConflict("Candidate đã hết hạn trước khi reserve.")
        if not idempotency_key.strip():
            raise ReservationConflict("idempotency key không được để trống.")
        values = (
            decision.requested_risk,
            decision.open_risk,
            decision.max_aggregate_open_risk,
            decision.requested_notional,
            decision.open_notional,
            decision.symbol_open_notional,
            decision.max_notional,
            decision.max_symbol_notional,
            decision.pending_reservation_risk,
            decision.pending_reservation_notional,
            decision.pending_symbol_reservation_notional,
            decision.available_margin,
            decision.requested_initial_margin,
            decision.pending_reservation_initial_margin,
        )
        if any(not value.is_finite() or value < Decimal("0") for value in values):
            raise ReservationConflict("Risk amount phải là Decimal hữu hạn không âm.")
        if decision.requested_initial_margin <= Decimal("0"):
            raise ReservationConflict("Initial margin cần reserve phải lớn hơn 0.")
        key = (decision.workspace_id, decision.account_id, idempotency_key)
        with self._lock:
            existing_id = self._keys.get(key)
            if existing_id is not None:
                existing = self._items[existing_id]
                if (
                    existing.plan_hash != decision.plan_hash
                    or existing.reserved_risk != decision.requested_risk
                    or existing.symbol != decision.symbol
                    or existing.reserved_notional != decision.requested_notional
                    or existing.reserved_initial_margin != decision.requested_initial_margin
                ):
                    raise ReservationConflict("idempotency key đã được dùng cho plan khác.")
                if now >= existing.expires_at:
                    raise ReservationConflict("Reservation đã hết hạn; cần idempotency key mới.")
                if existing.status is ReservationStatus.RELEASED:
                    raise ReservationConflict("Reservation đã ở trạng thái terminal.")
                return existing
            self._expire_due_unlocked(now)
            held = self._active_risk_unlocked(decision.workspace_id, decision.account_id)
            held_count = self._active_count_unlocked(decision.workspace_id, decision.account_id)
            held_notional = self._active_notional_unlocked(
                decision.workspace_id, decision.account_id
            )
            held_symbol_notional = self._active_notional_unlocked(
                decision.workspace_id, decision.account_id, symbol=decision.symbol
            )
            if (
                decision.open_risk + held + decision.requested_risk
                > decision.max_aggregate_open_risk
            ):
                raise ReservationConflict("Aggregate open risk vượt giới hạn sau reservation.")
            if (
                decision.open_notional
                - decision.pending_reservation_notional
                + held_notional
                + decision.requested_notional
                > decision.max_notional
            ):
                raise ReservationConflict("Tổng notional vượt giới hạn sau reservation.")
            if (
                decision.symbol_open_notional
                - decision.pending_symbol_reservation_notional
                + held_symbol_notional
                + decision.requested_notional
                > decision.max_symbol_notional
            ):
                raise ReservationConflict("Notional tập trung theo symbol vượt giới hạn.")
            if (
                decision.open_position_count + decision.pending_reservation_count + held_count + 1
                > decision.max_positions
            ):
                raise ReservationConflict("Số vị thế mở và reservation vượt giới hạn.")
            held_margin = self._active_initial_margin_unlocked(
                decision.workspace_id, decision.account_id
            )
            if held_margin + decision.requested_initial_margin > decision.available_margin:
                raise ReservationConflict("Initial margin vượt margin khả dụng sau reservation.")
            reservation = RiskReservation(
                reservation_id=uuid4(),
                workspace_id=decision.workspace_id,
                account_id=decision.account_id,
                idempotency_key=idempotency_key,
                plan_hash=decision.plan_hash,
                reserved_risk=decision.requested_risk,
                status=ReservationStatus.RESERVED,
                expires_at=decision.candidate_expires_at,
                symbol=decision.symbol,
                reserved_notional=decision.requested_notional,
                reserved_initial_margin=decision.requested_initial_margin,
            )
            self._items[reservation.reservation_id] = reservation
            self._keys[key] = reservation.reservation_id
            return reservation

    def mark_unknown(self, reservation_id: UUID) -> RiskReservation:
        """Giữ nguyên ngân sách khi kết quả submit chưa xác định."""
        with self._lock:
            item = self._get(reservation_id)
            if item.status is ReservationStatus.RELEASED:
                raise ReservationConflict("Reservation đã release không thể chuyển UNKNOWN.")
            now = self._clock()
            _require_aware(now, "clock")
            if item.status is ReservationStatus.RESERVED and now >= item.expires_at:
                raise ReservationConflict("Reservation đã hết hạn trước khi submit.")
            updated = replace(item, status=ReservationStatus.UNKNOWN)
            self._items[reservation_id] = updated
            return updated

    def reconcile(self, reservation_id: UUID, *, outcome: ReservationOutcome) -> RiskReservation:
        """Giải phóng khi không có order hoặc chuyển sau khi open risk đã được ghi.

        Adapter DB phải ghi exposure trước/đồng thời với trạng thái TRANSFERRED
        trong cùng transaction để không tạo khoảng thời gian thiếu giữ risk.
        """
        with self._lock:
            item = self._get(reservation_id)
            if item.status in {ReservationStatus.RELEASED, ReservationStatus.TRANSFERRED}:
                expected_reason = {
                    ReservationOutcome.ORDER_ABSENT: ReservationReleaseReason.ORDER_ABSENT,
                    ReservationOutcome.OPEN_RISK_RECORDED: (
                        ReservationReleaseReason.OPEN_RISK_RECORDED
                    ),
                }.get(outcome)
                if expected_reason is not None and item.resolution_reason is expected_reason:
                    return item
                raise ReservationConflict("Kết quả reconcile mâu thuẫn với trạng thái đã ghi.")
            if item.status is not ReservationStatus.UNKNOWN:
                raise ReservationConflict("Chỉ reservation UNKNOWN mới được reconcile.")
            if outcome is ReservationOutcome.UNRESOLVED:
                return item
            if outcome is ReservationOutcome.OPEN_RISK_RECORDED:
                target_status = ReservationStatus.TRANSFERRED
            elif outcome is ReservationOutcome.ORDER_ABSENT:
                target_status = ReservationStatus.RELEASED
            else:
                raise ReservationConflict("Kết quả reconcile không hợp lệ.")
            updated = replace(
                item,
                status=target_status,
                resolution_reason=(
                    ReservationReleaseReason.ORDER_ABSENT
                    if outcome is ReservationOutcome.ORDER_ABSENT
                    else ReservationReleaseReason.OPEN_RISK_RECORDED
                ),
            )
            self._items[reservation_id] = updated
            return updated

    def release_before_submit(
        self,
        reservation_id: UUID,
        *,
        reason: ReservationReleaseReason,
        order_was_submitted: bool,
    ) -> RiskReservation:
        """Giải phóng RESERVED chỉ khi xác nhận lệnh chưa được gửi tới sàn."""
        with self._lock:
            item = self._get(reservation_id)
            if order_was_submitted:
                raise ReservationConflict("Không thể release nếu order đã được submit.")
            if item.status is ReservationStatus.RELEASED:
                if item.resolution_reason is reason:
                    return item
                raise ReservationConflict("Lý do release mâu thuẫn với trạng thái đã ghi.")
            if item.status is not ReservationStatus.RESERVED:
                raise ReservationConflict("Chỉ reservation RESERVED mới release trước submit.")
            if reason not in {
                ReservationReleaseReason.EXPIRED_BEFORE_SUBMIT,
                ReservationReleaseReason.CANCELLED_BEFORE_SUBMIT,
            }:
                raise ReservationConflict("Lý do release trước submit không hợp lệ.")
            if reason is ReservationReleaseReason.EXPIRED_BEFORE_SUBMIT:
                now = self._clock()
                _require_aware(now, "clock")
                if now < item.expires_at:
                    raise ReservationConflict("Reservation chưa hết hạn.")
            updated = replace(item, status=ReservationStatus.RELEASED, resolution_reason=reason)
            self._items[reservation_id] = updated
            return updated

    def expire_due(self, *, now: datetime | None = None) -> int:
        """Giải phóng RESERVED đã quá hạn; UNKNOWN luôn cần reconcile riêng."""
        current_time = self._clock() if now is None else now
        _require_aware(current_time, "now")
        with self._lock:
            return self._expire_due_unlocked(current_time)

    def active_risk(self, workspace_id: str, account_id: str) -> Decimal:
        """Tính risk còn giữ; cả RESERVED và UNKNOWN đều được cộng."""
        with self._lock:
            self._expire_due_unlocked(self._clock())
            return self._active_risk_unlocked(workspace_id, account_id)

    def active_count(self, workspace_id: str, account_id: str) -> int:
        """Đếm reservation đang giữ cả RESERVED và UNKNOWN."""
        with self._lock:
            self._expire_due_unlocked(self._clock())
            return self._active_count_unlocked(workspace_id, account_id)

    def active_initial_margin(self, workspace_id: str, account_id: str) -> Decimal:
        """Tổng initial margin của reservation RESERVED và UNKNOWN."""
        with self._lock:
            self._expire_due_unlocked(self._clock())
            return self._active_initial_margin_unlocked(workspace_id, account_id)

    def _expire_due_unlocked(self, now: datetime) -> int:
        _require_aware(now, "now")
        due = [
            item
            for item in self._items.values()
            if item.status is ReservationStatus.RESERVED and now >= item.expires_at
        ]
        for item in due:
            self._items[item.reservation_id] = replace(
                item,
                status=ReservationStatus.RELEASED,
                resolution_reason=ReservationReleaseReason.EXPIRED_BEFORE_SUBMIT,
            )
        return len(due)

    def _active_count_unlocked(self, workspace_id: str, account_id: str) -> int:
        return sum(
            item.workspace_id == workspace_id
            and item.account_id == account_id
            and item.status in {ReservationStatus.RESERVED, ReservationStatus.UNKNOWN}
            for item in self._items.values()
        )

    def _active_risk_unlocked(self, workspace_id: str, account_id: str) -> Decimal:
        return sum(
            (
                item.reserved_risk
                for item in self._items.values()
                if item.workspace_id == workspace_id
                and item.account_id == account_id
                and item.status in {ReservationStatus.RESERVED, ReservationStatus.UNKNOWN}
            ),
            Decimal("0"),
        )

    def _active_notional_unlocked(
        self, workspace_id: str, account_id: str, *, symbol: str | None = None
    ) -> Decimal:
        return sum(
            (
                item.reserved_notional
                for item in self._items.values()
                if item.workspace_id == workspace_id
                and item.account_id == account_id
                and item.status in {ReservationStatus.RESERVED, ReservationStatus.UNKNOWN}
                and (symbol is None or item.symbol == symbol)
            ),
            Decimal("0"),
        )

    def _active_initial_margin_unlocked(self, workspace_id: str, account_id: str) -> Decimal:
        return sum(
            (
                item.reserved_initial_margin
                for item in self._items.values()
                if item.workspace_id == workspace_id
                and item.account_id == account_id
                and item.status in {ReservationStatus.RESERVED, ReservationStatus.UNKNOWN}
            ),
            Decimal("0"),
        )

    def _get(self, reservation_id: UUID) -> RiskReservation:
        try:
            return self._items[reservation_id]
        except KeyError as error:
            raise ReservationConflict("Không tìm thấy reservation.") from error


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReservationConflict(f"{name} cần timezone.")
