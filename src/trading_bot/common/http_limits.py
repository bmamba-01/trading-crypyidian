"""Giới hạn tầng HTTP: IP client an toàn, rate limit chung và kích thước body (SEC-29, SEC-30).

Tham chiếu tài liệu (kiểm tra 2026-11-18):
- Starlette middleware (`TrustedHostMiddleware`): https://starlette.dev/middleware/
- Uvicorn settings (`--forwarded-allow-ips`, `--limit-concurrency`): https://uvicorn.dev/settings/
- Caddy reverse_proxy & request_body: https://caddyserver.com/docs/caddyfile/directives/reverse_proxy
- OWASP API4:2023 (Unrestricted Resource Consumption), ASVS V2.4.1, V12.2.1, V15.2.2, V15.3.4.

Ghi chú thiết kế:
- Starlette `TrustedHostMiddleware` chặn Host giả (400); lớp này cung cấp hàm parse/validate
  `allowed_hosts` trước khi mount để không lỗi thầm lặng lúc khởi động.
- `X-Forwarded-For` chỉ được tin khi peer TCP trực tiếp nằm trong `trusted_proxy_ips`. Chuỗi
  được duyệt từ phải sang trái, bỏ qua các proxy tin cậy; hop không tin cậy đầu tiên là client.
  Mục bên trái (do client tự chèn) không bao giờ được dùng khi còn hop tin cậy bên phải.
- Rate limit trong RAM của một process (cửa sổ trượt, bộ nhớ bị chặn trên); không chia sẻ giữa
  replica. Limiter bền vững/phân tán để MB-07/MB-09.
"""

from __future__ import annotations

import ipaddress
import math
from collections import deque
from collections.abc import Sequence
from hashlib import sha256
from http.cookies import SimpleCookie
from threading import Lock
from time import monotonic

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

UNKNOWN_CLIENT = "unknown"
MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
READ_METHODS = frozenset({"GET", "HEAD"})
# Telegram tự xác thực bằng secret header và gửi theo đợt từ dải IP của họ.
DEFAULT_EXEMPT_PATHS = ("/api/v1/telegram/webhook",)


def parse_allowed_hosts(value: str) -> tuple[str, ...]:
    """Phân tích danh sách Host hợp lệ (CSV); kiểm tra định dạng hợp lệ cho TrustedHost."""
    hosts: list[str] = []
    for raw in value.split(","):
        item = raw.strip().lower()
        if not item:
            continue
        if any(char.isspace() for char in item) or "/" in item or "://" in item:
            raise ValueError(f"allowed_hosts chứa giá trị không hợp lệ: {item!r}")
        if "*" in item:
            if item != "*" and (not item.startswith("*.") or "*" in item[2:] or len(item) <= 2):
                raise ValueError(f"allowed_hosts chứa mẫu wildcard không hợp lệ: {item!r}")
        hosts.append(item)
    return tuple(hosts)


def parse_trusted_proxies(value: str) -> tuple[IPNetwork, ...]:
    """Phân tích danh sách IP/CIDR phân tách bằng dấu phẩy; từ chối wildcard và `/0`."""
    networks: list[IPNetwork] = []
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        if item == "*":
            raise ValueError("trusted_proxy_ips không được dùng '*'; liệt kê IP/CIDR của proxy")
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError as error:
            raise ValueError(f"trusted_proxy_ips chứa giá trị không hợp lệ: {item!r}") from error
        if network.prefixlen == 0:
            raise ValueError("trusted_proxy_ips không được tin toàn bộ Internet (/0)")
        networks.append(network)
    return tuple(networks)


def _parse_ip(value: str) -> IPAddress | None:
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _is_trusted(address: IPAddress, trusted: Sequence[IPNetwork]) -> bool:
    return any(address in network for network in trusted)


def resolve_client_ip(
    peer: str | None, forwarded_for: str | None, trusted: Sequence[IPNetwork]
) -> str:
    """Trả IP client đã chuẩn hóa; chỉ tin `X-Forwarded-For` khi peer là proxy tin cậy."""
    peer_address = _parse_ip(peer) if peer else None
    if peer_address is None:
        return peer.strip() if peer and peer.strip() else UNKNOWN_CLIENT
    if not trusted or not forwarded_for or not _is_trusted(peer_address, trusted):
        return str(peer_address)
    # Duyệt từ phải (hop gần nhất) sang trái; dừng ở hop không tin cậy đầu tiên.
    for entry in reversed(forwarded_for.split(",")):
        hop = _parse_ip(entry)
        if hop is None:
            # Chuỗi hỏng: không đoán, quy về proxy để mọi client dùng chung một bộ đếm.
            return str(peer_address)
        if not _is_trusted(hop, trusted):
            return str(hop)
    # Mọi hop đều là proxy tin cậy: không có client công khai để quy kết.
    return str(peer_address)


def _header(scope: Scope, name: bytes) -> str | None:
    values = [value for key, value in scope.get("headers", []) if key == name]
    if not values:
        return None
    return ",".join(value.decode("latin-1") for value in values)


class ClientIPMiddleware:
    """Ghi IP client đã xác thực vào `scope["client"]` để mọi lớp phía trong dùng chung."""

    def __init__(self, app: ASGIApp, *, trusted_proxies: Sequence[IPNetwork]) -> None:
        self.app = app
        self.trusted_proxies = tuple(trusted_proxies)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            client = scope.get("client")
            peer = client[0] if client else None
            port = client[1] if client else 0
            resolved = resolve_client_ip(
                peer, _header(scope, b"x-forwarded-for"), self.trusted_proxies
            )
            scope = {**scope, "client": (resolved, port)}
        await self.app(scope, receive, send)


class SlidingWindowRateLimiter:
    """Cửa sổ trượt theo khóa; lần bị từ chối không được ghi nhận (không kéo dài khóa)."""

    def __init__(self, *, max_requests: int, window_seconds: int, max_keys: int = 10_000) -> None:
        if max_requests < 1 or window_seconds < 1 or max_keys < 1:
            raise ValueError("Ngưỡng rate limit phải lớn hơn 0")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def hit(self, key: str, *, now: float | None = None) -> int | None:
        """Ghi một request; trả `None` nếu cho phép, ngược lại số giây `Retry-After` (>= 1)."""
        timestamp = now if now is not None else monotonic()
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= self.max_keys:
                    self._evict(timestamp)
                hits = self._hits[key] = deque()
            while hits and timestamp - hits[0] >= self.window_seconds:
                hits.popleft()
            if len(hits) >= self.max_requests:
                return max(1, math.ceil(hits[0] + self.window_seconds - timestamp))
            hits.append(timestamp)
            return None

    def _evict(self, now: float) -> None:
        expired = [
            key
            for key, hits in self._hits.items()
            if not hits or now - hits[-1] >= self.window_seconds
        ]
        for key in expired:
            del self._hits[key]
        while len(self._hits) >= self.max_keys:
            del self._hits[next(iter(self._hits))]

    def __len__(self) -> int:
        return len(self._hits)


def _session_fingerprint(scope: Scope, cookie_names: Sequence[str]) -> str | None:
    raw = _header(scope, b"cookie")
    if not raw:
        return None
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:  # noqa: BLE001 - Cookie hỏng chỉ có nghĩa là không có khóa phiên.
        return None
    for name in cookie_names:
        morsel = jar.get(name)
        if morsel is not None and morsel.value:
            return sha256(morsel.value.encode()).hexdigest()[:32]
    return None


def _json_error(
    status_code: int, detail: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code, headers=headers)


class RequestRateLimitMiddleware:
    """Rate limit chung theo IP và theo phiên cho mutation và đọc nặng (429 + Retry-After)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        mutation_per_window: int,
        heavy_read_per_window: int,
        heavy_read_paths: Sequence[str] = (),
        exempt_paths: Sequence[str] = DEFAULT_EXEMPT_PATHS,
        session_cookie_names: Sequence[str] = (),
        window_seconds: int = 60,
        max_keys: int = 10_000,
    ) -> None:
        self.app = app
        self.heavy_read_paths = tuple(heavy_read_paths)
        self.exempt_paths = tuple(exempt_paths)
        self.session_cookie_names = tuple(session_cookie_names)
        self._limiters = {
            "mutation": SlidingWindowRateLimiter(
                max_requests=mutation_per_window, window_seconds=window_seconds, max_keys=max_keys
            ),
            "heavy": SlidingWindowRateLimiter(
                max_requests=heavy_read_per_window, window_seconds=window_seconds, max_keys=max_keys
            ),
        }

    def _bucket(self, scope: Scope) -> str | None:
        path = str(scope.get("path", ""))
        if path in self.exempt_paths:
            return None
        method = str(scope.get("method", "")).upper()
        if method in MUTATION_METHODS:
            return "mutation"
        if method in READ_METHODS and any(
            path.startswith(prefix) for prefix in self.heavy_read_paths
        ):
            return "heavy"
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        bucket = self._bucket(scope) if scope["type"] == "http" else None
        if bucket is None:
            await self.app(scope, receive, send)
            return
        limiter = self._limiters[bucket]
        client = scope.get("client")
        keys = [f"{bucket}:ip:{client[0] if client else UNKNOWN_CLIENT}"]
        session = _session_fingerprint(scope, self.session_cookie_names)
        if session is not None:
            keys.append(f"{bucket}:session:{session}")
        for key in keys:
            retry_after = limiter.hit(key)
            if retry_after is not None:
                response = _json_error(
                    429, "Quá nhiều yêu cầu. Thử lại sau.", {"Retry-After": str(retry_after)}
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class _BodyTooLargeError(Exception):
    """Báo hiệu body vượt ngưỡng khi đọc luồng không có Content-Length."""


class BodySizeLimitMiddleware:
    """Trả 413 khi body vượt ngưỡng: kiểm Content-Length trước, đếm byte khi stream."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("Ngưỡng kích thước body phải lớn hơn 0")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = _header(scope, b"content-length")
        if declared is not None:
            try:
                too_large = any(int(part.strip()) > self.max_bytes for part in declared.split(","))
            except ValueError:
                too_large = False
            if too_large:
                await self._reject(scope, receive, send)
                return
        received = 0
        response_started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLargeError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLargeError:
            if response_started:
                raise
            await self._reject(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = _json_error(
            413,
            "Nội dung yêu cầu vượt giới hạn cho phép.",
            {"Connection": "close"},
        )
        await response(scope, receive, send)
