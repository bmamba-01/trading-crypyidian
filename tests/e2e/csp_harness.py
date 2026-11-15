"""Tiện ích cho Playwright chạy trên app dựng trong tiến trình với đúng header bảo mật thật.

App thật luôn gắn `SecurityHeadersMiddleware`; nếu app test bỏ qua middleware này thì script
hoặc style nội tuyến vẫn chạy trong test nhưng bị trình duyệt chặn khi triển khai.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from playwright.sync_api import ConsoleMessage, Page

from trading_bot.common.http_security import SecurityHeadersMiddleware, security_headers
from trading_bot.config.settings import AppSettings

_CSP_MARKERS = ("content security policy", "content-security-policy")


def install_security_headers(app: FastAPI) -> None:
    """Gắn bộ header (gồm CSP) giống cấu hình của `create_app`."""
    app.add_middleware(
        SecurityHeadersMiddleware, headers=security_headers(AppSettings(app_env="local"))
    )


_VIOLATIONS: list[str] = []


def watch_browser_violations(page: Page) -> None:
    """Thu vi phạm CSP và lỗi JavaScript chưa bắt của một trang vào sổ chung của test."""

    def on_console(message: ConsoleMessage) -> None:
        text = message.text
        if any(marker in text.lower() for marker in _CSP_MARKERS):
            _VIOLATIONS.append(f"console: {text}")

    page.on("console", on_console)
    page.on("pageerror", lambda error: _VIOLATIONS.append(f"pageerror: {error}"))


@pytest.fixture
def browser_violations() -> Iterator[list[str]]:
    """Mỗi test bắt đầu với sổ rỗng và thất bại nếu trình duyệt báo vi phạm CSP/lỗi script."""
    _VIOLATIONS.clear()
    yield _VIOLATIONS
    assert _VIOLATIONS == [], f"Trình duyệt báo vi phạm CSP hoặc lỗi JavaScript: {_VIOLATIONS[:6]}"
