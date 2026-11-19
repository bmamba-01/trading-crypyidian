"""Unit tests cho SEC-32 (phủ audit bảo mật) và SEC-34 (cảnh báo bất thường)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from trading_bot.adapters.database.models import AuditEvent, OutboxEvent, SecretReference
from trading_bot.app import create_app
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.route_policy import CsrfMode, RouteAccess, audit_app_routes
from trading_bot.auth.service import AuthIdentity
from trading_bot.observability.redaction import contains_secret_canary
from trading_bot.observability.security_alerts import (
    InMemorySecurityAlertSink,
    LoggingSecurityAlertSink,
    OutboxSecurityAlertSink,
    SecurityAlertPolicy,
    SecurityAlertSeverity,
    SecurityAlertType,
    SecurityAnomalyDetector,
)
from trading_bot.observability.security_audit import (
    OUTCOME_DENIED,
    OUTCOME_FAILURE,
    OUTCOME_SUCCESS,
    SECURITY_ACTION_AUTHZ_DENIED,
    SECURITY_ACTION_CSRF_FAILED,
    SECURITY_ACTION_LOGIN_FAILED,
    SECURITY_ACTION_LOGIN_SUCCEEDED,
    SECURITY_ACTION_REAUTH_FAILED,
    SECURITY_ACTION_REAUTH_SUCCEEDED,
    SECURITY_ACTION_RISK_PROFILE_CREATED,
    SECURITY_ACTION_SECRET_ROTATED,
    SecretRotationService,
    build_security_audit_event,
    hash_session_identifier,
    mask_client_ip,
    record_security_audit_event,
    sanitize_audit_reason,
    sanitize_user_agent,
    security_audit_scope,
)


def test_sec32_mask_ip_sanitize_ua_hash_session_and_redact_reason() -> None:
    assert mask_client_ip("203.0.113.42") == "203.0.113.0/24"
    assert mask_client_ip("203.0.113.42:54321") == "203.0.113.0/24"
    assert mask_client_ip("198.51.100.77, 10.0.0.1") == "198.51.100.0/24"
    assert mask_client_ip("127.0.0.1") == "127.0.0.0/24"
    assert mask_client_ip("2001:db8:85a3:1234::1") == "2001:db8:85a3::/48"
    assert mask_client_ip(None) is None
    assert mask_client_ip("   ") is None

    canary_token = "ghp_SUPERSECRETTOKENVALUE1234567890"
    ua = sanitize_user_agent(f"Mozilla/5.0\r\nAuthorization: Bearer {canary_token} " + ("x" * 250))
    assert ua is not None
    assert len(ua) <= 160
    assert "\r" not in ua
    assert "\n" not in ua
    assert canary_token not in ua

    raw_cookie = f"{uuid4()}.raw-session-secret-canary-999"
    hashed = hash_session_identifier(raw_cookie)
    assert hashed is not None
    assert len(hashed) == 64
    assert "raw-session-secret-canary-999" not in hashed
    assert hash_session_identifier(raw_cookie) == hashed
    assert hash_session_identifier(None) is None

    reason = sanitize_audit_reason(f"Lỗi xác thực Bearer {canary_token} password=my-secret-pass")
    assert reason is not None
    assert canary_token not in reason
    assert "my-secret-pass" not in reason


@pytest.mark.asyncio
async def test_record_security_audit_event_uses_scope_and_redacts_secrets() -> None:
    workspace_id = uuid4()
    actor_id = uuid4()
    request_id = uuid4()
    added: list[Any] = []

    session = MagicMock()
    session.info = {}
    session.begin = AsyncMock()
    session.close = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.add = MagicMock(side_effect=added.append)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    sessions = MagicMock(return_value=session)

    raw_cookie = "session-id.super-secret-cookie-canary"
    secret_canary = "SECRET_CANARY_AUDIT_987654321"

    with security_audit_scope(
        client_ip="203.0.113.88",
        user_agent="CrypidianTest/2.0",
        session_cookie=raw_cookie,
        request_id=request_id,
    ):
        async with UnitOfWork(
            sessions,
            workspace_id=workspace_id,
            actor_membership_id=actor_id,
            request_id=request_id,
        ) as uow:
            event = await record_security_audit_event(
                uow=uow,
                workspace_id=workspace_id,
                actor_membership_id=actor_id,
                action=SECURITY_ACTION_LOGIN_SUCCEEDED,
                target_type="session",
                outcome=OUTCOME_SUCCESS,
                reason=f"Đăng nhập thành công token={secret_canary}",
            )

    assert isinstance(event, AuditEvent)
    assert len(added) == 1
    assert event.client_ip == "203.0.113.0/24"
    assert event.user_agent == "CrypidianTest/2.0"
    assert event.session_hash == hash_session_identifier(raw_cookie)
    assert event.outcome == OUTCOME_SUCCESS
    assert event.request_id == request_id
    assert event.reason is not None
    assert secret_canary not in event.reason
    assert "super-secret-cookie-canary" not in (event.session_hash or "")

    built = build_security_audit_event(
        workspace_id=workspace_id,
        actor_membership_id=None,
        action=SECURITY_ACTION_LOGIN_FAILED,
        target_type="auth_login",
        outcome=OUTCOME_FAILURE,
        reason="Sai mật khẩu",
        client_ip="198.51.100.12",
    )
    assert built.client_ip == "198.51.100.0/24"
    assert built.outcome == OUTCOME_FAILURE


@pytest.mark.asyncio
async def test_secret_rotation_service_updates_reference_audits_and_alerts() -> None:
    workspace_id = uuid4()
    actor_id = uuid4()
    secret_ref = SecretReference(
        id=uuid4(),
        workspace_id=workspace_id,
        provider="local_encrypted",
        reference_key="binance/live/v1",
        status="ACTIVE",
    )
    added: list[Any] = []

    session = MagicMock()
    session.info = {}
    session.begin = AsyncMock()
    session.close = AsyncMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(return_value=secret_ref)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.add = MagicMock(side_effect=added.append)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    sessions = MagicMock(return_value=session)

    sink = InMemorySecurityAlertSink()
    detector = SecurityAnomalyDetector(sink=sink)
    principal = Principal(
        user_id=uuid4(),
        membership_id=actor_id,
        workspace_id=workspace_id,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )

    service = SecretRotationService(sessions=sessions, anomaly_detector=detector)
    result = await service.rotate_secret(
        principal,
        secret_reference_id=secret_ref.id,
        new_reference_key="binance/live/v2",
        reason="Xoay khóa định kỳ quý 4",
    )

    assert result.secret_reference_id == secret_ref.id
    assert result.previous_reference_key == "binance/live/v1"
    assert result.new_reference_key == "binance/live/v2"
    assert secret_ref.reference_key == "binance/live/v2"
    assert secret_ref.status == "ACTIVE"

    audit_events = [item for item in added if isinstance(item, AuditEvent)]
    assert len(audit_events) == 1
    audit = audit_events[0]
    assert audit.action == SECURITY_ACTION_SECRET_ROTATED
    assert audit.target_type == "secret_reference"
    assert audit.target_id == secret_ref.id
    assert audit.before_hash is not None
    assert audit.after_hash is not None
    assert audit.before_hash != audit.after_hash
    assert audit.outcome == OUTCOME_SUCCESS

    assert len(sink.alerts) == 1
    assert sink.alerts[0].alert_type is SecurityAlertType.PRIVILEGED_CHANGE


def test_secret_rotate_route_has_owner_csrf_policy() -> None:
    app = create_app()
    policies = {
        (record.path, record.methods): record.policy for record in audit_app_routes(app).audited
    }
    assert ("/api/v1/secrets/rotate", frozenset({"POST"})) in policies
    policy = policies[("/api/v1/secrets/rotate", frozenset({"POST"}))]
    assert policy is not None
    assert policy.access is RouteAccess.OWNER_ONLY
    assert policy.csrf is CsrfMode.HEADER


@pytest.mark.asyncio
async def test_security_anomaly_detector_auth_and_step_up_spikes() -> None:
    workspace_id = uuid4()
    actor_id = uuid4()
    now = [datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)]
    sink = InMemorySecurityAlertSink()
    detector = SecurityAnomalyDetector(
        policy=SecurityAlertPolicy(
            auth_failure_threshold=3,
            auth_failure_window=timedelta(minutes=5),
            step_up_failure_threshold=2,
            step_up_failure_window=timedelta(minutes=5),
            alert_cooldown=timedelta(0),
        ),
        sink=sink,
        clock=lambda: now[0],
    )

    # 2 lần đầu chưa vượt ngưỡng 3
    assert (
        await detector.observe_auth_failure(
            workspace_id=workspace_id,
            actor_key="owner@example.com",
            reason="Sai mật khẩu",
            client_ip="203.0.113.10",
        )
        is None
    )
    assert (
        await detector.observe_auth_failure(
            workspace_id=workspace_id,
            actor_key="owner@example.com",
            reason="Sai mật khẩu",
            client_ip="203.0.113.10",
        )
        is None
    )
    # Lần 3 vượt ngưỡng -> phát cảnh báo AUTH_FAILURE_SPIKE, không rò email thô
    spike = await detector.observe_auth_failure(
        workspace_id=workspace_id,
        actor_key="owner@example.com",
        reason="Sai mật khẩu",
        client_ip="203.0.113.10",
    )
    assert spike is not None
    assert spike.alert_type is SecurityAlertType.AUTH_FAILURE_SPIKE
    assert spike.severity is SecurityAlertSeverity.HIGH
    assert "owner@example.com" not in str(spike.metadata)

    # Trượt qua cửa sổ thời gian -> đếm lại từ 1
    now[0] += timedelta(minutes=6)
    assert (
        await detector.observe_auth_failure(
            workspace_id=workspace_id,
            actor_key="owner@example.com",
            reason="Sai mật khẩu",
            client_ip="203.0.113.10",
        )
        is None
    )

    # Step-up failure vượt ngưỡng và khóa
    assert (
        await detector.observe_step_up_failure(
            workspace_id=workspace_id,
            actor_membership_id=actor_id,
            action="secret.write",
            failure_count=1,
            locked=False,
            reason="Mã OTP không đúng",
        )
        is None
    )
    step_alert = await detector.observe_step_up_failure(
        workspace_id=workspace_id,
        actor_membership_id=actor_id,
        action="secret.write",
        failure_count=2,
        locked=True,
        reason="Quá số lần thử",
    )
    assert step_alert is not None
    assert step_alert.alert_type is SecurityAlertType.STEP_UP_FAILURE_SPIKE
    assert step_alert.severity is SecurityAlertSeverity.CRITICAL


@pytest.mark.asyncio
async def test_security_anomaly_detector_emergency_foreign_clock_exchange_and_outbox(
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace_id = uuid4()
    actor_id = uuid4()
    bot_id = uuid4()

    added_outbox: list[Any] = []
    session = MagicMock()
    session.info = {}
    session.begin = AsyncMock()
    session.close = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.add = MagicMock(side_effect=added_outbox.append)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    sessions = MagicMock(return_value=session)

    memory_sink = InMemorySecurityAlertSink()
    outbox_sink = OutboxSecurityAlertSink(sessions=sessions)
    log_sink = LoggingSecurityAlertSink()

    from trading_bot.observability.security_alerts import CompositeSecurityAlertSink

    detector = SecurityAnomalyDetector(
        sink=CompositeSecurityAlertSink((memory_sink, outbox_sink, log_sink)),
    )

    with caplog.at_level(logging.WARNING, logger="trading_bot.security.alerts"):
        kill_alert = await detector.observe_emergency_action(
            workspace_id=workspace_id,
            actor_membership_id=actor_id,
            action="workspace.kill",
            target_type="workspace",
            target_id=workspace_id,
            reason="Khẩn cấp dừng toàn bộ",
        )
        flatten_alert = await detector.observe_emergency_action(
            workspace_id=workspace_id,
            actor_membership_id=actor_id,
            action="workspace.flatten.confirmed",
            target_type="workspace",
            target_id=workspace_id,
            reason="Đóng khẩn vị thế",
        )
        resume_alert = await detector.observe_emergency_action(
            workspace_id=workspace_id,
            actor_membership_id=actor_id,
            action="bot.runtime.resumed",
            target_type="bot",
            target_id=bot_id,
            reason="Khôi phục bot",
        )
        risk_alert = await detector.observe_emergency_action(
            workspace_id=workspace_id,
            actor_membership_id=actor_id,
            action="risk_appetite.published",
            target_type="risk_profile",
            target_id=uuid4(),
            reason="Xuất bản cấu hình rủi ro",
        )
        foreign_alert = await detector.observe_foreign_activity(
            workspace_id=workspace_id,
            bot_id=bot_id,
            symbol="BTCUSDT",
            detail="Phát hiện lệnh ngoài bot trên tài khoản live",
        )
        assert await detector.observe_clock_skew(workspace_id=workspace_id, offset_ms=120.0) is None
        warn_skew = await detector.observe_clock_skew(workspace_id=workspace_id, offset_ms=750.0)
        crit_skew = await detector.observe_clock_skew(workspace_id=workspace_id, offset_ms=-1800.0)
        assert (
            await detector.observe_exchange_auth_or_ban(
                workspace_id=workspace_id, status_code=200, error_code=0
            )
            is None
        )
        ban_alert = await detector.observe_exchange_auth_or_ban(
            workspace_id=workspace_id,
            status_code=418,
            error_code=-2015,
            detail="Invalid API-key, IP, or permissions api_key=BINANCE_CANARY_KEY_999",
        )

    assert kill_alert.alert_type is SecurityAlertType.EMERGENCY_ACTION
    assert kill_alert.severity is SecurityAlertSeverity.CRITICAL
    assert flatten_alert.severity is SecurityAlertSeverity.CRITICAL
    assert resume_alert.severity is SecurityAlertSeverity.HIGH
    assert risk_alert.severity is SecurityAlertSeverity.HIGH
    assert foreign_alert.alert_type is SecurityAlertType.FOREIGN_ACTIVITY
    assert foreign_alert.severity is SecurityAlertSeverity.CRITICAL
    assert warn_skew is not None
    assert warn_skew.severity is SecurityAlertSeverity.WARNING
    assert crit_skew is not None
    assert crit_skew.severity is SecurityAlertSeverity.CRITICAL
    assert ban_alert is not None
    assert ban_alert.alert_type is SecurityAlertType.EXCHANGE_AUTH_OR_BAN
    assert ban_alert.severity is SecurityAlertSeverity.CRITICAL
    assert not contains_secret_canary(caplog.text, ("BINANCE_CANARY_KEY_999",))

    # Kiểm tra EventEnvelope và OutboxEvent đã được xếp hàng hợp lệ (không vi phạm sensitive keys)
    outbox_rows = [item for item in added_outbox if isinstance(item, OutboxEvent)]
    assert len(outbox_rows) == 8
    envelope = kill_alert.to_event_envelope()
    assert envelope.event_type == SecurityAlertType.EMERGENCY_ACTION.value
    assert envelope.workspace_id == workspace_id


def test_http_403_denial_and_csrf_failure_trigger_security_audit_and_detector() -> None:
    workspace_id = uuid4()
    membership_id = uuid4()
    identity = AuthIdentity(
        principal=Principal(
            user_id=uuid4(),
            membership_id=membership_id,
            workspace_id=workspace_id,
            roles=frozenset({Role.OPERATOR}),
            mfa_verified_at=datetime.now(UTC),
            session_id=uuid4(),
        ),
        email="operator@example.test",
    )

    class _StubAuthService:
        async def authenticate(self, cookie: str | None) -> AuthIdentity:
            assert cookie == "sid.token"
            return identity

        async def verify_csrf(self, cookie: str | None, csrf_token: str | None) -> None:
            from trading_bot.auth.service import AuthenticationFailure

            if csrf_token != "valid-csrf":
                raise AuthenticationFailure(403, "Token CSRF không hợp lệ")

    sink = InMemorySecurityAlertSink()
    app = create_app()
    app.state.auth_service = _StubAuthService()
    app.state.security_alert_sink = sink
    # Kiểm tra các hằng số hành động SEC-32 chuẩn hóa tồn tại đầy đủ
    assert SECURITY_ACTION_AUTHZ_DENIED == "auth.authorization.denied"
    assert SECURITY_ACTION_CSRF_FAILED == "auth.csrf.failed"
    assert SECURITY_ACTION_REAUTH_SUCCEEDED == "auth.reauth.succeeded"
    assert SECURITY_ACTION_REAUTH_FAILED == "auth.reauth.failed"
    assert SECURITY_ACTION_RISK_PROFILE_CREATED == "risk_profile.created"
    assert OUTCOME_DENIED == "denied"

    client = TestClient(app)
    # Gọi route yêu cầu quyền RISK_PROFILE_WRITE với role OPERATOR nhưng sai CSRF -> 403
    bad_csrf = client.post(
        "/api/v1/risk-profiles",
        cookies={"crypidian_session": "sid.token"},
        headers={"X-CSRF-Token": "wrong-csrf"},
        json={"name": "test", "max_risk_fraction": "0.02", "settings": {}},
    )
    assert bad_csrf.status_code == 403

    # Gọi route yêu cầu quyền RISK_PROFILE_WRITE với đúng CSRF nhưng thiếu quyền -> 403
    denied = client.post(
        "/api/v1/risk-profiles",
        cookies={"crypidian_session": "sid.token"},
        headers={"X-CSRF-Token": "valid-csrf"},
        json={"name": "test", "max_risk_fraction": "0.02", "settings": {}},
    )
    assert denied.status_code == 403
