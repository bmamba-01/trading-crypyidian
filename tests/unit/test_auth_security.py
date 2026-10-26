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


def test_login_limiter_throttles_one_account_across_many_ips() -> None:
    """SEC-08: credential stuffing phân tán nhiều IP vẫn bị chặn theo tài khoản."""
    limiter = LoginRateLimiter(
        max_attempts=5, window_seconds=300, account_max_attempts=3, account_window_seconds=900
    )

    for index in range(3):
        assert limiter.allow(f"10.0.0.{index}", "Owner@Example.test ", now=index)
        limiter.record_failure(f"10.0.0.{index}", "owner@example.test", now=index)

    assert not limiter.allow("10.0.0.99", "owner@example.test", now=10)
    assert limiter.allow("10.0.0.99", "other@example.test", now=10)
    assert limiter.allow("10.0.0.99", "owner@example.test", now=3 + 900)


def test_login_limiter_throttles_password_spraying_from_one_ip() -> None:
    """SECT-06: 20 email khác nhau từ một IP → lần thứ 21 bị chặn theo IP."""
    limiter = LoginRateLimiter()

    for index in range(20):
        email = f"user{index}@example.test"
        assert limiter.allow("203.0.113.7", email, now=index)
        limiter.record_failure("203.0.113.7", email, now=index)

    assert not limiter.allow("203.0.113.7", "fresh@example.test", now=21)
    assert limiter.allow("198.51.100.1", "fresh@example.test", now=21)
    assert limiter.allow("203.0.113.7", "fresh@example.test", now=19 + 900)


def test_login_limiter_success_resets_account_but_not_ip_counter() -> None:
    limiter = LoginRateLimiter(
        max_attempts=5,
        window_seconds=300,
        account_max_attempts=2,
        account_window_seconds=900,
        ip_max_attempts=3,
        ip_window_seconds=900,
    )
    limiter.record_failure("10.0.0.1", "owner@example.test", now=0)
    limiter.record_failure("10.0.0.2", "owner@example.test", now=0)
    assert not limiter.allow("10.0.0.3", "owner@example.test", now=1)

    limiter.record_success("10.0.0.3", "owner@example.test")
    assert limiter.allow("10.0.0.3", "owner@example.test", now=1)

    limiter.record_failure("10.0.0.3", "a@example.test", now=1)
    limiter.record_failure("10.0.0.3", "b@example.test", now=1)
    limiter.record_failure("10.0.0.3", "c@example.test", now=1)
    limiter.record_success("10.0.0.3", "owner@example.test")
    assert not limiter.allow("10.0.0.3", "owner@example.test", now=2)


def test_login_limiter_behaves_identically_for_unknown_and_known_accounts() -> None:
    """Khóa dựa trên email người gửi nhập nên không lộ tài khoản có tồn tại hay không."""
    known = LoginRateLimiter(max_attempts=2, window_seconds=60)
    unknown = LoginRateLimiter(max_attempts=2, window_seconds=60)
    results: list[tuple[bool, bool]] = []
    for step in range(3):
        results.append(
            (
                known.allow("10.0.0.1", "owner@example.test", now=step),
                unknown.allow("10.0.0.1", "nobody@example.test", now=step),
            )
        )
        known.record_failure("10.0.0.1", "owner@example.test", now=step)
        unknown.record_failure("10.0.0.1", "nobody@example.test", now=step)

    assert results == [(True, True), (True, True), (False, False)]


def test_login_limiter_bounds_tracked_keys() -> None:
    limiter = LoginRateLimiter(max_attempts=1, max_tracked_keys=10, ip_max_attempts=1000)

    for index in range(50):
        limiter.record_failure("10.0.0.1", f"user{index}@example.test", now=index)

    assert limiter.tracked_key_count() <= 3 * 10
    # Khóa mới nhất vẫn được giữ sau khi bỏ khóa cũ nhất.
    assert not limiter.allow("10.0.0.1", "user49@example.test", now=50)


def test_login_limiter_rejects_invalid_thresholds() -> None:
    with pytest.raises(ValueError, match="lớn hơn 0"):
        LoginRateLimiter(account_max_attempts=0)
    with pytest.raises(ValueError, match="lớn hơn 0"):
        LoginRateLimiter(ip_window_seconds=0)


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
