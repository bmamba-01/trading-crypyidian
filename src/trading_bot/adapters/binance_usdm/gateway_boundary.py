"""Ranh giới bảo mật Host/Key, phân tách bí mật và kiểm tra thời gian cho Binance USDⓈ-M.

Cung cấp các cơ chế fail-closed:
- Chặn tuyệt đối kết nối tới production khi ở chế độ DEMO hoặc PAPER.
- Xác thực allowlist URL và cấu trúc endpoint (loại bỏ path, query injection, userinfo).
- Che giấu và redact toàn bộ API key, Secret key, signature và listenKey trong repr, log.
- Kiểm tra tính hợp lệ của recvWindow và ngăn chặn clock drift lệch quá dung sai.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit


class BinanceExecutionMode(StrEnum):
    """Chế độ thực thi được phân định rõ ràng."""

    DEMO = "DEMO"
    PAPER = "PAPER"
    LIVE = "LIVE"


# --- Danh sách allowlist chính thức của Binance USDⓈ-M Futures ---
ALLOWED_PRODUCTION_REST_HOSTS: frozenset[str] = frozenset({"https://fapi.binance.com"})
ALLOWED_PRODUCTION_WS_HOSTS: frozenset[str] = frozenset({"wss://fstream.binance.com"})

ALLOWED_DEMO_REST_HOSTS: frozenset[str] = frozenset(
    {
        "https://demo-fapi.binance.com",
        "https://testnet.binancefuture.com",
    }
)
ALLOWED_DEMO_WS_HOSTS: frozenset[str] = frozenset(
    {
        "wss://demo-fstream.binance.com",
        "wss://stream.binancefuture.com",
        "wss://fstream.binancefuture.com",
    }
)

LOCAL_MOCK_HOSTNAMES: frozenset[str] = frozenset({"localhost", "127.0.0.1", "mock.local"})

# --- Regex patterns dùng để redact dữ liệu nhạy cảm ---
_SIGNATURE_PATTERN = re.compile(r"signature=[a-fA-F0-9]{32,64}")
_LISTEN_KEY_PATTERN = re.compile(r"listenKey=[a-zA-Z0-9_-]{16,}")
_APIKEY_HEADER_PATTERN = re.compile(r"(X-MBX-APIKEY:\s*)([a-zA-Z0-9_-]{16,})", re.IGNORECASE)
_APIKEY_PARAM_PATTERN = re.compile(r"(apiKey=)([a-zA-Z0-9_-]{16,})", re.IGNORECASE)


# --- Exceptions ---


class GatewayBoundaryError(RuntimeError):
    """Lỗi cơ sở khi vi phạm ranh giới bảo mật gateway; tự động che giấu secret."""

    def __init__(self, message: str) -> None:
        super().__init__(redact_sensitive_text(message))


class HostBoundaryViolationError(GatewayBoundaryError):
    """Lỗi vi phạm host allowlist hoặc cố kết nối production trong chế độ mô phỏng."""


class TimestampBoundaryError(GatewayBoundaryError):
    """Lỗi vi phạm giới hạn tham số thời gian hoặc recvWindow."""


class ClockDriftBoundaryError(TimestampBoundaryError):
    """Lỗi độ lệch đồng hồ giữa client và server sàn vượt quá ngưỡng cho phép."""


class SecretRedactionError(GatewayBoundaryError):
    """Lỗi phát sinh khi xử lý dữ liệu bí mật."""


# --- Kiểu dữ liệu an toàn cho Credential ---


@dataclass(frozen=True, slots=True)
class ApiKeyCredentials:
    """Credentials chứa API key và Secret key; bảo đảm không lộ thô qua log/repr."""

    api_key: str = field(repr=False)
    secret_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("api_key phải là chuỗi không trống.")
        if not isinstance(self.secret_key, str) or not self.secret_key.strip():
            raise ValueError("secret_key phải là chuỗi không trống.")
        if len(self.api_key.strip()) < 8:
            raise ValueError("api_key quá ngắn so với định dạng khóa Binance hợp lệ.")
        if len(self.secret_key.strip()) < 8:
            raise ValueError("secret_key quá ngắn so với định dạng khóa Binance hợp lệ.")

    def masked_api_key(self) -> str:
        """Trả về API key đã che giấu, chỉ để lại 4 ký tự cuối phục vụ audit."""
        clean = self.api_key.strip()
        tail = clean[-4:]
        return f"***{tail}"

    def __repr__(self) -> str:
        return f"ApiKeyCredentials(api_key='{self.masked_api_key()}', secret_key='[REDACTED]')"

    def __str__(self) -> str:
        return self.__repr__()


# --- Tiện ích Redaction ---


def redact_sensitive_text(text: str, extra_secrets: Iterable[str] | None = None) -> str:
    """Tự động thay thế các chữ ký, API keys, listenKeys và bí mật bổ sung."""
    if not text:
        return text

    sanitized = text
    # Thay thế HMAC-SHA256 signature
    sanitized = _SIGNATURE_PATTERN.sub("signature=[REDACTED]", sanitized)
    # Thay thế Listen Key
    sanitized = _LISTEN_KEY_PATTERN.sub("listenKey=[REDACTED]", sanitized)
    # Thay thế API Key header và param
    sanitized = _APIKEY_HEADER_PATTERN.sub(r"\1[REDACTED]", sanitized)
    sanitized = _APIKEY_PARAM_PATTERN.sub(r"\1[REDACTED]", sanitized)

    if extra_secrets:
        for secret in extra_secrets:
            if secret and len(secret) >= 4:
                sanitized = sanitized.replace(secret, "[REDACTED]")

    return sanitized


class SecretRedactingLoggingFilter(logging.Filter):
    """Logging filter loại trừ bí mật khỏi tất cả bản ghi log."""

    def __init__(self, registered_secrets: Iterable[str] | None = None) -> None:
        super().__init__()
        self.registered_secrets = list(registered_secrets or [])

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_sensitive_text(record.msg, self.registered_secrets)
        if record.args:
            sanitized_args: list[object] = []
            for arg in record.args:
                if isinstance(arg, str):
                    sanitized_args.append(redact_sensitive_text(arg, self.registered_secrets))
                else:
                    sanitized_args.append(arg)
            record.args = tuple(sanitized_args)
        return True


# --- Hàm xác thực ranh giới Host & Mode ---


def validate_host_boundary(
    mode: BinanceExecutionMode | str,
    rest_base_url: str,
    ws_base_url: str,
    *,
    allow_local_mock: bool = False,
) -> None:
    """Kiểm tra nghiêm ngặt ranh giới host cho chế độ vận hành (fail-closed)."""
    try:
        execution_mode = BinanceExecutionMode(mode)
    except ValueError:
        raise HostBoundaryViolationError(f"Chế độ thực thi không hợp lệ: {mode}") from None

    if not isinstance(rest_base_url, str) or not rest_base_url.strip():
        raise HostBoundaryViolationError("rest_base_url không được để trống.")
    if not isinstance(ws_base_url, str) or not ws_base_url.strip():
        raise HostBoundaryViolationError("ws_base_url không được để trống.")

    # 1. Phân tích và kiểm tra URL REST
    parsed_rest = urlsplit(rest_base_url)
    if parsed_rest.query or parsed_rest.fragment or parsed_rest.username or parsed_rest.password:
        raise HostBoundaryViolationError(
            "rest_base_url không được chứa query, fragment hoặc userinfo."
        )
    if parsed_rest.path and parsed_rest.path != "/":
        raise HostBoundaryViolationError("rest_base_url không được chứa đường dẫn bổ sung.")

    # 2. Phân tích và kiểm tra URL WebSocket
    parsed_ws = urlsplit(ws_base_url)
    if parsed_ws.query or parsed_ws.fragment or parsed_ws.username or parsed_ws.password:
        raise HostBoundaryViolationError(
            "ws_base_url không được chứa query, fragment hoặc userinfo."
        )
    if parsed_ws.path and parsed_ws.path != "/":
        raise HostBoundaryViolationError("ws_base_url không được chứa đường dẫn bổ sung.")

    normalized_rest = f"{parsed_rest.scheme}://{parsed_rest.netloc}"
    normalized_ws = f"{parsed_ws.scheme}://{parsed_ws.netloc}"

    # 3. QUY TẮC AN TOÀN TỐI CAO: Cấm kết nối production khi ở chế độ DEMO hoặc PAPER
    if execution_mode in (BinanceExecutionMode.DEMO, BinanceExecutionMode.PAPER):
        if (
            normalized_rest in ALLOWED_PRODUCTION_REST_HOSTS
            or parsed_rest.netloc == "fapi.binance.com"
        ):
            raise HostBoundaryViolationError(
                f"Cấm kết nối tới production Binance REST host ({normalized_rest}) "
                f"trong chế độ {execution_mode}."
            )
        if (
            normalized_ws in ALLOWED_PRODUCTION_WS_HOSTS
            or parsed_ws.netloc == "fstream.binance.com"
        ):
            raise HostBoundaryViolationError(
                f"Cấm kết nối tới production Binance WebSocket host ({normalized_ws}) "
                f"trong chế độ {execution_mode}."
            )

    # 4. Kiểm tra mock offline
    is_mock_rest = allow_local_mock and parsed_rest.hostname in LOCAL_MOCK_HOSTNAMES
    is_mock_ws = allow_local_mock and parsed_ws.hostname in LOCAL_MOCK_HOSTNAMES

    # 5. Kiểm tra REST allowlist
    if is_mock_rest:
        if parsed_rest.scheme not in ("http", "https"):
            raise HostBoundaryViolationError("Scheme của REST mock phải là http hoặc https.")
    else:
        if parsed_rest.scheme != "https":
            raise HostBoundaryViolationError("REST host bắt buộc phải dùng scheme https.")
        if execution_mode in (BinanceExecutionMode.DEMO, BinanceExecutionMode.PAPER):
            if normalized_rest not in ALLOWED_DEMO_REST_HOSTS:
                raise HostBoundaryViolationError(
                    f"REST host không thuộc allowlist Demo/Testnet: {normalized_rest}"
                )
        elif execution_mode is BinanceExecutionMode.LIVE:
            if normalized_rest not in ALLOWED_PRODUCTION_REST_HOSTS:
                raise HostBoundaryViolationError(
                    f"REST host không thuộc allowlist Production: {normalized_rest}"
                )

    # 6. Kiểm tra WebSocket allowlist
    if is_mock_ws:
        if parsed_ws.scheme not in ("ws", "wss"):
            raise HostBoundaryViolationError("Scheme của WS mock phải là ws hoặc wss.")
    else:
        if parsed_ws.scheme != "wss":
            raise HostBoundaryViolationError("WebSocket host bắt buộc phải dùng scheme wss.")
        if execution_mode in (BinanceExecutionMode.DEMO, BinanceExecutionMode.PAPER):
            if normalized_ws not in ALLOWED_DEMO_WS_HOSTS:
                raise HostBoundaryViolationError(
                    f"WebSocket host không thuộc allowlist Demo/Testnet: {normalized_ws}"
                )
        elif execution_mode is BinanceExecutionMode.LIVE:
            if normalized_ws not in ALLOWED_PRODUCTION_WS_HOSTS:
                raise HostBoundaryViolationError(
                    f"WebSocket host không thuộc allowlist Production: {normalized_ws}"
                )


# --- Hàm xác thực tham số ký và thời gian ---


def validate_recv_window(recv_window_ms: int) -> None:
    """Xác thực recvWindow nằm trong khoảng hợp lệ 1000ms đến 60000ms theo tài liệu Binance."""
    if not isinstance(recv_window_ms, int) or isinstance(recv_window_ms, bool):
        raise TimestampBoundaryError("recv_window_ms phải là kiểu số nguyên int.")
    if recv_window_ms < 1000 or recv_window_ms > 60_000:
        raise TimestampBoundaryError(
            f"recv_window_ms ({recv_window_ms}) phải nằm trong khoảng 1000ms đến 60000ms."
        )


def validate_timestamp_and_drift(
    request_timestamp_ms: int,
    server_time_ms: int,
    recv_window_ms: int,
    *,
    max_drift_tolerance_ms: int = 1000,
) -> None:
    """Kiểm tra clock skew và hạn sử dụng của timestamp theo quy chuẩn Binance USDⓈ-M."""
    validate_recv_window(recv_window_ms)

    # Đồng hồ máy trạm chạy nhanh hơn đồng hồ sàn vượt ngưỡng dung sai
    if request_timestamp_ms > server_time_ms + max_drift_tolerance_ms:
        drift = request_timestamp_ms - server_time_ms
        raise ClockDriftBoundaryError(
            f"Clock drift vượt ngưỡng: local request_timestamp ({request_timestamp_ms}) "
            f"chạy trước server time ({server_time_ms}) là {drift}ms "
            f"(dung sai tối đa: {max_drift_tolerance_ms}ms)."
        )

    # Request quá cũ nằm ngoài cửa sổ recvWindow
    if server_time_ms - request_timestamp_ms > recv_window_ms:
        age = server_time_ms - request_timestamp_ms
        raise ClockDriftBoundaryError(
            f"Request timestamp ({request_timestamp_ms}) quá cũ so với recvWindow "
            f"({recv_window_ms}ms) và server time ({server_time_ms}); độ trễ: {age}ms."
        )


def sign_hmac_sha256(secret_key: str, query_or_body: str) -> str:
    """Tính chữ ký HMAC-SHA256 theo chuẩn Binance; fail-closed không lộ secret nếu lỗi."""
    try:
        return hmac.new(
            secret_key.encode("utf-8"),
            query_or_body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    except Exception as exc:
        raise GatewayBoundaryError("Lỗi trong quá trình tính chữ ký HMAC-SHA256.") from exc
