from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from trading_bot.auth.policy import Permission, Principal, Role, authorize
from trading_bot.auth.security import (
    LoginRateLimiter,
    PasswordManager,
    TotpManager,
    verify_bootstrap_token,
)


def test_password_manager_hashes_and_verifies_without_reusing_hash() -> None:
    manager = PasswordManager()

    first_hash = manager.hash("correct horse battery staple")
    second_hash = manager.hash("correct horse battery staple")

    assert first_hash != second_hash
    assert manager.verify(first_hash, "correct horse battery staple")
    assert not manager.verify(first_hash, "wrong password")


def test_totp_manager_verifies_current_code_and_rejects_invalid_code() -> None:
    manager = TotpManager()
    secret = manager.generate_secret()
    now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

    assert manager.verify(secret, manager.code_at(secret, now), now)
    assert not manager.verify(secret, "000000", now)


def test_totp_manager_returns_the_matching_counter_with_a_bounded_window() -> None:
    manager = TotpManager()
    secret = manager.generate_secret()
    now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    previous_code = manager.code_at(secret, now.replace(second=0) - timedelta(seconds=30))

    assert manager.matching_counter(secret, previous_code, now, valid_window=1) == (
        int(now.timestamp()) // 30 - 1
    )
    assert manager.matching_counter(secret, "000000", now, valid_window=1) is None


def test_login_limiter_blocks_after_configured_failures_and_resets() -> None:
    limiter = LoginRateLimiter(max_attempts=2, window_seconds=60)

    assert limiter.allow("127.0.0.1", "owner@example.test", now=0)
    limiter.record_failure("127.0.0.1", "owner@example.test", now=0)
    limiter.record_failure("127.0.0.1", "owner@example.test", now=1)
    assert not limiter.allow("127.0.0.1", "owner@example.test", now=2)

    limiter.record_success("127.0.0.1", "owner@example.test")
    assert limiter.allow("127.0.0.1", "owner@example.test", now=2)


def test_role_policy_checks_permissions_on_server() -> None:
    workspace_id = uuid4()
    viewer = Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=workspace_id,
        roles=frozenset({Role.ANALYST_REVIEWER}),
        mfa_verified_at=datetime.now(UTC),
    )
    owner = Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=workspace_id,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )

    assert not authorize(viewer, Permission.RISK_PROFILE_WRITE)
    assert authorize(owner, Permission.RISK_PROFILE_WRITE)
    with pytest.raises(PermissionError):
        authorize(viewer, Permission.RISK_PROFILE_WRITE, raise_error=True)


def test_bootstrap_token_is_checked_in_constant_time_and_missing_token_fails(tmp_path) -> None:
    token_file = tmp_path / "bootstrap_token"
    token_file.write_text("one-time-owner-setup-token", encoding="utf-8")

    assert verify_bootstrap_token(token_file, "one-time-owner-setup-token")
    assert not verify_bootstrap_token(token_file, "wrong-token")
    assert not verify_bootstrap_token(tmp_path / "missing", "one-time-owner-setup-token")
