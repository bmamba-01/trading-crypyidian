"""Circuit Breaker Engine và quy trình phục hồi có kiểm soát cho hệ thống giao dịch (OPS07B)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any


class BreakerTriggerKind(StrEnum):
    """Phân loại nguyên nhân kích hoạt ngắt mạch tự động."""

    FEED_STALE = "FEED_STALE"
    DAILY_LOSS_BREACH = "DAILY_LOSS_BREACH"
    MAX_DRAWDOWN_BREACH = "MAX_DRAWDOWN_BREACH"
    API_ERROR_BURST = "API_ERROR_BURST"
    OUTBOX_QUEUE_LAG = "OUTBOX_QUEUE_LAG"
    CLOCK_SKEW = "CLOCK_SKEW"
    MISSING_PROTECTION = "MISSING_PROTECTION"
    RECONCILIATION_DISCREPANCY = "RECONCILIATION_DISCREPANCY"


class BreakerState(StrEnum):
    """Trạng thái máy hữu hạn của Circuit Breaker."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    COOLDOWN_PASSED = "COOLDOWN_PASSED"


class CircuitBreakerResumeDenied(RuntimeError):
    """Ngoại lệ khi yêu cầu mở lại giao dịch không đáp ứng đủ điều kiện an toàn."""


@dataclass(frozen=True)
class CircuitBreakerPolicy:
    """Ngưỡng định lượng để tự động kích hoạt ngắt mạch và thời gian cooldown tối thiểu."""

    max_feed_age_seconds: int = 30
    max_daily_loss: Decimal = Decimal("500")
    max_drawdown_pct: Decimal = Decimal("10")
    max_consecutive_api_errors: int = 5
    max_outbox_lag_seconds: int = 60
    max_clock_skew_ms: int = 1000
    cooldown_period: timedelta = timedelta(minutes=15)


@dataclass(frozen=True)
class OperationalTelemetry:
    """Ảnh chụp telemetry vận hành tại thời điểm đánh giá ngắt mạch."""

    evaluated_at: datetime
    feed_age_seconds: int
    daily_loss: Decimal
    drawdown_pct: Decimal
    consecutive_api_errors: int
    outbox_lag_seconds: int
    clock_skew_ms: int
    unprotected_exposure_count: int
    reconciliation_discrepancy_count: int
    unknown_order_count: int


@dataclass(frozen=True)
class CircuitBreakerSnapshot:
    """Trạng thái hiện hành của Circuit Breaker kèm bằng chứng kiểm toán."""

    state: BreakerState
    entry_allowed: bool
    protective_exit_allowed: bool
    tripped_reasons: tuple[BreakerTriggerKind, ...]
    detail_vi: str
    tripped_at: datetime | None
    cooldown_until: datetime | None
    revision: int
    evidence: dict[str, Any] = field(default_factory=dict)


class CircuitBreakerEngine:
    """Động cơ ngắt mạch tự động: chặn mở vị thế mới khi có rủi ro, luôn giữ lối thoát bảo vệ."""

    def __init__(self, policy: CircuitBreakerPolicy | None = None) -> None:
        self._policy = policy or CircuitBreakerPolicy()
        self._state = BreakerState.CLOSED
        self._tripped_reasons: tuple[BreakerTriggerKind, ...] = ()
        self._tripped_at: datetime | None = None
        self._cooldown_until: datetime | None = None
        self._revision = 1
        self._detail_vi = "Hệ thống vận hành bình thường; cổng vào lệnh đang mở."
        self._evidence: dict[str, Any] = {}

    @property
    def policy(self) -> CircuitBreakerPolicy:
        """Trả về chính sách ngưỡng hiện hành."""
        return self._policy

    def _detect_triggers(self, telemetry: OperationalTelemetry) -> tuple[BreakerTriggerKind, ...]:
        reasons: list[BreakerTriggerKind] = []
        p = self._policy
        if telemetry.feed_age_seconds > p.max_feed_age_seconds:
            reasons.append(BreakerTriggerKind.FEED_STALE)
        if telemetry.daily_loss > p.max_daily_loss:
            reasons.append(BreakerTriggerKind.DAILY_LOSS_BREACH)
        if telemetry.drawdown_pct > p.max_drawdown_pct:
            reasons.append(BreakerTriggerKind.MAX_DRAWDOWN_BREACH)
        if telemetry.consecutive_api_errors > p.max_consecutive_api_errors:
            reasons.append(BreakerTriggerKind.API_ERROR_BURST)
        if telemetry.outbox_lag_seconds > p.max_outbox_lag_seconds:
            reasons.append(BreakerTriggerKind.OUTBOX_QUEUE_LAG)
        if telemetry.clock_skew_ms > p.max_clock_skew_ms:
            reasons.append(BreakerTriggerKind.CLOCK_SKEW)
        if telemetry.unprotected_exposure_count > 0:
            reasons.append(BreakerTriggerKind.MISSING_PROTECTION)
        if telemetry.reconciliation_discrepancy_count > 0 or telemetry.unknown_order_count > 0:
            reasons.append(BreakerTriggerKind.RECONCILIATION_DISCREPANCY)
        return tuple(reasons)

    def _build_snapshot(self) -> CircuitBreakerSnapshot:
        return CircuitBreakerSnapshot(
            state=self._state,
            entry_allowed=self._state is BreakerState.CLOSED,
            protective_exit_allowed=True,
            tripped_reasons=self._tripped_reasons,
            detail_vi=self._detail_vi,
            tripped_at=self._tripped_at,
            cooldown_until=self._cooldown_until,
            revision=self._revision,
            evidence=dict(self._evidence),
        )

    def evaluate(self, telemetry: OperationalTelemetry) -> CircuitBreakerSnapshot:
        """Đánh giá telemetry mới và cập nhật trạng thái ngắt mạch."""
        reasons = self._detect_triggers(telemetry)
        if reasons:
            self._state = BreakerState.OPEN
            self._tripped_reasons = reasons
            if self._tripped_at is None:
                self._tripped_at = telemetry.evaluated_at
            self._cooldown_until = telemetry.evaluated_at + self._policy.cooldown_period
            self._revision += 1
            self._detail_vi = (
                "Đã kích hoạt ngắt mạch ("
                + ", ".join(r.value for r in reasons)
                + "); chặn mở vị thế mới, vẫn cho phép thoát lệnh bảo vệ."
            )
            self._evidence = {
                "evaluated_at": telemetry.evaluated_at.isoformat(),
                "feed_age_seconds": telemetry.feed_age_seconds,
                "daily_loss": str(telemetry.daily_loss),
                "drawdown_pct": str(telemetry.drawdown_pct),
                "consecutive_api_errors": telemetry.consecutive_api_errors,
                "outbox_lag_seconds": telemetry.outbox_lag_seconds,
                "clock_skew_ms": telemetry.clock_skew_ms,
                "unprotected_exposure_count": telemetry.unprotected_exposure_count,
                "reconciliation_discrepancy_count": telemetry.reconciliation_discrepancy_count,
                "unknown_order_count": telemetry.unknown_order_count,
            }
            return self._build_snapshot()

        if self._state is BreakerState.OPEN:
            if self._cooldown_until is not None and telemetry.evaluated_at >= self._cooldown_until:
                self._state = BreakerState.COOLDOWN_PASSED
                self._revision += 1
                self._detail_vi = (
                    "Đã hết thời gian cooldown và telemetry sạch; "
                    "đang chờ người vận hành phê duyệt MFA để mở lại."
                )
        return self._build_snapshot()

    def request_resume(
        self,
        *,
        now: datetime,
        telemetry: OperationalTelemetry,
        operator_id: str,
        mfa_verified: bool,
        approval_reason: str,
    ) -> CircuitBreakerSnapshot:
        """Yêu cầu khôi phục trạng thái CLOSED sau khi kiểm tra đủ cooldown, đối soát và MFA."""
        if self._state is BreakerState.CLOSED:
            return self._build_snapshot()

        if self._cooldown_until is not None and now < self._cooldown_until:
            raise CircuitBreakerResumeDenied(
                "Chưa thể mở lại: chưa hết thời gian cooldown an toàn."
            )

        if not mfa_verified:
            raise CircuitBreakerResumeDenied(
                "Chưa thể mở lại: yêu cầu xác thực MFA của người vận hành."
            )

        if not operator_id.strip() or not approval_reason.strip():
            raise CircuitBreakerResumeDenied(
                "Chưa thể mở lại: cần ghi rõ mã người vận hành và lý do phê duyệt."
            )

        if telemetry.reconciliation_discrepancy_count > 0 or telemetry.unknown_order_count > 0:
            raise CircuitBreakerResumeDenied(
                "Chưa thể mở lại: còn lệnh UNKNOWN hoặc sai lệch đối soát chưa xử lý xong."
            )

        active_triggers = self._detect_triggers(telemetry)
        if active_triggers:
            raise CircuitBreakerResumeDenied(
                "Chưa thể mở lại: telemetry sức khỏe vẫn còn cảnh báo ("
                + ", ".join(t.value for t in active_triggers)
                + ")."
            )

        self._state = BreakerState.CLOSED
        self._tripped_reasons = ()
        self._tripped_at = None
        self._cooldown_until = None
        self._revision += 1
        self._detail_vi = f"Đã khôi phục vận hành bởi {operator_id} (MFA hợp lệ): {approval_reason}"
        self._evidence = {
            "resumed_at": now.isoformat(),
            "operator_id": operator_id,
            "mfa_verified": True,
            "approval_reason": approval_reason,
        }
        return self._build_snapshot()
