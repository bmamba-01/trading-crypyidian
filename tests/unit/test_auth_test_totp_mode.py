"""Cờ kiểm thử mã TOTP cố định: chỉ bật được ở local/test + PAPER, mã thật vẫn hợp lệ."""

from __future__ import annotations

from datetime import UTC, datetime

import pyotp
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from trading_bot.app import create_app
from trading_bot.auth.security import TEST_TOTP_CODE, TestModeTotpManager, TotpManager
from trading_bot.config.settings import AppSettings

NOW = datetime(2026, 11, 14, 9, 0, 15, tzinfo=UTC)


def test_test_mode_accepts_fixed_code_and_still_accepts_real_codes() -> None:
    secret = pyotp.random_base32()
    manager = TestModeTotpManager()

    assert TEST_TOTP_CODE == "123456"
    # Mã cố định ứng với chu kỳ hiện hành, nên chống dùng lại theo counter vẫn áp dụng.
    assert manager.matching_counter(secret, TEST_TOTP_CODE, NOW, valid_window=1) == (
        int(NOW.timestamp()) // 30
    )
    real = pyotp.TOTP(secret).at(NOW)
    assert manager.matching_counter(secret, real, NOW, valid_window=1) == int(NOW.timestamp()) // 30
    assert manager.matching_counter(secret, "654321x", NOW, valid_window=1) is None


def test_default_manager_never_accepts_the_fixed_code_without_the_flag() -> None:
    secret = pyotp.random_base32()
    while pyotp.TOTP(secret).at(NOW) == TEST_TOTP_CODE:  # pragma: no cover - xác suất 1e-6
        secret = pyotp.random_base32()

    assert TotpManager().matching_counter(secret, TEST_TOTP_CODE, NOW, valid_window=1) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"app_env": "production"},
        {"trading_mode": "DEMO"},
    ],
)
def test_flag_is_rejected_outside_local_paper(overrides: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="kiểm thử mã xác thực"):
        AppSettings(auth_test_totp_enabled=True, **overrides)


def test_flag_defaults_off_and_login_page_shows_warning_and_prefill_only_when_on() -> None:
    assert AppSettings(app_env="test").auth_test_totp_enabled is False

    normal = TestClient(create_app(AppSettings(app_env="test"))).get("/admin/login")
    flagged = TestClient(create_app(AppSettings(app_env="test", auth_test_totp_enabled=True))).get(
        "/admin/login"
    )

    assert "data-test-totp" not in normal.text
    assert 'value="123456"' not in normal.text
    assert "Chế độ kiểm thử" in flagged.text
    assert 'value="123456"' in flagged.text
    assert "data-test-totp" in flagged.text
