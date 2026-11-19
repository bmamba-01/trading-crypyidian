"""Bộ giới hạn xác thực và lockout bền vững, chia sẻ giữa các replica (SEC-08).

Hỗ trợ hai chế độ hoạt động trên cùng một giao diện:
- **PostgreSQL (`session_factory` / `engine`)**: lưu trạng thái vào bảng ``auth_rate_limit_state``
  bằng câu lệnh nguyên tử ``INSERT ... ON CONFLICT (scope_key) DO UPDATE ...`` giúp nhiều
  replica dùng chung bộ đếm và giữ nguyên lockout/backoff khi process restart.
- **In-memory (`store` hoặc mặc định khi chưa có DB)**: giữ tương thích hoàn toàn cho unit test
  và cho phép nhiều instance chia sẻ một ``InMemoryRateLimitStore``.

Mọi khóa lưu trữ đều là chuỗi băm SHA-256 phân tách miền theo định dạng ``<scope_type>:<hex>``,
không lưu IP, email hay slug workspace dạng rõ và không tiết lộ tài khoản có tồn tại hay không.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import deque
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from time import monotonic
from typing import Any, TypeVar, cast

from sqlalchemy import DateTime, Engine, Float, and_, case, delete, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from trading_bot.adapters.database.models.entities import AuthRateLimitState

_T = TypeVar("_T")

KEY_HASH_VERSION = "crypidian.auth.rate_limit.v1"
VALID_SCOPE_TYPES = frozenset(
    {"ip", "email", "pair", "account", "familiar", "step_up", "telegram_link"}
)


def hash_rate_limit_key(scope_type: str, *parts: str) -> str:
    """Tạo khóa băm SHA-256 ổn định, không chứa PII hay thông tin tồn tại tài khoản."""
    if scope_type not in VALID_SCOPE_TYPES:
        raise ValueError(f"scope_type không hợp lệ: {scope_type!r}")
    serialized = json.dumps(
        [KEY_HASH_VERSION, scope_type, *parts],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"{scope_type}:{digest}"


def compute_progressive_delay_seconds(
    failures: int,
    *,
    start_at: int = 5,
    base_seconds: float = 1.0,
    max_seconds: float = 30.0,
) -> float:
    """Tính delay tăng dần theo cấp số nhân từ lần lỗi thứ ``start_at`` trở đi."""
    if failures < start_at or base_seconds <= 0 or max_seconds <= 0:
        return 0.0
    exponent = min(failures - start_at, 30)
    return float(min(max_seconds, base_seconds * (2**exponent)))


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Kết quả kiểm tra hoặc ghi nhận lỗi từ limiter (không chứa PII)."""

    allowed: bool
    retry_after_seconds: int | None = None
    delay_seconds: float = 0.0
    failures: int = 0
    locked_until: datetime | None = None
    blocking_scope: str | None = None


@dataclass(frozen=True, slots=True)
class BucketPolicySpec:
    """Chính sách đếm lỗi, delay tăng dần và lockout cho một phạm vi khóa."""

    scope_key: str
    scope_type: str
    max_attempts: int
    window_seconds: int
    lockout_seconds: int
    progressive_delay_start: int | None
    progressive_delay_base_seconds: float
    progressive_delay_max_seconds: float


@dataclass(slots=True)
class _InMemoryBucket:
    scope_type: str
    timestamps: deque[float]
    locked_until: float | None = None


class InMemoryRateLimitStore:
    """Kho lưu trạng thái throttle trong RAM, an toàn luồng và có giới hạn số khóa."""

    def __init__(self, *, max_keys: int = 10_000) -> None:
        if max_keys < 1:
            raise ValueError("Ngưỡng số khóa phải lớn hơn 0")
        self.max_keys = max_keys
        self._buckets: dict[str, _InMemoryBucket] = {}
        self._familiar: dict[str, float] = {}
        self._lock = Lock()

    def check(
        self,
        specs: Sequence[BucketPolicySpec],
        *,
        familiar_key: str,
        familiar_seconds: int,
        skip_on_familiar: frozenset[str],
        now: float,
    ) -> RateLimitDecision:
        with self._lock:
            familiar = self._is_familiar_locked(familiar_key, familiar_seconds, now)
            max_remaining = 0.0
            max_locked_until: float | None = None
            blocking_scope: str | None = None
            blocking_failures = 0
            active_failures = 0

            for spec in specs:
                if familiar and spec.scope_type in skip_on_familiar:
                    continue
                bucket = self._buckets.get(spec.scope_key)
                if bucket is None:
                    continue
                self._refresh_bucket_locked(bucket, spec, now)
                if not bucket.timestamps and bucket.locked_until is None:
                    self._buckets.pop(spec.scope_key, None)
                    continue

                failures = len(bucket.timestamps)
                active_failures = max(active_failures, failures)
                remaining = 0.0
                locked_until = bucket.locked_until
                if locked_until is not None and locked_until > now:
                    remaining = locked_until - now

                if remaining > max_remaining:
                    max_remaining = remaining
                    max_locked_until = locked_until
                    blocking_scope = spec.scope_type
                    blocking_failures = failures

            if max_remaining > 0.0:
                return _blocked_decision(
                    remaining=max_remaining,
                    failures=blocking_failures,
                    locked_until_ts=max_locked_until,
                    blocking_scope=blocking_scope,
                )
            return RateLimitDecision(allowed=True, failures=active_failures)

    def record_failure(
        self,
        specs: Sequence[BucketPolicySpec],
        *,
        now: float,
    ) -> RateLimitDecision:
        with self._lock:
            max_remaining = 0.0
            max_locked_until: float | None = None
            blocking_scope: str | None = None
            blocking_failures = 0
            max_failures = 0

            for spec in specs:
                bucket = self._buckets.get(spec.scope_key)
                if bucket is None:
                    self._evict_buckets_locked(spec.scope_type, spec.window_seconds, now)
                    bucket = self._buckets[spec.scope_key] = _InMemoryBucket(
                        scope_type=spec.scope_type,
                        timestamps=deque(),
                    )
                else:
                    self._refresh_bucket_locked(bucket, spec, now)

                bucket.timestamps.append(now)
                count = len(bucket.timestamps)
                max_failures = max(max_failures, count)

                candidate_lock = _candidate_lock_timestamp(spec, count, now)
                if candidate_lock is not None:
                    if bucket.locked_until is None or candidate_lock > bucket.locked_until:
                        bucket.locked_until = candidate_lock

                if bucket.locked_until is not None and bucket.locked_until > now:
                    remaining = bucket.locked_until - now
                    if remaining > max_remaining:
                        max_remaining = remaining
                        max_locked_until = bucket.locked_until
                        blocking_scope = spec.scope_type
                        blocking_failures = count

            if max_remaining > 0.0:
                return _blocked_decision(
                    remaining=max_remaining,
                    failures=blocking_failures,
                    locked_until_ts=max_locked_until,
                    blocking_scope=blocking_scope,
                )
            return RateLimitDecision(allowed=True, failures=max_failures)

    def record_success(
        self,
        *,
        reset_keys: Sequence[str],
        familiar_key: str,
        familiar_seconds: int,
        now: float,
    ) -> None:
        with self._lock:
            for key in reset_keys:
                self._buckets.pop(key, None)
            self._familiar.pop(familiar_key, None)
            if len(self._familiar) >= self.max_keys:
                self._evict_familiar_locked(familiar_seconds, now)
            self._familiar[familiar_key] = now

    def failure_count(
        self,
        scope_key: str,
        *,
        window_seconds: int,
        now: float | None = None,
    ) -> int:
        with self._lock:
            bucket = self._buckets.get(scope_key)
            if bucket is None:
                return 0
            if now is not None:
                while bucket.timestamps and now - bucket.timestamps[0] >= window_seconds:
                    bucket.timestamps.popleft()
            return len(bucket.timestamps)

    def tracked_key_count(self) -> int:
        with self._lock:
            return len(self._buckets)

    def all_keys(self) -> list[str]:
        with self._lock:
            return [*self._buckets.keys(), *self._familiar.keys()]

    def _is_familiar_locked(self, familiar_key: str, familiar_seconds: int, now: float) -> bool:
        last_success = self._familiar.get(familiar_key)
        if last_success is None:
            return False
        if now - last_success >= familiar_seconds:
            del self._familiar[familiar_key]
            return False
        return True

    def _refresh_bucket_locked(
        self, bucket: _InMemoryBucket, spec: BucketPolicySpec, now: float
    ) -> None:
        if bucket.locked_until is not None and bucket.locked_until <= now:
            if len(bucket.timestamps) >= spec.max_attempts:
                bucket.timestamps.clear()
            bucket.locked_until = None
        while bucket.timestamps and now - bucket.timestamps[0] >= spec.window_seconds:
            bucket.timestamps.popleft()

    def _evict_familiar_locked(self, familiar_seconds: int, now: float) -> None:
        for key, last_success in list(self._familiar.items()):
            if now - last_success >= familiar_seconds:
                del self._familiar[key]
        while len(self._familiar) >= self.max_keys:
            del self._familiar[next(iter(self._familiar))]

    def _evict_buckets_locked(self, target_scope: str, window_seconds: int, now: float) -> None:
        """Giữ tối đa `max_keys` khóa mỗi loại và tổng khóa <= `3 * max_keys`."""
        same_scope = [k for k, b in self._buckets.items() if b.scope_type == target_scope]
        if len(same_scope) >= self.max_keys:
            for key in same_scope:
                bucket = self._buckets[key]
                while bucket.timestamps and now - bucket.timestamps[0] >= window_seconds:
                    bucket.timestamps.popleft()
                if not bucket.timestamps and (
                    bucket.locked_until is None or bucket.locked_until <= now
                ):
                    del self._buckets[key]
            same_scope = [k for k, b in self._buckets.items() if b.scope_type == target_scope]
            while len(same_scope) >= self.max_keys:
                self._buckets.pop(same_scope.pop(0), None)

        total_cap = 3 * self.max_keys
        if len(self._buckets) < total_cap:
            return
        for key in list(self._buckets):
            bucket = self._buckets[key]
            if not bucket.timestamps and (
                bucket.locked_until is None or bucket.locked_until <= now
            ):
                del self._buckets[key]
        eviction_order = ("email", "pair", "account", "ip", "step_up", "telegram_link")
        for scope in eviction_order:
            if len(self._buckets) < total_cap:
                break
            for key in [k for k, b in self._buckets.items() if b.scope_type == scope]:
                if len(self._buckets) < total_cap:
                    break
                self._buckets.pop(key, None)


class PostgresRateLimitStore:
    """Kho lưu trạng thái throttle trên PostgreSQL bằng ``INSERT ... ON CONFLICT DO UPDATE``."""

    def __init__(
        self,
        *,
        async_sessions: async_sessionmaker[AsyncSession] | None = None,
        sync_sessions: sessionmaker[Session] | None = None,
    ) -> None:
        if (async_sessions is None) == (sync_sessions is None):
            raise ValueError("Cần cung cấp đúng một trong async_sessions hoặc sync_sessions")
        self._async_sessions = async_sessions
        self._sync_sessions = sync_sessions

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession] | sessionmaker[Session] | None:
        return self._async_sessions if self._async_sessions is not None else self._sync_sessions

    async def check_async(
        self,
        specs: Sequence[BucketPolicySpec],
        *,
        familiar_key: str,
        familiar_seconds: int,
        skip_on_familiar: frozenset[str],
        now: datetime | None,
    ) -> RateLimitDecision:
        if self._async_sessions is None:
            assert self._sync_sessions is not None
            return await asyncio.to_thread(
                self.check_sync,
                specs,
                familiar_key=familiar_key,
                familiar_seconds=familiar_seconds,
                skip_on_familiar=skip_on_familiar,
                now=now,
            )
        keys = [familiar_key, *(spec.scope_key for spec in specs)]
        stmt = _build_check_select(keys, now)
        async with self._async_sessions() as session:
            rows = (await session.execute(stmt)).all()
        return _evaluate_db_check_rows(
            rows,
            specs=specs,
            familiar_key=familiar_key,
            familiar_seconds=familiar_seconds,
            skip_on_familiar=skip_on_familiar,
            explicit_now=now,
        )

    def check_sync(
        self,
        specs: Sequence[BucketPolicySpec],
        *,
        familiar_key: str,
        familiar_seconds: int,
        skip_on_familiar: frozenset[str],
        now: datetime | None,
    ) -> RateLimitDecision:
        if self._sync_sessions is None:
            return _run_async_from_sync(
                self.check_async(
                    specs,
                    familiar_key=familiar_key,
                    familiar_seconds=familiar_seconds,
                    skip_on_familiar=skip_on_familiar,
                    now=now,
                )
            )
        keys = [familiar_key, *(spec.scope_key for spec in specs)]
        stmt = _build_check_select(keys, now)
        with self._sync_sessions() as session:
            rows = session.execute(stmt).all()
        return _evaluate_db_check_rows(
            rows,
            specs=specs,
            familiar_key=familiar_key,
            familiar_seconds=familiar_seconds,
            skip_on_familiar=skip_on_familiar,
            explicit_now=now,
        )

    async def record_failure_async(
        self,
        specs: Sequence[BucketPolicySpec],
        *,
        now: datetime | None,
    ) -> RateLimitDecision:
        if self._async_sessions is None:
            assert self._sync_sessions is not None
            return await asyncio.to_thread(self.record_failure_sync, specs, now=now)
        # Sắp xếp khóa theo thứ tự toàn phần để các transaction đồng thời không bao giờ deadlock.
        ordered_specs = sorted(specs, key=lambda spec: spec.scope_key)
        spec_by_key = {spec.scope_key: spec for spec in ordered_specs}
        rows: list[tuple[Any, ...]] = []
        async with self._async_sessions() as session, session.begin():
            for spec in ordered_specs:
                stmt = _build_failure_upsert(spec, now)
                row = (await session.execute(stmt)).first()
                if row is not None:
                    rows.append(row)
        return _evaluate_db_failure_rows(rows, spec_by_key=spec_by_key)

    def record_failure_sync(
        self,
        specs: Sequence[BucketPolicySpec],
        *,
        now: datetime | None,
    ) -> RateLimitDecision:
        if self._sync_sessions is None:
            return _run_async_from_sync(self.record_failure_async(specs, now=now))
        ordered_specs = sorted(specs, key=lambda spec: spec.scope_key)
        spec_by_key = {spec.scope_key: spec for spec in ordered_specs}
        rows: list[tuple[Any, ...]] = []
        with self._sync_sessions() as session, session.begin():
            for spec in ordered_specs:
                stmt = _build_failure_upsert(spec, now)
                row = session.execute(stmt).first()
                if row is not None:
                    rows.append(row)
        return _evaluate_db_failure_rows(rows, spec_by_key=spec_by_key)

    async def record_success_async(
        self,
        *,
        reset_keys: Sequence[str],
        familiar_key: str,
        now: datetime | None,
    ) -> None:
        if self._async_sessions is None:
            assert self._sync_sessions is not None
            await asyncio.to_thread(
                self.record_success_sync,
                reset_keys=reset_keys,
                familiar_key=familiar_key,
                now=now,
            )
            return
        delete_stmt, familiar_stmt = _build_success_statements(
            reset_keys=reset_keys,
            familiar_key=familiar_key,
            now=now,
        )
        async with self._async_sessions() as session, session.begin():
            if delete_stmt is not None:
                await session.execute(delete_stmt)
            await session.execute(familiar_stmt)

    def record_success_sync(
        self,
        *,
        reset_keys: Sequence[str],
        familiar_key: str,
        now: datetime | None,
    ) -> None:
        if self._sync_sessions is None:
            _run_async_from_sync(
                self.record_success_async(
                    reset_keys=reset_keys,
                    familiar_key=familiar_key,
                    now=now,
                )
            )
            return
        delete_stmt, familiar_stmt = _build_success_statements(
            reset_keys=reset_keys,
            familiar_key=familiar_key,
            now=now,
        )
        with self._sync_sessions() as session, session.begin():
            if delete_stmt is not None:
                session.execute(delete_stmt)
            session.execute(familiar_stmt)

    async def failure_count_async(
        self,
        scope_key: str,
        *,
        window_seconds: int,
        now: datetime | None,
    ) -> int:
        if self._async_sessions is None:
            assert self._sync_sessions is not None
            return await asyncio.to_thread(
                self.failure_count_sync,
                scope_key,
                window_seconds=window_seconds,
                now=now,
            )
        stmt = _build_check_select([scope_key], now)
        async with self._async_sessions() as session:
            row = (await session.execute(stmt)).first()
        return _extract_failure_count(row, window_seconds=window_seconds, explicit_now=now)

    def failure_count_sync(
        self,
        scope_key: str,
        *,
        window_seconds: int,
        now: datetime | None,
    ) -> int:
        if self._sync_sessions is None:
            return _run_async_from_sync(
                self.failure_count_async(scope_key, window_seconds=window_seconds, now=now)
            )
        stmt = _build_check_select([scope_key], now)
        with self._sync_sessions() as session:
            row = session.execute(stmt).first()
        return _extract_failure_count(row, window_seconds=window_seconds, explicit_now=now)

    async def purge_expired_async(
        self, *, older_than_seconds: int, now: datetime | None = None
    ) -> int:
        if self._async_sessions is None:
            raise RuntimeError("purge_expired_async yêu cầu async session factory")
        now_expr = _db_now_expr(now)
        cutoff = now_expr - timedelta(seconds=older_than_seconds)
        stmt = delete(AuthRateLimitState).where(
            AuthRateLimitState.updated_at < cutoff,
            or_(
                AuthRateLimitState.locked_until.is_(None),
                AuthRateLimitState.locked_until <= now_expr,
            ),
        )
        async with self._async_sessions() as session, session.begin():
            result = await session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)


class SharedAuthRateLimiter:
    """Limiter xác thực dùng chung giữa các replica qua PostgreSQL hoặc store chia sẻ (SEC-08).

    Áp dụng đồng thời 4 bộ đếm độc lập với khóa băm SHA-256 (không lưu PII):
    - ``pair`` (IP + email): mặc định 5 lỗi / 300 giây.
    - ``account`` (phạm vi workspace + email + factor): mặc định 10 lỗi / 900 giây,
      delay tăng dần từ lần lỗi thứ 5 trở đi (1s, 2s, 4s, 8s, 16s...).
    - ``email`` (email hash xuyên IP): mặc định 20 lỗi / 900 giây, delay tăng dần từ lần 5.
    - ``ip`` (IP nguồn): mặc định 20 lỗi / 900 giây chống password spraying.

    Cặp ``IP + email`` đã xác thực thành công trong ``familiar_pair_seconds`` (14 ngày) được
    miễn bộ đếm ``account`` và ``email`` để kẻ tấn công từ IP lạ không khóa vĩnh viễn chủ
    tài khoản tại IP quen, nhưng vẫn chịu bộ đếm ``pair`` và ``ip``.
    """

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        window_seconds: int = 300,
        account_max_attempts: int = 10,
        account_window_seconds: int = 900,
        email_max_attempts: int = 20,
        email_window_seconds: int = 900,
        ip_max_attempts: int = 20,
        ip_window_seconds: int = 900,
        familiar_pair_seconds: int = 14 * 24 * 3600,
        max_tracked_keys: int = 10_000,
        progressive_delay_start: int | None = 5,
        progressive_delay_base_seconds: float = 1.0,
        progressive_delay_max_seconds: float = 30.0,
        lockout_seconds: int | None = None,
        session_factory: async_sessionmaker[AsyncSession] | sessionmaker[Session] | None = None,
        engine: AsyncEngine | Engine | None = None,
        store: InMemoryRateLimitStore | None = None,
        clock: Callable[[], datetime | float] | None = None,
    ) -> None:
        _validate_positive_thresholds(
            max_attempts=max_attempts,
            window_seconds=window_seconds,
            account_max_attempts=account_max_attempts,
            account_window_seconds=account_window_seconds,
            email_max_attempts=email_max_attempts,
            email_window_seconds=email_window_seconds,
            ip_max_attempts=ip_max_attempts,
            ip_window_seconds=ip_window_seconds,
            familiar_pair_seconds=familiar_pair_seconds,
            max_tracked_keys=max_tracked_keys,
        )
        if progressive_delay_start is not None and progressive_delay_start < 1:
            raise ValueError("Ngưỡng bắt đầu progressive delay phải lớn hơn 0")
        if progressive_delay_base_seconds < 0 or progressive_delay_max_seconds < 0:
            raise ValueError("Thời gian progressive delay không được âm")
        if lockout_seconds is not None and lockout_seconds < 1:
            raise ValueError("Thời gian lockout phải lớn hơn 0")
        if session_factory is not None and engine is not None:
            raise ValueError("Chỉ truyền một trong session_factory hoặc engine")
        if (session_factory is not None or engine is not None) and store is not None:
            raise ValueError("Không kết hợp session_factory/engine với in-memory store")

        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.account_max_attempts = account_max_attempts
        self.account_window_seconds = account_window_seconds
        self.email_max_attempts = email_max_attempts
        self.email_window_seconds = email_window_seconds
        self.ip_max_attempts = ip_max_attempts
        self.ip_window_seconds = ip_window_seconds
        self.familiar_pair_seconds = familiar_pair_seconds
        self.progressive_delay_start = progressive_delay_start
        self.progressive_delay_base_seconds = progressive_delay_base_seconds
        self.progressive_delay_max_seconds = progressive_delay_max_seconds
        self.lockout_seconds = lockout_seconds
        self._max_tracked_keys = max_tracked_keys
        self._clock = clock

        self._memory_store: InMemoryRateLimitStore | None = None
        self._postgres_store: PostgresRateLimitStore | None = None
        resolved_factory = _resolve_session_factory(session_factory=session_factory, engine=engine)
        if resolved_factory is not None:
            self._postgres_store = _build_postgres_store(resolved_factory)
        else:
            self._memory_store = store or InMemoryRateLimitStore(max_keys=max_tracked_keys)

    @property
    def session_factory(self) -> Any:
        if self._postgres_store is None:
            return None
        return self._postgres_store.session_factory

    @property
    def is_shared_database(self) -> bool:
        return self._postgres_store is not None

    def bind_session_factory(
        self,
        session_factory: async_sessionmaker[AsyncSession] | sessionmaker[Session],
    ) -> None:
        """Gắn session factory PostgreSQL sau khi app khởi tạo engine trong lifespan."""
        self._postgres_store = _build_postgres_store(session_factory)

    def _build_specs(
        self,
        client_ip: str,
        email: str,
        *,
        account_scope: str = "",
        factor: str = "",
        include_account: bool = True,
        include_email: bool = True,
    ) -> tuple[list[BucketPolicySpec], str]:
        address = client_ip.strip()
        account = email.strip().casefold()
        scope = account_scope.strip().casefold()
        norm_factor = factor.strip().casefold()

        pair_key = hash_rate_limit_key("pair", address, account)
        ip_key = hash_rate_limit_key("ip", address)
        account_key = hash_rate_limit_key("account", scope, account, norm_factor)
        email_key = hash_rate_limit_key("email", account)
        familiar_key = hash_rate_limit_key("familiar", address, account)

        specs = [
            BucketPolicySpec(
                scope_key=pair_key,
                scope_type="pair",
                max_attempts=self.max_attempts,
                window_seconds=self.window_seconds,
                lockout_seconds=self.lockout_seconds or self.window_seconds,
                progressive_delay_start=self.progressive_delay_start,
                progressive_delay_base_seconds=self.progressive_delay_base_seconds,
                progressive_delay_max_seconds=self.progressive_delay_max_seconds,
            ),
            BucketPolicySpec(
                scope_key=ip_key,
                scope_type="ip",
                max_attempts=self.ip_max_attempts,
                window_seconds=self.ip_window_seconds,
                lockout_seconds=self.lockout_seconds or self.ip_window_seconds,
                # Bộ đếm IP chống password spraying trên nhiều email nên giữ ngưỡng cứng IP
                # thay vì áp delay từ lần 5 (tránh chặn sớm lượt thử email khác nhau hợp lệ).
                progressive_delay_start=None,
                progressive_delay_base_seconds=0.0,
                progressive_delay_max_seconds=0.0,
            ),
        ]
        if include_account:
            specs.append(
                BucketPolicySpec(
                    scope_key=account_key,
                    scope_type="account",
                    max_attempts=self.account_max_attempts,
                    window_seconds=self.account_window_seconds,
                    lockout_seconds=self.lockout_seconds or self.account_window_seconds,
                    progressive_delay_start=self.progressive_delay_start,
                    progressive_delay_base_seconds=self.progressive_delay_base_seconds,
                    progressive_delay_max_seconds=self.progressive_delay_max_seconds,
                )
            )
        if include_account and include_email:
            specs.append(
                BucketPolicySpec(
                    scope_key=email_key,
                    scope_type="email",
                    max_attempts=self.email_max_attempts,
                    window_seconds=self.email_window_seconds,
                    lockout_seconds=self.lockout_seconds or self.email_window_seconds,
                    progressive_delay_start=self.progressive_delay_start,
                    progressive_delay_base_seconds=self.progressive_delay_base_seconds,
                    progressive_delay_max_seconds=self.progressive_delay_max_seconds,
                )
            )
        return specs, familiar_key

    def _keys_for_reset(
        self,
        client_ip: str,
        email: str,
        *,
        account_scope: str = "",
        factor: str = "",
        reset_email: bool = True,
    ) -> tuple[list[str], str]:
        address = client_ip.strip()
        account = email.strip().casefold()
        scope = account_scope.strip().casefold()
        norm_factor = factor.strip().casefold()

        reset_keys = [
            hash_rate_limit_key("pair", address, account),
            hash_rate_limit_key("account", scope, account, norm_factor),
        ]
        if reset_email:
            reset_keys.append(hash_rate_limit_key("email", account))
        familiar_key = hash_rate_limit_key("familiar", address, account)
        return reset_keys, familiar_key

    def check(
        self,
        client_ip: str,
        email: str,
        *,
        account_scope: str = "",
        factor: str = "",
        check_email: bool = True,
        now: float | datetime | None = None,
    ) -> RateLimitDecision:
        specs, familiar_key = self._build_specs(
            client_ip,
            email,
            account_scope=account_scope,
            factor=factor,
            include_account=True,
            include_email=check_email,
        )
        if self._postgres_store is not None:
            return self._postgres_store.check_sync(
                specs,
                familiar_key=familiar_key,
                familiar_seconds=self.familiar_pair_seconds,
                skip_on_familiar=frozenset({"account", "email"}),
                now=self._resolve_db_now(now),
            )
        assert self._memory_store is not None
        return self._memory_store.check(
            specs,
            familiar_key=familiar_key,
            familiar_seconds=self.familiar_pair_seconds,
            skip_on_familiar=frozenset({"account", "email"}),
            now=self._resolve_memory_now(now),
        )

    async def check_async(
        self,
        client_ip: str,
        email: str,
        *,
        account_scope: str = "",
        factor: str = "",
        check_email: bool = True,
        now: float | datetime | None = None,
    ) -> RateLimitDecision:
        specs, familiar_key = self._build_specs(
            client_ip,
            email,
            account_scope=account_scope,
            factor=factor,
            include_account=True,
            include_email=check_email,
        )
        if self._postgres_store is not None:
            return await self._postgres_store.check_async(
                specs,
                familiar_key=familiar_key,
                familiar_seconds=self.familiar_pair_seconds,
                skip_on_familiar=frozenset({"account", "email"}),
                now=self._resolve_db_now(now),
            )
        assert self._memory_store is not None
        return self._memory_store.check(
            specs,
            familiar_key=familiar_key,
            familiar_seconds=self.familiar_pair_seconds,
            skip_on_familiar=frozenset({"account", "email"}),
            now=self._resolve_memory_now(now),
        )

    def allow(
        self,
        client_ip: str,
        email: str,
        *,
        account_scope: str = "",
        factor: str = "",
        check_email: bool = True,
        now: float | datetime | None = None,
    ) -> bool:
        return self.check(
            client_ip,
            email,
            account_scope=account_scope,
            factor=factor,
            check_email=check_email,
            now=now,
        ).allowed

    async def allow_async(
        self,
        client_ip: str,
        email: str,
        *,
        account_scope: str = "",
        factor: str = "",
        check_email: bool = True,
        now: float | datetime | None = None,
    ) -> bool:
        return (
            await self.check_async(
                client_ip,
                email,
                account_scope=account_scope,
                factor=factor,
                check_email=check_email,
                now=now,
            )
        ).allowed

    def record_failure(
        self,
        client_ip: str,
        email: str,
        *,
        account_scope: str = "",
        factor: str = "",
        count_account: bool = True,
        check_email: bool = True,
        now: float | datetime | None = None,
    ) -> RateLimitDecision:
        """Ghi lỗi; ``count_account=False`` không ghi bộ đếm tài khoản/email."""
        specs, _ = self._build_specs(
            client_ip,
            email,
            account_scope=account_scope,
            factor=factor,
            include_account=count_account,
            include_email=count_account and check_email,
        )
        if self._postgres_store is not None:
            return self._postgres_store.record_failure_sync(specs, now=self._resolve_db_now(now))
        assert self._memory_store is not None
        return self._memory_store.record_failure(specs, now=self._resolve_memory_now(now))

    async def record_failure_async(
        self,
        client_ip: str,
        email: str,
        *,
        account_scope: str = "",
        factor: str = "",
        count_account: bool = True,
        check_email: bool = True,
        now: float | datetime | None = None,
    ) -> RateLimitDecision:
        """Bản bất đồng bộ của ``record_failure`` cho FastAPI/async PostgreSQL."""
        specs, _ = self._build_specs(
            client_ip,
            email,
            account_scope=account_scope,
            factor=factor,
            include_account=count_account,
            include_email=count_account and check_email,
        )
        if self._postgres_store is not None:
            return await self._postgres_store.record_failure_async(
                specs, now=self._resolve_db_now(now)
            )
        assert self._memory_store is not None
        return self._memory_store.record_failure(specs, now=self._resolve_memory_now(now))

    def record_success(
        self,
        client_ip: str,
        email: str,
        *,
        account_scope: str = "",
        factor: str = "",
        reset_email: bool = True,
        now: float | datetime | None = None,
    ) -> None:
        """Xóa bộ đếm lỗi liên tiếp, đánh dấu cặp IP + email quen; giữ bộ đếm IP."""
        reset_keys, familiar_key = self._keys_for_reset(
            client_ip,
            email,
            account_scope=account_scope,
            factor=factor,
            reset_email=reset_email,
        )
        if self._postgres_store is not None:
            self._postgres_store.record_success_sync(
                reset_keys=reset_keys,
                familiar_key=familiar_key,
                now=self._resolve_db_now(now),
            )
            return
        assert self._memory_store is not None
        self._memory_store.record_success(
            reset_keys=reset_keys,
            familiar_key=familiar_key,
            familiar_seconds=self.familiar_pair_seconds,
            now=self._resolve_memory_now(now),
        )

    async def record_success_async(
        self,
        client_ip: str,
        email: str,
        *,
        account_scope: str = "",
        factor: str = "",
        reset_email: bool = True,
        now: float | datetime | None = None,
    ) -> None:
        """Bản bất đồng bộ của ``record_success`` cho FastAPI/async PostgreSQL."""
        reset_keys, familiar_key = self._keys_for_reset(
            client_ip,
            email,
            account_scope=account_scope,
            factor=factor,
            reset_email=reset_email,
        )
        if self._postgres_store is not None:
            await self._postgres_store.record_success_async(
                reset_keys=reset_keys,
                familiar_key=familiar_key,
                now=self._resolve_db_now(now),
            )
            return
        assert self._memory_store is not None
        self._memory_store.record_success(
            reset_keys=reset_keys,
            familiar_key=familiar_key,
            familiar_seconds=self.familiar_pair_seconds,
            now=self._resolve_memory_now(now),
        )

    def account_failure_count(
        self,
        account_scope: str,
        email: str,
        *,
        factor: str = "",
        now: float | datetime | None = None,
    ) -> int:
        """Số lỗi đang tính trong bộ đếm tài khoản (cho test/chẩn đoán, không lộ ra API)."""
        account_key = hash_rate_limit_key(
            "account",
            account_scope.strip().casefold(),
            email.strip().casefold(),
            factor.strip().casefold(),
        )
        if self._postgres_store is not None:
            return self._postgres_store.failure_count_sync(
                account_key,
                window_seconds=self.account_window_seconds,
                now=self._resolve_db_now(now),
            )
        assert self._memory_store is not None
        resolved_now = self._resolve_memory_now(now) if now is not None else None
        return self._memory_store.failure_count(
            account_key,
            window_seconds=self.account_window_seconds,
            now=resolved_now,
        )

    async def account_failure_count_async(
        self,
        account_scope: str,
        email: str,
        *,
        factor: str = "",
        now: float | datetime | None = None,
    ) -> int:
        account_key = hash_rate_limit_key(
            "account",
            account_scope.strip().casefold(),
            email.strip().casefold(),
            factor.strip().casefold(),
        )
        if self._postgres_store is not None:
            return await self._postgres_store.failure_count_async(
                account_key,
                window_seconds=self.account_window_seconds,
                now=self._resolve_db_now(now),
            )
        assert self._memory_store is not None
        resolved_now = self._resolve_memory_now(now) if now is not None else None
        return self._memory_store.failure_count(
            account_key,
            window_seconds=self.account_window_seconds,
            now=resolved_now,
        )

    def tracked_key_count(self) -> int:
        if self._memory_store is not None and self._postgres_store is None:
            return self._memory_store.tracked_key_count()
        return 0

    def _resolve_memory_now(self, now: float | datetime | None) -> float:
        if now is not None:
            if isinstance(now, datetime):
                if now.tzinfo is None:
                    raise ValueError("Thời điểm phải có timezone UTC")
                return now.astimezone(UTC).timestamp()
            return float(now)
        if self._clock is not None:
            tick = self._clock()
            if isinstance(tick, datetime):
                if tick.tzinfo is None:
                    raise ValueError("Thời điểm clock phải có timezone UTC")
                return tick.astimezone(UTC).timestamp()
            return float(tick)
        return monotonic()

    def _resolve_db_now(self, now: float | datetime | None) -> datetime | None:
        if now is not None:
            if isinstance(now, datetime):
                if now.tzinfo is None:
                    raise ValueError("Thời điểm phải có timezone UTC")
                return now.astimezone(UTC)
            return datetime.fromtimestamp(float(now), tz=UTC)
        if self._clock is not None:
            tick = self._clock()
            if isinstance(tick, datetime):
                if tick.tzinfo is None:
                    raise ValueError("Thời điểm clock phải có timezone UTC")
                return tick.astimezone(UTC)
            return datetime.fromtimestamp(float(tick), tz=UTC)
        return None


def _validate_positive_thresholds(**values: int) -> None:
    for value in values.values():
        if value < 1:
            raise ValueError("Ngưỡng giới hạn đăng nhập phải lớn hơn 0")


def _resolve_session_factory(
    *,
    session_factory: async_sessionmaker[AsyncSession] | sessionmaker[Session] | None,
    engine: AsyncEngine | Engine | None,
) -> async_sessionmaker[AsyncSession] | sessionmaker[Session] | None:
    if session_factory is not None:
        return session_factory
    if isinstance(engine, AsyncEngine):
        return async_sessionmaker(engine, expire_on_commit=False)
    if isinstance(engine, Engine):
        return sessionmaker(engine, expire_on_commit=False)
    return None


def _build_postgres_store(
    factory: async_sessionmaker[AsyncSession] | sessionmaker[Session] | Any,
) -> PostgresRateLimitStore:
    if isinstance(factory, sessionmaker):
        return PostgresRateLimitStore(sync_sessions=factory)
    return PostgresRateLimitStore(async_sessions=factory)


def _candidate_lock_timestamp(spec: BucketPolicySpec, failures: int, now: float) -> float | None:
    if failures >= spec.max_attempts:
        return now + spec.lockout_seconds
    if spec.progressive_delay_start is not None and failures >= spec.progressive_delay_start:
        delay = compute_progressive_delay_seconds(
            failures,
            start_at=spec.progressive_delay_start,
            base_seconds=spec.progressive_delay_base_seconds,
            max_seconds=spec.progressive_delay_max_seconds,
        )
        if delay > 0:
            return now + delay
    return None


def _blocked_decision(
    *,
    remaining: float,
    failures: int,
    locked_until_ts: float | None,
    blocking_scope: str | None,
) -> RateLimitDecision:
    locked_until_dt = (
        datetime.fromtimestamp(locked_until_ts, tz=UTC) if locked_until_ts is not None else None
    )
    return RateLimitDecision(
        allowed=False,
        retry_after_seconds=max(1, math.ceil(remaining)),
        delay_seconds=remaining,
        failures=failures,
        locked_until=locked_until_dt,
        blocking_scope=blocking_scope,
    )


def _db_now_expr(explicit_now: datetime | None) -> Any:
    if explicit_now is not None:
        return literal(explicit_now, type_=DateTime(timezone=True))
    return func.now()


def _build_check_select(keys: Sequence[str], explicit_now: datetime | None) -> Any:
    now_expr = _db_now_expr(explicit_now)
    return select(
        AuthRateLimitState.scope_key,
        AuthRateLimitState.scope_type,
        AuthRateLimitState.failures,
        AuthRateLimitState.window_started_at,
        AuthRateLimitState.locked_until,
        now_expr.label("eval_now"),
    ).where(AuthRateLimitState.scope_key.in_(keys))


def _build_failure_upsert(spec: BucketPolicySpec, explicit_now: datetime | None) -> Any:
    now_expr = _db_now_expr(explicit_now)
    win_td = timedelta(seconds=spec.window_seconds)
    lock_td = timedelta(seconds=spec.lockout_seconds)

    initial_locked_expr: Any
    if 1 >= spec.max_attempts:
        initial_locked_expr = now_expr + lock_td
    elif spec.progressive_delay_start is not None and 1 >= spec.progressive_delay_start:
        initial_delay = compute_progressive_delay_seconds(
            1,
            start_at=spec.progressive_delay_start,
            base_seconds=spec.progressive_delay_base_seconds,
            max_seconds=spec.progressive_delay_max_seconds,
        )
        initial_locked_expr = (
            now_expr + timedelta(seconds=initial_delay)
            if initial_delay > 0
            else literal(None, type_=DateTime(timezone=True))
        )
    else:
        initial_locked_expr = literal(None, type_=DateTime(timezone=True))

    lock_expired = or_(
        AuthRateLimitState.locked_until.is_(None),
        AuthRateLimitState.locked_until <= now_expr,
    )
    window_expired = or_(
        AuthRateLimitState.window_started_at + win_td <= now_expr,
        AuthRateLimitState.failures >= spec.max_attempts,
    )
    reset_cond = and_(lock_expired, window_expired)
    next_failures = case((reset_cond, 1), else_=AuthRateLimitState.failures + 1)
    next_window_started = case((reset_cond, now_expr), else_=AuthRateLimitState.window_started_at)

    lockout_target = func.greatest(
        func.coalesce(AuthRateLimitState.locked_until, now_expr),
        now_expr + lock_td,
    )
    preserved_or_cleared = case(
        (reset_cond, literal(None, type_=DateTime(timezone=True))),
        else_=AuthRateLimitState.locked_until,
    )
    if (
        spec.progressive_delay_start is not None
        and spec.progressive_delay_base_seconds > 0
        and spec.progressive_delay_max_seconds > 0
    ):
        exponent = func.greatest(
            literal(0),
            func.least(next_failures - literal(spec.progressive_delay_start), literal(30)),
        )
        delay_secs = func.least(
            literal(float(spec.progressive_delay_max_seconds), type_=Float),
            literal(float(spec.progressive_delay_base_seconds), type_=Float)
            * func.power(literal(2.0, type_=Float), exponent),
        )
        delay_target = func.greatest(
            func.coalesce(AuthRateLimitState.locked_until, now_expr),
            now_expr + func.make_interval(0, 0, 0, 0, 0, 0, delay_secs),
        )
        next_locked = case(
            (next_failures >= spec.max_attempts, lockout_target),
            (next_failures >= spec.progressive_delay_start, delay_target),
            else_=preserved_or_cleared,
        )
    else:
        next_locked = case(
            (next_failures >= spec.max_attempts, lockout_target),
            else_=preserved_or_cleared,
        )

    return (
        pg_insert(AuthRateLimitState)
        .values(
            scope_key=spec.scope_key,
            scope_type=spec.scope_type,
            failures=1,
            window_started_at=now_expr,
            locked_until=initial_locked_expr,
            updated_at=now_expr,
        )
        .on_conflict_do_update(
            index_elements=[AuthRateLimitState.scope_key],
            set_={
                "scope_type": spec.scope_type,
                "failures": next_failures,
                "window_started_at": next_window_started,
                "locked_until": next_locked,
                "updated_at": now_expr,
            },
        )
        .returning(
            AuthRateLimitState.scope_key,
            AuthRateLimitState.failures,
            AuthRateLimitState.window_started_at,
            AuthRateLimitState.locked_until,
            now_expr.label("eval_now"),
        )
    )


def _build_success_statements(
    *,
    reset_keys: Sequence[str],
    familiar_key: str,
    now: datetime | None,
) -> tuple[Any | None, Any]:
    now_expr = _db_now_expr(now)
    delete_stmt = (
        delete(AuthRateLimitState).where(AuthRateLimitState.scope_key.in_(reset_keys))
        if reset_keys
        else None
    )
    familiar_stmt = (
        pg_insert(AuthRateLimitState)
        .values(
            scope_key=familiar_key,
            scope_type="familiar",
            failures=0,
            window_started_at=now_expr,
            locked_until=None,
            updated_at=now_expr,
        )
        .on_conflict_do_update(
            index_elements=[AuthRateLimitState.scope_key],
            set_={
                "scope_type": "familiar",
                "failures": 0,
                "window_started_at": now_expr,
                "locked_until": None,
                "updated_at": now_expr,
            },
        )
    )
    return delete_stmt, familiar_stmt


def _evaluate_db_check_rows(
    rows: Sequence[tuple[Any, ...]],
    *,
    specs: Sequence[BucketPolicySpec],
    familiar_key: str,
    familiar_seconds: int,
    skip_on_familiar: frozenset[str],
    explicit_now: datetime | None,
) -> RateLimitDecision:
    if not rows:
        return RateLimitDecision(allowed=True)
    by_key = {str(row[0]): row for row in rows}
    eval_now = explicit_now or cast(datetime, rows[0][5])

    familiar = False
    fam_row = by_key.get(familiar_key)
    if fam_row is not None:
        fam_started = cast(datetime, fam_row[3])
        if fam_started + timedelta(seconds=familiar_seconds) > eval_now:
            familiar = True

    max_remaining = 0.0
    max_locked_until: datetime | None = None
    blocking_scope: str | None = None
    blocking_failures = 0
    active_failures = 0

    for spec in specs:
        if familiar and spec.scope_type in skip_on_familiar:
            continue
        row = by_key.get(spec.scope_key)
        if row is None:
            continue
        failures = int(row[2])
        window_started_at = cast(datetime, row[3])
        locked_until = cast(datetime | None, row[4])
        window_end = window_started_at + timedelta(seconds=spec.window_seconds)

        remaining = 0.0
        effective_lock = locked_until
        if locked_until is not None and locked_until > eval_now:
            remaining = (locked_until - eval_now).total_seconds()
        elif window_end > eval_now:
            active_failures = max(active_failures, failures)
            if failures >= spec.max_attempts:
                remaining = (window_end - eval_now).total_seconds()
                effective_lock = window_end

        if remaining > max_remaining:
            max_remaining = remaining
            max_locked_until = effective_lock
            blocking_scope = spec.scope_type
            blocking_failures = failures

    if max_remaining > 0.0:
        return RateLimitDecision(
            allowed=False,
            retry_after_seconds=max(1, math.ceil(max_remaining)),
            delay_seconds=max_remaining,
            failures=blocking_failures,
            locked_until=max_locked_until,
            blocking_scope=blocking_scope,
        )
    return RateLimitDecision(allowed=True, failures=active_failures)


def _evaluate_db_failure_rows(
    rows: Sequence[tuple[Any, ...]],
    *,
    spec_by_key: dict[str, BucketPolicySpec],
) -> RateLimitDecision:
    max_remaining = 0.0
    max_locked_until: datetime | None = None
    blocking_scope: str | None = None
    blocking_failures = 0
    max_failures = 0

    for row in rows:
        scope_key = str(row[0])
        failures = int(row[1])
        locked_until = cast(datetime | None, row[3])
        eval_now = cast(datetime, row[4])
        max_failures = max(max_failures, failures)
        spec = spec_by_key.get(scope_key)
        if locked_until is not None and locked_until > eval_now:
            remaining = (locked_until - eval_now).total_seconds()
            if remaining > max_remaining:
                max_remaining = remaining
                max_locked_until = locked_until
                blocking_scope = spec.scope_type if spec is not None else None
                blocking_failures = failures

    if max_remaining > 0.0:
        return RateLimitDecision(
            allowed=False,
            retry_after_seconds=max(1, math.ceil(max_remaining)),
            delay_seconds=max_remaining,
            failures=blocking_failures,
            locked_until=max_locked_until,
            blocking_scope=blocking_scope,
        )
    return RateLimitDecision(allowed=True, failures=max_failures)


def _extract_failure_count(
    row: tuple[Any, ...] | None,
    *,
    window_seconds: int,
    explicit_now: datetime | None,
) -> int:
    if row is None:
        return 0
    failures = int(row[2])
    window_started_at = cast(datetime, row[3])
    locked_until = cast(datetime | None, row[4])
    eval_now = explicit_now or cast(datetime, row[5])
    if locked_until is not None and locked_until > eval_now:
        return failures
    if window_started_at + timedelta(seconds=window_seconds) <= eval_now:
        return 0
    return failures


def _run_async_from_sync[T](coro: Coroutine[Any, Any, _T]) -> _T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError(
        "Không thể gọi phương thức sync của SharedAuthRateLimiter khi đang ở trong event loop; "
        "hãy dùng bản *_async."
    )


__all__ = [
    "BucketPolicySpec",
    "InMemoryRateLimitStore",
    "PostgresRateLimitStore",
    "RateLimitDecision",
    "SharedAuthRateLimiter",
    "compute_progressive_delay_seconds",
    "hash_rate_limit_key",
]
