"""Chính sách mật khẩu và kiểm tra blocklist mật khẩu phổ biến/bị lộ (SEC-15).

Tuân thủ NIST SP 800-63B-4 §3.1.1.2 và OWASP ASVS 5.0 V6.2.2–V6.2.4, V6.2.12:
- Độ dài tối thiểu 14 ký tự, tối đa 128 ký tự (khuyến khích passphrase).
- Không áp đặt luật thành phần ký tự (không bắt buộc chữ hoa/số/ký tự đặc biệt).
- Chặn mật khẩu phổ biến, dễ đoán hoặc từng bị lộ qua ``COMMON_BREACHED_PASSWORDS``.
- Chặn mật khẩu trùng hoặc suy trực tiếp từ email/username/mật khẩu hiện tại.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection

MIN_PASSWORD_LENGTH = 14
MAX_PASSWORD_LENGTH = 128

_SEPARATOR_RE = re.compile(r"[\s\-_.]+")
_TRAILING_DECORATION_RE = re.compile(r"[0-9!@#$%^&*+=~`|\\:;\"'<>,.?/_\-\s]+$")

# Danh sách chuẩn hóa chữ thường các mật khẩu phổ biến / bị lộ dài >= 14 ký tự và mẫu mặc định.
_RAW_COMMON_BREACHED_PASSWORDS: tuple[str, ...] = (
    "password123456",
    "password1234567",
    "password12345678",
    "password123456789",
    "password1234567890",
    "p@ssw0rd123456",
    "p@ssword123456",
    "passw0rd123456",
    "passphrase1234",
    "passphrase12345",
    "12345678901234",
    "123456789012345",
    "1234567890123456",
    "123456789101112",
    "01234567891234",
    "98765432109876",
    "qwertyuiopasdf",
    "qwertyuiop1234",
    "qwertyuiop12345",
    "qwerty12345678",
    "qwerty123456789",
    "qazwsxedcrfvtg",
    "1q2w3e4r5t6y7u",
    "1qaz2wsx3edc4r",
    "zaq12wsxcde34r",
    "abcdefghijklmn",
    "abcdefghijklmno",
    "admin123456789",
    "admin1234567890",
    "administrator1",
    "administrator12",
    "administrator123",
    "administrator2026",
    "letmein1234567",
    "letmein12345678",
    "welcome1234567",
    "welcome12345678",
    "iloveyou123456",
    "iloveyouforever",
    "changeme123456",
    "changemeplease",
    "defaultpassword",
    "defaultpass123",
    "rootpassword12",
    "supersecret123",
    "supersecretpassword",
    "crypidianadmin",
    "crypidian12345",
    "crypidian123456",
    "crypidiantrading",
    "crypidianpassword",
    "binancefutures",
    "binance1234567",
    "tradingbot1234",
    "tradingbot2026",
    "bitcoin1234567",
    "crypto12345678",
    "trustno1234567",
    "monkey12345678",
    "dragon12345678",
    "football123456",
    "sunshine123456",
    "princess123456",
    "master12345678",
    "00000000000000",
    "11111111111111",
    "99999999999999",
)


def normalize_password_for_blocklist(value: str) -> str:
    """Chuẩn hóa NFKC + casefold để so khớp blocklist nhất quán (không biến đổi để lưu hash)."""
    return unicodedata.normalize("NFKC", value).strip().casefold()


def compact_password_for_blocklist(value: str) -> str:
    """Biến thể loại bỏ khoảng trắng và dấu nối để bắt mật khẩu yếu thêm dấu gạch/cách."""
    return _SEPARATOR_RE.sub("", normalize_password_for_blocklist(value))


COMMON_BREACHED_PASSWORDS: frozenset[str] = frozenset(
    compact_password_for_blocklist(item) for item in _RAW_COMMON_BREACHED_PASSWORDS
)


class PasswordPolicyViolation(ValueError):
    """Lỗi vi phạm chính sách mật khẩu với mã lỗi ổn định và thông điệp tiếng Việt."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def is_breached_or_common_password(
    password: str,
    *,
    extra_blocklist: Collection[str] | None = None,
) -> bool:
    """Kiểm tra mật khẩu có khớp danh sách mật khẩu phổ biến/bị lộ đã chuẩn hóa hay không."""
    normalized = normalize_password_for_blocklist(password)
    compact = compact_password_for_blocklist(password)
    if normalized in COMMON_BREACHED_PASSWORDS or compact in COMMON_BREACHED_PASSWORDS:
        return True
    if extra_blocklist:
        extra_normalized = {
            item
            for raw in extra_blocklist
            for item in (
                normalize_password_for_blocklist(raw),
                compact_password_for_blocklist(raw),
            )
            if item
        }
        if normalized in extra_normalized or compact in extra_normalized:
            return True
    return False


def _matches_identifier(
    password: str,
    *,
    email: str | None = None,
    username: str | None = None,
) -> bool:
    norm_pw = normalize_password_for_blocklist(password)
    compact_pw = compact_password_for_blocklist(password)
    stripped_tail = _TRAILING_DECORATION_RE.sub("", norm_pw)
    compact_tail = _SEPARATOR_RE.sub("", stripped_tail)

    if email:
        norm_email = normalize_password_for_blocklist(email)
        compact_email = compact_password_for_blocklist(email)
        if norm_email and (
            norm_pw == norm_email
            or norm_email in norm_pw
            or compact_pw == compact_email
            or compact_email in compact_pw
        ):
            return True
        local_part = norm_email.partition("@")[0]
        compact_local = compact_password_for_blocklist(local_part)
        if len(local_part) >= 4 and (
            norm_pw == local_part
            or compact_pw == compact_local
            or stripped_tail == local_part
            or compact_tail == compact_local
        ):
            return True

    if username:
        norm_user = normalize_password_for_blocklist(username)
        compact_user = compact_password_for_blocklist(username)
        if len(norm_user) >= 3 and (
            norm_pw == norm_user
            or compact_pw == compact_user
            or stripped_tail == norm_user
            or compact_tail == compact_user
            or (len(norm_user) >= 5 and (norm_user in norm_pw or compact_user in compact_pw))
        ):
            return True

    return False


def validate_password_policy(
    password: str,
    *,
    email: str | None = None,
    username: str | None = None,
    current_password: str | None = None,
    extra_blocklist: Collection[str] | None = None,
) -> None:
    """Xác thực mật khẩu theo SEC-15; ném ``PasswordPolicyViolation`` khi không đạt."""
    if not isinstance(password, str) or not password.strip():
        raise PasswordPolicyViolation("PASSWORD_BLANK", "Mật khẩu không được để trống.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyViolation(
            "PASSWORD_TOO_SHORT",
            f"Mật khẩu cần tối thiểu {MIN_PASSWORD_LENGTH} ký tự.",
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyViolation(
            "PASSWORD_TOO_LONG",
            f"Mật khẩu không được vượt quá {MAX_PASSWORD_LENGTH} ký tự.",
        )
    normalized = normalize_password_for_blocklist(password)
    if len(set(normalized)) <= 1:
        raise PasswordPolicyViolation(
            "PASSWORD_REPETITIVE",
            "Mật khẩu không được chỉ lặp lại một ký tự duy nhất.",
        )
    if current_password is not None and password == current_password:
        raise PasswordPolicyViolation(
            "PASSWORD_UNCHANGED",
            "Mật khẩu mới phải khác mật khẩu hiện tại.",
        )
    if _matches_identifier(password, email=email, username=username):
        raise PasswordPolicyViolation(
            "PASSWORD_MATCHES_IDENTIFIER",
            "Mật khẩu không được trùng hoặc dựa trực tiếp trên email/tên đăng nhập.",
        )
    if is_breached_or_common_password(password, extra_blocklist=extra_blocklist):
        raise PasswordPolicyViolation(
            "PASSWORD_BREACHED",
            "Mật khẩu nằm trong danh sách phổ biến hoặc đã bị lộ; hãy chọn passphrase mạnh hơn.",
        )


__all__ = [
    "COMMON_BREACHED_PASSWORDS",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "PasswordPolicyViolation",
    "compact_password_for_blocklist",
    "is_breached_or_common_password",
    "normalize_password_for_blocklist",
    "validate_password_policy",
]
