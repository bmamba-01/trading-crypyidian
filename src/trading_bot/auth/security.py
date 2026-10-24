import hmac
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from time import monotonic

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError


class PasswordManager:
    """Băm mật khẩu bằng Argon2id với tham số mặc định của thư viện."""

    def __init__(self) -> None:
        self._hasher = PasswordHasher()

    def hash(self, password: str) -> str:
        if len(password) < 14:
            raise ValueError("Mật khẩu cần tối thiểu 14 ký tự")
        return self._hasher.hash(password)

    def verify(self, encoded_hash: str, password: str) -> bool:
        try:
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


class LoginRateLimiter:
    """Giới hạn thử đăng nhập trong một process để giảm đoán mật khẩu tự động."""

    def __init__(self, *, max_attempts: int = 5, window_seconds: int = 300) -> None:
        if max_attempts < 1 or window_seconds < 1:
            raise ValueError("Ngưỡng giới hạn đăng nhập phải lớn hơn 0")
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    @staticmethod
    def _key(client_ip: str, email: str) -> str:
        return f"{client_ip.strip()}:{email.strip().casefold()}"

    def allow(self, client_ip: str, email: str, *, now: float | None = None) -> bool:
        timestamp = now if now is not None else monotonic()
        key = self._key(client_ip, email)
        with self._lock:
            failures = self._failures.get(key)
            if failures is None:
                return True
            self._discard_expired(failures, timestamp)
            if not failures:
                self._failures.pop(key, None)
                return True
            return len(failures) < self.max_attempts

    def record_failure(self, client_ip: str, email: str, *, now: float | None = None) -> None:
        timestamp = now if now is not None else monotonic()
        with self._lock:
            failures = self._failures[self._key(client_ip, email)]
            self._discard_expired(failures, timestamp)
            failures.append(timestamp)

    def record_success(self, client_ip: str, email: str) -> None:
        with self._lock:
            self._failures.pop(self._key(client_ip, email), None)

    def _discard_expired(self, failures: deque[float], now: float) -> None:
        while failures and now - failures[0] >= self.window_seconds:
            failures.popleft()


def verify_bootstrap_token(token_file: Path | None, candidate: str | None) -> bool:
    if token_file is None or not candidate:
        return False
    try:
        expected = token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return bool(expected) and hmac.compare_digest(expected, candidate)
