"""Header bảo mật HTTP cho mọi response của app (SEC-27).

Giá trị và lý do ghi ở `docs/superpowers/plans/2026-10-26-sec-auth-quick-wins-wbs.md`
(R1–R3), theo OWASP HTTP Headers và REST Security Cheat Sheet (kiểm tra 2026-10-26).
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from trading_bot.config.settings import AppSettings

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; base-uri 'none'; object-src 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
STRICT_TRANSPORT_SECURITY = "max-age=31536000; includeSubDomains"

_BASELINE_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", PERMISSIONS_POLICY),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    # Mặc định không cache: response lỗi/phiên cũng có thể chứa dữ liệu nhạy cảm.
    ("Cache-Control", "no-store"),
)


def security_headers(settings: AppSettings) -> tuple[tuple[str, str], ...]:
    """Trả bộ header theo môi trường; HSTS chỉ khi production (bắt buộc chạy sau HTTPS)."""
    if settings.app_env == "production":
        return (*_BASELINE_HEADERS, ("Strict-Transport-Security", STRICT_TRANSPORT_SECURITY))
    return _BASELINE_HEADERS


class SecurityHeadersMiddleware:
    """ASGI middleware thuần: thêm header nếu route chưa tự đặt, không đọc body."""

    def __init__(self, app: ASGIApp, *, headers: tuple[tuple[str, str], ...]) -> None:
        self.app = app
        self.headers = headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for name, value in self.headers:
                    response_headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)
