"""Unit test cho Circuit Breaker Engine và cơ chế phục hồi có kiểm soát (OPS07B)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain.risk.circuit_breaker import (
    BreakerState,
    BreakerTriggerKind,
    CircuitBreakerEngine,
    CircuitBreakerPolicy,
    CircuitBreakerResumeDenied,
    OperationalTelemetry,
)

NOW = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)


def _clean_telemetry(at: datetime = NOW) -> OperationalTelemetry:
    return OperationalTelemetry(
        evaluated_at=at,
        feed_age_seconds=2,
        daily_loss=Decimal("50"),
        drawdown_pct=Decimal("1.5"),
        consecutive_api_errors=0,
        outbox_lag_seconds=1,
        clock_skew_ms=20,
        unprotected_exposure_count=0,
        reconciliation_discrepancy_count=0,
        unknown_order_count=0,
    )


def test_circuit_breaker_trips_on_threshold_breach_and_keeps_protective_exit() -> None:
    """Khi vượt ngưỡng lỗ ngày hoặc feed cũ, đóng entry gate nhưng vẫn cho thoát lệnh bảo vệ."""
    engine = CircuitBreakerEngine(
        policy=CircuitBreakerPolicy(max_daily_loss=Decimal("200"), max_feed_age_seconds=30)
    )
    bad = OperationalTelemetry(
        evaluated_at=NOW,
        feed_age_seconds=45,
        daily_loss=Decimal("250"),
        drawdown_pct=Decimal("2.0"),
        consecutive_api_errors=0,
        outbox_lag_seconds=1,
        clock_skew_ms=10,
        unprotected_exposure_count=0,
        reconciliation_discrepancy_count=0,
        unknown_order_count=0,
    )

    snap = engine.evaluate(bad)
    assert snap.state is BreakerState.OPEN
    assert snap.entry_allowed is False
    assert snap.protective_exit_allowed is True
    assert BreakerTriggerKind.FEED_STALE in snap.tripped_reasons
    assert BreakerTriggerKind.DAILY_LOSS_BREACH in snap.tripped_reasons
    assert snap.revision == 2


def test_circuit_breaker_cannot_be_cleared_by_single_clean_health_ping() -> None:
    """Health ping sạch đơn lẻ không được tự mở lại entry khi chưa qua cooldown và phê duyệt."""
    engine = CircuitBreakerEngine(
        policy=CircuitBreakerPolicy(cooldown_period=timedelta(minutes=15))
    )
    tripped = OperationalTelemetry(
        evaluated_at=NOW,
        feed_age_seconds=5,
        daily_loss=Decimal("10"),
        drawdown_pct=Decimal("1.0"),
        consecutive_api_errors=6,
        outbox_lag_seconds=1,
        clock_skew_ms=10,
        unprotected_exposure_count=0,
        reconciliation_discrepancy_count=0,
        unknown_order_count=0,
    )
    engine.evaluate(tripped)

    # Ping sạch sau 5 phút -> vẫn OPEN và entry_allowed = False
    snap_5m = engine.evaluate(_clean_telemetry(NOW + timedelta(minutes=5)))
    assert snap_5m.state is BreakerState.OPEN
    assert snap_5m.entry_allowed is False

    # Ping sạch sau 20 phút (qua cooldown) -> chuyển COOLDOWN_PASSED nhưng VẪN CHỜ duyệt
    snap_20m = engine.evaluate(_clean_telemetry(NOW + timedelta(minutes=20)))
    assert snap_20m.state is BreakerState.COOLDOWN_PASSED
    assert snap_20m.entry_allowed is False


def test_circuit_breaker_resume_requires_cooldown_clean_health_and_mfa_approval() -> None:
    """Khôi phục đòi hỏi đủ cooldown + health sạch (0 UNKNOWN) + MFA + lý do tiếng Việt."""
    engine = CircuitBreakerEngine(
        policy=CircuitBreakerPolicy(cooldown_period=timedelta(minutes=15))
    )
    engine.evaluate(
        OperationalTelemetry(
            evaluated_at=NOW,
            feed_age_seconds=5,
            daily_loss=Decimal("10"),
            drawdown_pct=Decimal("1.0"),
            consecutive_api_errors=0,
            outbox_lag_seconds=1,
            clock_skew_ms=2000,  # Lệch đồng hồ
            unprotected_exposure_count=0,
            reconciliation_discrepancy_count=0,
            unknown_order_count=1,
        )
    )

    # Từ chối nếu chưa hết cooldown
    with pytest.raises(CircuitBreakerResumeDenied, match="cooldown"):
        engine.request_resume(
            now=NOW + timedelta(minutes=5),
            telemetry=_clean_telemetry(NOW + timedelta(minutes=5)),
            operator_id="owner@crypidian.io",
            mfa_verified=True,
            approval_reason="Đã kiểm tra đồng hồ NTP",
        )

    # Từ chối nếu thiếu MFA
    with pytest.raises(CircuitBreakerResumeDenied, match="MFA"):
        engine.request_resume(
            now=NOW + timedelta(minutes=20),
            telemetry=_clean_telemetry(NOW + timedelta(minutes=20)),
            operator_id="owner@crypidian.io",
            mfa_verified=False,
            approval_reason="Đã kiểm tra đồng hồ NTP",
        )

    # Từ chối nếu còn UNKNOWN order chưa đối soát sạch
    dirty_reconcile = OperationalTelemetry(
        evaluated_at=NOW + timedelta(minutes=20),
        feed_age_seconds=2,
        daily_loss=Decimal("10"),
        drawdown_pct=Decimal("1.0"),
        consecutive_api_errors=0,
        outbox_lag_seconds=1,
        clock_skew_ms=10,
        unprotected_exposure_count=0,
        reconciliation_discrepancy_count=0,
        unknown_order_count=1,
    )
    with pytest.raises(CircuitBreakerResumeDenied, match="đối soát"):
        engine.request_resume(
            now=NOW + timedelta(minutes=20),
            telemetry=dirty_reconcile,
            operator_id="owner@crypidian.io",
            mfa_verified=True,
            approval_reason="Thử mở lại khi còn lệnh UNKNOWN",
        )

    # Thành công khi đủ mọi điều kiện
    resumed = engine.request_resume(
        now=NOW + timedelta(minutes=20),
        telemetry=_clean_telemetry(NOW + timedelta(minutes=20)),
        operator_id="owner@crypidian.io",
        mfa_verified=True,
        approval_reason="Đồng bộ NTP hoàn tất, toàn bộ lệnh đối soát sạch",
    )
    assert resumed.state is BreakerState.CLOSED
    assert resumed.entry_allowed is True
    assert resumed.protective_exit_allowed is True
