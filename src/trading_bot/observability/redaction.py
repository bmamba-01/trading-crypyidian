"""Bộ lọc che giấu thông tin nhạy cảm (Log Redaction) và kiểm tra rò rỉ canary (OPS07A)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

REDACTED_TOKEN = "[REDACTED]"

_SENSITIVE_KEY_TOKENS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "api_secret",
    "secret",
    "secret_key",
    "password",
    "passwd",
    "token",
    "access_token",
    "refresh_token",
    "csrf_token",
    "cookie",
    "set-cookie",
    "authorization",
    "totp",
    "mfa_secret",
    "private_key",
    "passphrase",
    "signature",
    "binance_secret",
)

# Che mật khẩu trong URL dạng scheme://user:password@host
_URL_CREDENTIAL_RE = re.compile(r"(https?://[^:/\s]+:)([^@/\s]+)(@)")
# Che query/kv nhạy cảm dạng apiKey=..., signature=..., token=...
_KV_SECRET_RE = re.compile(
    r"(?i)\b(api_?key|api_?secret|signature|token|secret|password|passwd)=([^\s&;,\"']+)"
)
# Che Bearer / Basic token nếu xuất hiện trong chuỗi tự do
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+([A-Za-z0-9._\-+/=]+)")


def _is_sensitive_key(key_hint: str | None) -> bool:
    if not key_hint:
        return False
    normalized = key_hint.strip().lower()
    return any(token in normalized for token in _SENSITIVE_KEY_TOKENS)


def _redact_string(text: str, *, key_hint: str | None = None) -> str:
    if key_hint and key_hint.strip().lower() == "authorization":
        stripped = text.strip()
        for scheme in ("Bearer ", "Basic "):
            if stripped.lower().startswith(scheme.lower()):
                return f"{stripped[: len(scheme) - 1]} {REDACTED_TOKEN}"
        return REDACTED_TOKEN

    if _is_sensitive_key(key_hint):
        return REDACTED_TOKEN

    masked = _URL_CREDENTIAL_RE.sub(rf"\1{REDACTED_TOKEN}\3", text)
    masked = _KV_SECRET_RE.sub(rf"\1={REDACTED_TOKEN}", masked)
    masked = _AUTH_SCHEME_RE.sub(rf"\1 {REDACTED_TOKEN}", masked)
    return masked


def redact_value(value: Any, *, key_hint: str | None = None) -> Any:
    """Che đệ quy các trường nhạy cảm trong dict, list hoặc chuỗi tự do."""
    if isinstance(value, Mapping):
        return {str(k): redact_value(v, key_hint=str(k)) for k, v in value.items()}
    if isinstance(value, str):
        return _redact_string(value, key_hint=key_hint)
    if isinstance(value, (bytes, bytearray)):
        return REDACTED_TOKEN if _is_sensitive_key(key_hint) else value
    if isinstance(value, Sequence):
        return [redact_value(item, key_hint=key_hint) for item in value]
    if _is_sensitive_key(key_hint) and value is not None:
        return REDACTED_TOKEN
    return value


def redact_log_record(
    event: str,
    payload: Mapping[str, Any],
    *,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Tạo bản ghi log có cấu trúc, giữ correlation_id và che toàn bộ dữ liệu nhạy cảm."""
    return {
        "event": event,
        "correlation_id": correlation_id,
        "payload": redact_value(payload),
    }


def contains_secret_canary(data: Any, canaries: Iterable[str]) -> bool:
    """Kiểm tra đệ quy xem có bất kỳ chuỗi canary bí mật nào bị rò rỉ trong dữ liệu đầu ra không."""
    active_canaries = [c for c in canaries if c]
    if not active_canaries:
        return False

    def _walk(node: Any) -> bool:
        if isinstance(node, str):
            return any(canary in node for canary in active_canaries)
        if isinstance(node, Mapping):
            return any(_walk(k) or _walk(v) for k, v in node.items())
        if isinstance(node, Sequence) and not isinstance(node, (bytes, bytearray)):
            return any(_walk(item) for item in node)
        return False

    return _walk(data)
