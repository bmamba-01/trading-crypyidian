"""Ảnh chụp sức khỏe hệ thống, đối chiếu tương quan (correlation_id) và bảo vệ bí mật (OPS07A)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from trading_bot.domain.risk.circuit_breaker import BreakerState, CircuitBreakerSnapshot
from trading_bot.domain.risk.risk_windows import LossWindowSnapshot
from trading_bot.observability.redaction import redact_value


class SystemHealthStatus(StrEnum):
    """Trạng thái sức khỏe tổng hợp của hệ thống giao dịch."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    HALTED = "HALTED"


@dataclass(frozen=True)
class SystemHealthSnapshot:
    """Ảnh chụp sức khỏe vận hành có thể serialize an toàn ra API hoặc log."""

    service_name: str
    environment: str
    status: SystemHealthStatus
    correlation_id: str
    evaluated_at: datetime
    breaker: CircuitBreakerSnapshot
    loss_windows: LossWindowSnapshot
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Chuyển sang dict JSON-safe đã che toàn bộ thông tin nhạy cảm."""
        raw = {
            "service_name": self.service_name,
            "environment": self.environment,
            "status": self.status.value,
            "correlation_id": self.correlation_id,
            "evaluated_at": self.evaluated_at.isoformat(),
            "breaker": {
                "state": self.breaker.state.value,
                "entry_allowed": self.breaker.entry_allowed,
                "protective_exit_allowed": self.breaker.protective_exit_allowed,
                "tripped_reasons": [r.value for r in self.breaker.tripped_reasons],
                "detail_vi": self.breaker.detail_vi,
                "tripped_at": (
                    self.breaker.tripped_at.isoformat() if self.breaker.tripped_at else None
                ),
                "cooldown_until": (
                    self.breaker.cooldown_until.isoformat() if self.breaker.cooldown_until else None
                ),
                "revision": self.breaker.revision,
                "evidence": self.breaker.evidence,
            },
            "loss_windows": {
                "as_of": self.loss_windows.as_of.isoformat(),
                "timezone_name": self.loss_windows.timezone_name,
                "realized_loss_today": str(self.loss_windows.realized_loss_today),
                "realized_loss_rolling": str(self.loss_windows.realized_loss_rolling),
                "gross_loss_today": str(self.loss_windows.gross_loss_today),
                "gross_loss_rolling": str(self.loss_windows.gross_loss_rolling),
                "trade_count_today": self.loss_windows.trade_count_today,
                "trade_count_rolling": self.loss_windows.trade_count_rolling,
            },
            "metadata": self.metadata,
        }
        return redact_value(raw)


def build_system_health_snapshot(
    *,
    service_name: str,
    environment: str,
    correlation_id: str,
    breaker: CircuitBreakerSnapshot,
    loss_windows: LossWindowSnapshot,
    metadata: Mapping[str, Any] | None = None,
    evaluated_at: datetime | None = None,
) -> SystemHealthSnapshot:
    """Tổng hợp trạng thái Circuit Breaker và Loss Windows thành ảnh chụp sức khỏe an toàn."""
    if breaker.state is BreakerState.OPEN:
        status = SystemHealthStatus.HALTED
    elif breaker.state is BreakerState.COOLDOWN_PASSED:
        status = SystemHealthStatus.DEGRADED
    else:
        status = SystemHealthStatus.HEALTHY

    safe_metadata = redact_value(dict(metadata or {}))
    return SystemHealthSnapshot(
        service_name=service_name,
        environment=environment,
        status=status,
        correlation_id=correlation_id,
        evaluated_at=evaluated_at or datetime.now(UTC),
        breaker=breaker,
        loss_windows=loss_windows,
        metadata=safe_metadata,
    )
