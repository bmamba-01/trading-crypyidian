"""Unit test cho Health & Correlation Snapshot kết hợp Circuit Breaker và Loss Windows (OPS07A)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_bot.domain.risk.circuit_breaker import (
    BreakerState,
    CircuitBreakerEngine,
    CircuitBreakerPolicy,
    OperationalTelemetry,
)
from trading_bot.domain.risk.risk_windows import (
    LossWindowPolicy,
    RealizedTradePnlEvent,
    calculate_loss_windows,
)
from trading_bot.observability.health import (
    SystemHealthStatus,
    build_system_health_snapshot,
)
from trading_bot.observability.redaction import contains_secret_canary


def test_health_snapshot_healthy_and_redacts_secrets() -> None:
    """Snapshot sạch báo HEALTHY, mang correlation_id và che toàn bộ secret trong metadata."""
    now = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
    loss_snapshot = calculate_loss_windows(
        events=[
            RealizedTradePnlEvent(
                trade_id="t-1",
                closed_at=now - timedelta(hours=1),
                net_pnl=Decimal("-25"),
            )
        ],
        as_of=now,
        policy=LossWindowPolicy(),
    )
    breaker_snap = CircuitBreakerEngine().evaluate(
        OperationalTelemetry(
            evaluated_at=now,
            feed_age_seconds=2,
            daily_loss=loss_snapshot.realized_loss_today,
            drawdown_pct=Decimal("1.0"),
            consecutive_api_errors=0,
            outbox_lag_seconds=1,
            clock_skew_ms=10,
            unprotected_exposure_count=0,
            reconciliation_discrepancy_count=0,
            unknown_order_count=0,
        )
    )
    canary = "CANARY_HEALTH_SECRET_777"
    health = build_system_health_snapshot(
        service_name="crypidian-admin",
        environment="PAPER",
        correlation_id="corr-health-001",
        breaker=breaker_snap,
        loss_windows=loss_snapshot,
        metadata={"api_key": canary, "region": "ap-southeast-1"},
    )
    payload = health.to_dict()
    assert health.status is SystemHealthStatus.HEALTHY
    assert payload["status"] == "HEALTHY"
    assert payload["correlation_id"] == "corr-health-001"
    assert payload["breaker"]["state"] == BreakerState.CLOSED.value
    assert payload["loss_windows"]["realized_loss_today"] == "25"
    assert payload["metadata"]["region"] == "ap-southeast-1"
    assert payload["metadata"]["api_key"] == "[REDACTED]"
    assert not contains_secret_canary(payload, [canary])


def test_health_snapshot_marks_halted_when_breaker_open() -> None:
    """Khi Circuit Breaker mở (OPEN) hoặc COOLDOWN_PASSED, hệ thống báo HALTED/DEGRADED."""
    now = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
    loss_snapshot = calculate_loss_windows(events=[], as_of=now)
    engine = CircuitBreakerEngine(policy=CircuitBreakerPolicy(max_feed_age_seconds=10))
    tripped = engine.evaluate(
        OperationalTelemetry(
            evaluated_at=now,
            feed_age_seconds=60,
            daily_loss=Decimal("0"),
            drawdown_pct=Decimal("0"),
            consecutive_api_errors=0,
            outbox_lag_seconds=0,
            clock_skew_ms=0,
            unprotected_exposure_count=0,
            reconciliation_discrepancy_count=0,
            unknown_order_count=0,
        )
    )
    health = build_system_health_snapshot(
        service_name="crypidian-admin",
        environment="PAPER",
        correlation_id="corr-health-002",
        breaker=tripped,
        loss_windows=loss_snapshot,
    )
    assert health.status is SystemHealthStatus.HALTED
    assert health.to_dict()["breaker"]["entry_allowed"] is False
