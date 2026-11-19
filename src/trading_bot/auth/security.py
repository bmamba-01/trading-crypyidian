import hmac
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import BoundedSemaphore

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from trading_bot.auth.shared_limiter import (
    InMemoryRateLimitStore,
    PostgresRateLimitStore,
    RateLimitDecision,
    SharedAuthRateLimiter,
    compute_progressive_delay_seconds,
    hash_rate_limit_key,
)

# SEC-12: hash Argon2id hợp lệ dùng để chống timing enumeration khi user/workspace không tồn tại.
# Sinh một lần từ chuỗi ngẫu nhiên 256 bit đã hủy bỏ; thông số khớp mặc định argon2-cffi
# (t=3, m=64 MiB, p=4).
DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$jIWONG7nXQP18JBPOWbHNw$"
    "Al+z41HXIY5SrAX8qbrktJoAtQLr5/t0xgAojoFAUFA"
)


class PasswordManager:
    """Băm mật khẩu bằng Argon2id với giới hạn số phép chạy đồng thời (SEC-08)."""

    def __init__(self, *, max_concurrency: int = 8) -> None:
        if max_concurrency < 1:
            raise ValueError("Giới hạn đồng thời Argon2 phải lớn hơn 0")
        self._hasher = PasswordHasher()
        self._concurrency_guard = BoundedSemaphore(max_concurrency)

    def hash(self, password: str) -> str:
        if len(password) < 14:
            raise ValueError("Mật khẩu cần tối thiểu 14 ký tự")
        with self._concurrency_guard:
            return self._hasher.hash(password)

    def verify(self, encoded_hash: str, password: str) -> bool:
        try:
            with self._concurrency_guard:
                return self._hasher.verify(encoded_hash, password)
        except InvalidHashError, VerificationError:
            return False

    def needs_rehash(self, encoded_hash: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(encoded_hash)
        except InvalidHashError:
            return True


class TotpManager:
    """Tạo và xác minh mã TOTP; mỗi request chỉ chấp nhận chu kỳ hiện hành."""

    def generate_secret(self) -> str:
        return pyotp.random_base32()

    def provisioning_uri(self, secret: str, *, account: str) -> str:
        return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name="Crypidian")

    def code_at(self, secret: str, at: datetime) -> str:
        if at.tzinfo is None:
            raise ValueError("Thời điểm TOTP phải có timezone")
        return pyotp.TOTP(secret).at(at.astimezone(UTC))

    def verify(self, secret: str, code: str, at: datetime | None = None) -> bool:
        current_time = at or datetime.now(UTC)
        if current_time.tzinfo is None or len(code) != 6 or not code.isdigit():
            return False
        return pyotp.TOTP(secret).verify(code, for_time=current_time.astimezone(UTC))

    def matching_counter(
        self, secret: str, code: str, at: datetime, *, valid_window: int
    ) -> int | None:
        if at.tzinfo is None or valid_window < 0 or len(code) != 6 or not code.isdigit():
            return None
        current_time = at.astimezone(UTC)
        offsets = [0]
        for distance in range(1, valid_window + 1):
            offsets.extend((-distance, distance))
        totp = pyotp.TOTP(secret)
        for offset in offsets:
            candidate_time = current_time + timedelta(seconds=totp.interval * offset)
            if totp.at(candidate_time) == code:
                return int(candidate_time.timestamp()) // totp.interval
        return None


# Mã xác thực cố định cho chế độ kiểm thử local (cờ `auth_test_totp_enabled`).
TEST_TOTP_CODE = "123456"


class TestModeTotpManager(TotpManager):
    """TOTP cho stack kiểm thử local: chấp nhận thêm mã cố định `123456`.

    Chỉ được tạo khi cấu hình đã kiểm `app_env` local/test và mode PAPER. Mã thật vẫn hợp lệ.
    Mã cố định ứng với chu kỳ 30 giây hiện hành, nên cơ chế chống dùng lại theo counter vẫn
    áp dụng: hai lần đăng nhập bằng mã cố định trong cùng một chu kỳ, lần sau bị từ chối.
    """

    def matching_counter(
        self, secret: str, code: str, at: datetime, *, valid_window: int
    ) -> int | None:
        if code == TEST_TOTP_CODE and at.tzinfo is not None:
            return int(at.astimezone(UTC).timestamp()) // pyotp.TOTP(secret).interval
        return super().matching_counter(secret, code, at, valid_window=valid_window)


class LoginRateLimiter(SharedAuthRateLimiter):
    """Giới hạn thử xác thực tương thích ngược, nâng cấp sang kho bền/chia sẻ (SEC-08).

    Khi khởi tạo không truyền ``session_factory``/``engine``, limiter chạy ở chế độ in-memory
    tương thích 100% với bộ unit test. Khi gắn ``session_factory`` PostgreSQL (hoặc ``store``
    dùng chung), nhiều replica chia sẻ cùng bộ đếm, progressive delay từ lần lỗi thứ 5 và
    lockout bền qua restart.
    """


def verify_bootstrap_token(token_file: Path | None, candidate: str | None) -> bool:
    if token_file is None or not candidate:
        return False
    try:
        expected = token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return bool(expected) and hmac.compare_digest(expected, candidate)


__all__ = [
    "TEST_TOTP_CODE",
    "InMemoryRateLimitStore",
    "LoginRateLimiter",
    "PasswordManager",
    "PostgresRateLimitStore",
    "RateLimitDecision",
    "SharedAuthRateLimiter",
    "TestModeTotpManager",
    "TotpManager",
    "compute_progressive_delay_seconds",
    "hash_rate_limit_key",
    "verify_bootstrap_token",
]
