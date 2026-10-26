import hmac
from collections import deque
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


class _SlidingWindowCounter:
    """Đếm lỗi theo khóa trong cửa sổ trượt; gọi khi đã giữ lock của limiter."""

    def __init__(self, *, max_attempts: int, window_seconds: int, max_keys: int) -> None:
        if max_attempts < 1 or window_seconds < 1 or max_keys < 1:
            raise ValueError("Ngưỡng giới hạn đăng nhập phải lớn hơn 0")
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._failures: dict[str, deque[float]] = {}

    def allow(self, key: str, now: float) -> bool:
        failures = self._failures.get(key)
        if failures is None:
            return True
        self._discard_expired(failures, now)
        if not failures:
            self._failures.pop(key, None)
            return True
        return len(failures) < self.max_attempts

    def record(self, key: str, now: float) -> None:
        failures = self._failures.get(key)
        if failures is None:
            if len(self._failures) >= self.max_keys:
                self._evict(now)
            failures = self._failures[key] = deque()
        self._discard_expired(failures, now)
        failures.append(now)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)

    def count(self, key: str) -> int:
        failures = self._failures.get(key)
        return len(failures) if failures is not None else 0

    def __len__(self) -> int:
        return len(self._failures)

    def _evict(self, now: float) -> None:
        # Dọn khóa hết cửa sổ trước; nếu vẫn đầy thì bỏ khóa cũ nhất (thứ tự chèn).
        for key in list(self._failures):
            failures = self._failures[key]
            self._discard_expired(failures, now)
            if not failures:
                del self._failures[key]
        while len(self._failures) >= self.max_keys:
            del self._failures[next(iter(self._failures))]

    def _discard_expired(self, failures: deque[float], now: float) -> None:
        while failures and now - failures[0] >= self.window_seconds:
            failures.popleft()


class LoginRateLimiter:
    """Giới hạn thử xác thực trong RAM của một process (SEC-08, bước đầu).

    Ba bộ đếm độc lập, chặn khi bất kỳ bộ nào chạm ngưỡng: theo IP + email, theo tài
    khoản (phạm vi workspace + email chuẩn hóa, mọi IP) chống credential stuffing phân
    tán, và theo IP (mọi email) chống password spraying. Cặp IP + email đã đăng nhập
    thành công trong `familiar_pair_seconds` được bỏ qua bộ đếm tài khoản (vẫn chịu bộ
    đếm IP + email và IP) để kẻ tấn công không khóa vô hạn chủ tài khoản. Khóa dựa trên
    email người gửi nhập nên hành vi giống nhau dù tài khoản có tồn tại hay không.
    Giới hạn: mất khi restart và không chia sẻ giữa replica; limiter bền vững (DB/Redis)
    để MB-07/MB-09.
    """

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        window_seconds: int = 300,
        account_max_attempts: int = 10,
        account_window_seconds: int = 900,
        ip_max_attempts: int = 20,
        ip_window_seconds: int = 900,
        familiar_pair_seconds: int = 14 * 24 * 3600,
        max_tracked_keys: int = 10_000,
    ) -> None:
        if familiar_pair_seconds < 1:
            raise ValueError("Ngưỡng giới hạn đăng nhập phải lớn hơn 0")
        self.familiar_pair_seconds = familiar_pair_seconds
        self._max_tracked_keys = max_tracked_keys
        # Thời điểm đăng nhập thành công gần nhất theo cặp IP + email (thứ tự chèn = cũ→mới).
        self._familiar: dict[str, float] = {}
        self._pair = _SlidingWindowCounter(
            max_attempts=max_attempts, window_seconds=window_seconds, max_keys=max_tracked_keys
        )
        self._account = _SlidingWindowCounter(
            max_attempts=account_max_attempts,
            window_seconds=account_window_seconds,
            max_keys=max_tracked_keys,
        )
        self._ip = _SlidingWindowCounter(
            max_attempts=ip_max_attempts,
            window_seconds=ip_window_seconds,
            max_keys=max_tracked_keys,
        )
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._lock = Lock()

    @staticmethod
    def _keys(client_ip: str, email: str, account_scope: str) -> tuple[str, str, str]:
        address = client_ip.strip()
        account = email.strip().casefold()
        scope = account_scope.strip().casefold()
        return f"{address}:{account}", f"{scope}:{account}", address

    def allow(
        self,
        client_ip: str,
        email: str,
        *,
        account_scope: str = "",
        now: float | None = None,
    ) -> bool:
        timestamp = now if now is not None else monotonic()
        pair_key, account_key, ip_key = self._keys(client_ip, email, account_scope)
        with self._lock:
            account_allowed = self._is_familiar(pair_key, timestamp) or self._account.allow(
                account_key, timestamp
            )
            return (
                self._pair.allow(pair_key, timestamp)
                and account_allowed
                and self._ip.allow(ip_key, timestamp)
            )

    def record_failure(
        self,
        client_ip: str,
        email: str,
        *,
        account_scope: str = "",
        count_account: bool = True,
        now: float | None = None,
    ) -> None:
        """Ghi lỗi; `count_account=False` cho lỗi không chứng minh gì về tài khoản
        (ví dụ token bootstrap sai) để không thành đường khóa tài khoản theo email."""
        timestamp = now if now is not None else monotonic()
        pair_key, account_key, ip_key = self._keys(client_ip, email, account_scope)
        with self._lock:
            self._pair.record(pair_key, timestamp)
            if count_account:
                self._account.record(account_key, timestamp)
            self._ip.record(ip_key, timestamp)

    def record_success(
        self,
        client_ip: str,
        email: str,
        *,
        account_scope: str = "",
        now: float | None = None,
    ) -> None:
        """Xóa bộ đếm lỗi liên tiếp, đánh dấu cặp IP + email quen; giữ bộ đếm IP."""
        timestamp = now if now is not None else monotonic()
        pair_key, account_key, _ = self._keys(client_ip, email, account_scope)
        with self._lock:
            self._pair.reset(pair_key)
            self._account.reset(account_key)
            self._familiar.pop(pair_key, None)
            if len(self._familiar) >= self._max_tracked_keys:
                self._evict_familiar(timestamp)
            self._familiar[pair_key] = timestamp

    def account_failure_count(self, account_scope: str, email: str) -> int:
        """Số lỗi đang tính trong bộ đếm tài khoản (cho test/chẩn đoán, không lộ ra API)."""
        _, account_key, _ = self._keys("", email, account_scope)
        with self._lock:
            return self._account.count(account_key)

    def _is_familiar(self, pair_key: str, now: float) -> bool:
        last_success = self._familiar.get(pair_key)
        if last_success is None:
            return False
        if now - last_success >= self.familiar_pair_seconds:
            del self._familiar[pair_key]
            return False
        return True

    def _evict_familiar(self, now: float) -> None:
        for key, last_success in list(self._familiar.items()):
            if now - last_success >= self.familiar_pair_seconds:
                del self._familiar[key]
        while len(self._familiar) >= self._max_tracked_keys:
            del self._familiar[next(iter(self._familiar))]

    def tracked_key_count(self) -> int:
        with self._lock:
            return len(self._pair) + len(self._account) + len(self._ip)


def verify_bootstrap_token(token_file: Path | None, candidate: str | None) -> bool:
    if token_file is None or not candidate:
        return False
    try:
        expected = token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return bool(expected) and hmac.compare_digest(expected, candidate)
