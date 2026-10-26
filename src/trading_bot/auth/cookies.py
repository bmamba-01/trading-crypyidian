"""Nguồn duy nhất cho tên và thuộc tính cookie phiên admin (SEC-13).

Production (HTTPS) dùng `__Host-crypidian_session` với `Secure`, `Path=/`, không `Domain`;
local/test chạy HTTP nên không `Secure` và vì vậy không được dùng prefix `__Host-`.
Lý do ghi ở `docs/superpowers/plans/2026-10-26-sec-auth-quick-wins-wbs.md` (R4–R7).
"""

from typing import Final

from fastapi import Request, Response

from trading_bot.config.settings import AppSettings

SESSION_COOKIE_BASE_NAME = "crypidian_session"
SESSION_COOKIE_HOST_PREFIX = "__Host-"
SESSION_COOKIE_PATH = "/"
SESSION_COOKIE_SAMESITE: Final = "strict"


def session_cookie_secure(settings: AppSettings) -> bool:
    """Chỉ production chạy sau HTTPS; local/test là HTTP cục bộ."""
    return settings.app_env == "production"


def session_cookie_name(settings: AppSettings) -> str:
    """`__Host-` chỉ hợp lệ khi cookie có `Secure`, nên tên phụ thuộc môi trường."""
    if session_cookie_secure(settings):
        return f"{SESSION_COOKIE_HOST_PREFIX}{SESSION_COOKIE_BASE_NAME}"
    return SESSION_COOKIE_BASE_NAME


def read_session_cookie(request: Request) -> str | None:
    """Chỉ đọc đúng tên của môi trường; production bỏ qua cookie không có prefix."""
    settings: AppSettings = request.app.state.settings
    return request.cookies.get(session_cookie_name(settings))


def set_session_cookie(
    response: Response, settings: AppSettings, value: str, *, max_age: int
) -> None:
    response.set_cookie(
        session_cookie_name(settings),
        value,
        max_age=max_age,
        path=SESSION_COOKIE_PATH,
        secure=session_cookie_secure(settings),
        httponly=True,
        samesite=SESSION_COOKIE_SAMESITE,
    )


def clear_session_cookie(response: Response, settings: AppSettings) -> None:
    response.delete_cookie(
        session_cookie_name(settings),
        path=SESSION_COOKIE_PATH,
        secure=session_cookie_secure(settings),
        httponly=True,
        samesite=SESSION_COOKIE_SAMESITE,
    )
