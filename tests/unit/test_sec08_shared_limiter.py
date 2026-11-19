"""TDD unit tests cho SEC-08/shared-limiter (CRY-286): chia sẻ replica, lockout bền, backoff."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import (
    THROTTLED_DETAIL,
    get_auth_service,
    require_csrf,
)
from trading_bot.auth.security import LoginRateLimiter, PasswordManager
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity
from trading_bot.auth.shared_limiter import (
    InMemoryRateLimitStore,
    SharedAuthRateLimiter,
    compute_progressive_delay_seconds,
    hash_rate_limit_key,
)
from trading_bot.config.settings import AppSettings

HASH_KEY_RE = re.compile(r"^(ip|email|pair|account|familiar|step_up|telegram_link):[0-9a-f]{64}$")


def test_shared_limiter_replicates_counts_and_survives_restart_with_shared_store() -> None:
    """Hai replica cùng trỏ 1 store chia sẻ bộ đếm và giữ lockout sau khi tạo instance mới."""
    store = InMemoryRateLimitStore()
    replica_a = SharedAuthRateLimiter(store=store, max_attempts=5, window_seconds=300)
    replica_b = SharedAuthRateLimiter(store=store, max_attempts=5, window_seconds=300)

    for step in range(3):
        assert replica_a.allow("198.51.100.10", "Owner@Example.test", now=step)
        replica_a.record_failure("198.51.100.10", "owner@example.test", now=step)

    for step in range(3, 5):
        assert replica_b.allow("198.51.100.10", "owner@example.test", now=step)
        replica_b.record_failure("198.51.100.10", "owner@example.test", now=step)

    decision_a = replica_a.check("198.51.100.10", "owner@example.test", now=6)
    decision_b = replica_b.check("198.51.100.10", "owner@example.test", now=6)
    assert not decision_a.allowed
    assert not decision_b.allowed
    assert decision_a.retry_after_seconds is not None
    assert decision_a.retry_after_seconds >= 290

    # Mô phỏng restart process bằng instance hoàn toàn mới đọc cùng store.
    restarted = SharedAuthRateLimiter(store=store, max_attempts=5, window_seconds=300)
    after_restart = restarted.check("198.51.100.10", "owner@example.test", now=10)
    assert not after_restart.allowed
    assert after_restart.retry_after_seconds is not None
    assert after_restart.retry_after_seconds >= 290
    assert restarted.allow("198.51.100.10", "owner@example.test", now=4 + 301)


def test_progressive_delay_starts_at_fifth_failure_and_locks_out_at_threshold() -> None:
    """Từ lỗi thứ 5 tăng delay lũy thừa (1s, 2s, 4s, 8s, 16s), chạm ngưỡng thì lockout."""
    assert compute_progressive_delay_seconds(4) == 0.0
    assert compute_progressive_delay_seconds(5) == 1.0
    assert compute_progressive_delay_seconds(6) == 2.0
    assert compute_progressive_delay_seconds(7) == 4.0
    assert compute_progressive_delay_seconds(8) == 8.0
    assert compute_progressive_delay_seconds(9) == 16.0
    assert compute_progressive_delay_seconds(12, max_seconds=30.0) == 30.0

    store = InMemoryRateLimitStore()
    limiter = SharedAuthRateLimiter(
        store=store,
        max_attempts=10,
        window_seconds=900,
        account_max_attempts=10,
        account_window_seconds=900,
        email_max_attempts=20,
        ip_max_attempts=50,
        progressive_delay_start=5,
        progressive_delay_base_seconds=1.0,
        progressive_delay_max_seconds=30.0,
    )

    now = 100.0
    # 4 lỗi đầu từ nhiều IP khác nhau chưa bị trễ.
    for idx in range(4):
        assert limiter.allow(
            f"10.1.0.{idx}", "trader@example.test", account_scope="slug:w1", now=now
        )
        d = limiter.record_failure(
            f"10.1.0.{idx}", "trader@example.test", account_scope="slug:w1", now=now
        )
        assert d.delay_seconds == 0.0
        now += 0.1

    # Lần lỗi thứ 5 kích hoạt delay 1s.
    d5 = limiter.record_failure("10.1.0.5", "trader@example.test", account_scope="slug:w1", now=now)
    assert d5.delay_seconds == 1.0
    assert not limiter.allow(
        "10.1.0.99", "trader@example.test", account_scope="slug:w1", now=now + 0.5
    )
    assert limiter.allow(
        "10.1.0.99", "trader@example.test", account_scope="slug:w1", now=now + 1.05
    )

    # Lần lỗi thứ 6 kích hoạt delay 2s.
    now += 1.05
    d6 = limiter.record_failure("10.1.0.6", "trader@example.test", account_scope="slug:w1", now=now)
    assert d6.delay_seconds == 2.0
    assert not limiter.allow(
        "10.1.0.99", "trader@example.test", account_scope="slug:w1", now=now + 1.5
    )
    assert limiter.allow(
        "10.1.0.99", "trader@example.test", account_scope="slug:w1", now=now + 2.05
    )

    # Lần 7 (4s), lần 8 (8s), lần 9 (16s), lần 10 lockout 900s.
    for expected_delay, idx in ((4.0, 7), (8.0, 8), (16.0, 9)):
        now += expected_delay / 2 + 0.1 if idx == 7 else (2.05 if idx == 8 else 8.05)
        d = limiter.record_failure(
            f"10.1.0.{idx}", "trader@example.test", account_scope="slug:w1", now=now
        )
        assert round(d.delay_seconds, 6) == expected_delay
        now += expected_delay + 0.05

    d10 = limiter.record_failure(
        "10.1.0.10", "trader@example.test", account_scope="slug:w1", now=now
    )
    assert not d10.allowed
    assert d10.retry_after_seconds is not None
    assert d10.retry_after_seconds >= 899
    assert not limiter.allow(
        "10.1.0.99", "trader@example.test", account_scope="slug:w1", now=now + 100
    )
    assert limiter.allow("10.1.0.99", "trader@example.test", account_scope="slug:w1", now=now + 901)


def test_keys_are_sha256_hashed_and_never_store_plaintext_pii() -> None:
    """Không lưu email, IP hay slug workspace dạng plaintext trong key hoặc store."""
    store = InMemoryRateLimitStore()
    limiter = SharedAuthRateLimiter(store=store)
    secret_email = "Sensitive.Trader+VIP@Example.Test"
    secret_ip = "203.0.113.42"
    secret_slug = "slug:alpha-desk"

    limiter.record_failure(
        secret_ip, secret_email, account_scope=secret_slug, factor="totp", now=10
    )
    limiter.record_success(
        secret_ip, secret_email, account_scope=secret_slug, factor="totp", now=20
    )
    limiter.record_failure(
        secret_ip, secret_email, account_scope=secret_slug, factor="totp", now=30
    )

    raw_keys = store.all_keys()
    assert len(raw_keys) >= 4
    for key in raw_keys:
        assert HASH_KEY_RE.fullmatch(key), key
        lowered = key.lower()
        assert "sensitive" not in lowered
        assert "example.test" not in lowered
        assert "203.0.113.42" not in lowered
        assert "alpha-desk" not in lowered
        assert "@" not in key

    assert hash_rate_limit_key("email", "a@b.test") != hash_rate_limit_key("pair", "a@b.test")


def test_account_plus_factor_counters_are_isolated_per_factor() -> None:
    """Bộ đếm account+factor tách riêng giữa TOTP và recovery code mà vẫn chia sẻ IP/pair."""
    store = InMemoryRateLimitStore()
    limiter = SharedAuthRateLimiter(
        store=store,
        max_attempts=50,
        account_max_attempts=3,
        email_max_attempts=50,
        ip_max_attempts=50,
    )

    for idx in range(3):
        limiter.record_failure(
            f"10.2.0.{idx}",
            "user@example.test",
            account_scope="slug:w1",
            factor="totp",
            now=idx,
        )

    assert not limiter.allow(
        "10.2.0.99", "user@example.test", account_scope="slug:w1", factor="totp", now=10
    )
    assert limiter.allow(
        "10.2.0.99", "user@example.test", account_scope="slug:w1", factor="recovery", now=10
    )


def test_email_hash_counter_throttles_distributed_attempts_and_resets_on_success() -> None:
    """Email hash đếm lỗi liên tiếp xuyên scope/IP; thành công xóa bộ đếm lỗi liên tiếp."""
    store = InMemoryRateLimitStore()
    limiter = SharedAuthRateLimiter(
        store=store,
        max_attempts=50,
        account_max_attempts=50,
        email_max_attempts=5,
        email_window_seconds=600,
        ip_max_attempts=50,
    )

    for idx in range(5):
        limiter.record_failure(
            f"10.3.0.{idx}",
            "Target@Example.test",
            account_scope=f"slug:ws-{idx}",
            now=idx,
        )

    decision = limiter.check("10.3.0.99", "target@example.test", account_scope="slug:ws-99", now=10)
    assert not decision.allowed
    assert decision.retry_after_seconds is not None
    assert decision.retry_after_seconds >= 590

    limiter.record_success("10.3.0.99", "target@example.test", account_scope="slug:ws-99", now=11)
    assert limiter.allow("10.3.0.88", "target@example.test", account_scope="slug:ws-88", now=12)


@dataclass
class _CapturedStatement:
    sql: str
    params: dict[str, Any]


class _FakeAsyncResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def first(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self) -> Any:
        return self._rows[0][0] if self._rows else None


class _RecordingAsyncSession:
    def __init__(self, statements: list[_CapturedStatement], now_dt: datetime) -> None:
        self._statements = statements
        self._now_dt = now_dt

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        yield

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _FakeAsyncResult:
        compiled = stmt.compile(dialect=postgresql.dialect())
        self._statements.append(_CapturedStatement(sql=str(compiled), params=dict(compiled.params)))
        sql_text = str(compiled)
        if sql_text.lstrip().upper().startswith("INSERT"):
            scope_key = str(compiled.params["scope_key"])
            return _FakeAsyncResult([(scope_key, 1, self._now_dt, None, self._now_dt)])
        return _FakeAsyncResult([])


class _RecordingAsyncSessionFactory:
    def __init__(self, now_dt: datetime) -> None:
        self.statements: list[_CapturedStatement] = []
        self._now_dt = now_dt

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[_RecordingAsyncSession]:
        yield _RecordingAsyncSession(self.statements, self._now_dt)


async def test_postgres_store_emits_atomic_insert_on_conflict_without_plaintext_pii() -> None:
    """Khi có session_factory Postgres, phải dùng INSERT ... ON CONFLICT DO UPDATE với key băm."""
    now_dt = datetime(2026, 11, 19, 8, 0, tzinfo=UTC)
    sessions = _RecordingAsyncSessionFactory(now_dt)
    limiter = SharedAuthRateLimiter(session_factory=sessions)  # type: ignore[arg-type]

    decision = await limiter.record_failure_async(
        "203.0.113.5",
        "Private.User@Example.test",
        account_scope="slug:secret-ws",
        factor="totp",
        now=now_dt,
    )
    assert decision.allowed
    assert len(sessions.statements) == 4
    for captured in sessions.statements:
        normalized_sql = " ".join(captured.sql.split()).upper()
        assert "INSERT INTO AUTH_RATE_LIMIT_STATE" in normalized_sql
        assert "ON CONFLICT (SCOPE_KEY) DO UPDATE" in normalized_sql
        scope_key = str(captured.params["scope_key"])
        assert HASH_KEY_RE.fullmatch(scope_key)
        assert "private.user" not in str(captured.params).lower()
        assert "203.0.113.5" not in str(captured.params)
        assert "secret-ws" not in str(captured.params)


def test_http_routes_return_retry_after_and_identical_429_for_unknown_and_known_accounts() -> None:
    """HTTP route dùng SharedAuthRateLimiter, trả Retry-After và không lộ tồn tại tài khoản."""
    shared_store = InMemoryRateLimitStore()

    @dataclass
    class RejectingAuthService:
        calls: int = 0

        async def login(self, **_: object) -> object:
            self.calls += 1
            raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")

    def build_replica(service: RejectingAuthService) -> TestClient:
        app = create_app(AppSettings(app_env="test"))
        app.state.login_limiter = SharedAuthRateLimiter(store=shared_store)
        app.dependency_overrides[get_auth_service] = lambda: service
        return TestClient(app)

    service_a = RejectingAuthService()
    service_b = RejectingAuthService()
    replica_a = build_replica(service_a)
    replica_b = build_replica(service_b)

    known_payload = {
        "workspace_slug": "paper-workspace",
        "email": "owner@example.test",
        "password": "wrong password value",
        "code": "000000",
    }
    for _ in range(3):
        assert replica_a.post("/api/v1/auth/login", json=known_payload).status_code == 401
    for _ in range(2):
        assert replica_b.post("/api/v1/auth/login", json=known_payload).status_code == 401

    blocked = replica_a.post("/api/v1/auth/login", json=known_payload)
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": THROTTLED_DETAIL}
    assert int(blocked.headers["Retry-After"]) >= 1
    assert service_a.calls == 3
    assert service_b.calls == 2

    # Tài khoản không tồn tại trả cấu trúc y hệt.
    unknown_store = InMemoryRateLimitStore()
    unknown_app = create_app(AppSettings(app_env="test"))
    unknown_app.state.login_limiter = SharedAuthRateLimiter(store=unknown_store)
    unknown_app.dependency_overrides[get_auth_service] = lambda: RejectingAuthService()
    unknown_client = TestClient(unknown_app)
    unknown_payload = {**known_payload, "email": "nobody@example.test"}
    for _ in range(5):
        assert unknown_client.post("/api/v1/auth/login", json=unknown_payload).status_code == 401
    unknown_blocked = unknown_client.post("/api/v1/auth/login", json=unknown_payload)
    assert unknown_blocked.status_code == 429
    assert unknown_blocked.json() == blocked.json()
    assert int(unknown_blocked.headers["Retry-After"]) >= 1


def test_create_app_binds_bound_session_factory_and_leaves_unbound_in_memory() -> None:
    bound_engine = create_async_engine("postgresql+psycopg://u:p@localhost/crypidian")
    bound_sessions = async_sessionmaker(bound_engine, expire_on_commit=False)
    app_bound = create_app(AppSettings(app_env="test"), session_factory=bound_sessions)
    assert isinstance(app_bound.state.login_limiter, LoginRateLimiter)
    assert app_bound.state.login_limiter.session_factory is bound_sessions

    unbound_app = create_app(AppSettings(app_env="test"), session_factory=async_sessionmaker())
    assert unbound_app.state.login_limiter.session_factory is None

    # Reauth với route vẫn hoạt động bình thường qua TestClient
    principal = Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=uuid4(),
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    class RejectingReauth:
        async def reauthenticate(self, *_: object, **__: object) -> None:
            raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")

    unbound_app.dependency_overrides[get_auth_service] = lambda: RejectingReauth()
    unbound_app.dependency_overrides[require_csrf] = lambda: AuthIdentity(
        principal=principal, email="owner@example.test"
    )
    client = TestClient(unbound_app)
    for _ in range(5):
        assert (
            client.post(
                "/api/v1/auth/reauthenticate",
                json={"password": "wrong password value", "code": "000000"},
            ).status_code
            == 401
        )
    r6 = client.post(
        "/api/v1/auth/reauthenticate",
        json={"password": "wrong password value", "code": "000000"},
    )
    assert r6.status_code == 429
    assert int(r6.headers["Retry-After"]) >= 1


def test_password_manager_caps_concurrent_argon2_operations() -> None:
    """SEC-08: giới hạn số phép băm/xác minh Argon2id chạy đồng thời để tránh cạn RAM."""
    manager = PasswordManager(max_concurrency=2)
    active = 0
    max_seen = 0
    lock = threading.Lock()

    class _CountingHasher:
        def verify(self, encoded_hash: str, password: str) -> bool:
            nonlocal active, max_seen
            del encoded_hash, password
            with lock:
                active += 1
                max_seen = max(max_seen, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return True

    manager._hasher = _CountingHasher()  # type: ignore[assignment]
    threads = [threading.Thread(target=manager.verify, args=("hash", "password")) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert max_seen <= 2
