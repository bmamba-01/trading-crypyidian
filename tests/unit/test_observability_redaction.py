"""Unit test cho bộ lọc che giấu bí mật (Log Redaction) và Secret Canary Guard (OPS07A)."""

from __future__ import annotations

from trading_bot.observability.redaction import (
    contains_secret_canary,
    redact_log_record,
    redact_value,
)


def test_redact_value_masks_sensitive_keys_and_bearer_tokens() -> None:
    """Mọi trường chứa api_key, secret, token, cookie hoặc Bearer phải bị che."""
    canary_secret = "CANARY_SECRET_XYZ_998877"
    canary_token = "eyJhbGciOiJIUzI1NiJ9.canary_jwt_payload.sig"
    raw = {
        "workspace_id": "ws-123",
        "binance_api_key": "bnk_live_12345",
        "api_secret": canary_secret,
        "headers": {
            "Authorization": f"Bearer {canary_token}",
            "Cookie": "crypidian_session=secret_cookie_val",
            "X-Request-Id": "req-001",
        },
        "nested": [
            {"password": "my_password_123"},
            {"url": "https://user:pass_secret@db.internal:5432/crypidian"},
        ],
    }

    redacted = redact_value(raw)
    assert redacted["workspace_id"] == "ws-123"
    assert redacted["binance_api_key"] == "[REDACTED]"
    assert redacted["api_secret"] == "[REDACTED]"
    assert redacted["headers"]["Authorization"] == "Bearer [REDACTED]"
    assert redacted["headers"]["Cookie"] == "[REDACTED]"
    assert redacted["headers"]["X-Request-Id"] == "req-001"
    assert redacted["nested"][0]["password"] == "[REDACTED]"
    assert "pass_secret" not in redacted["nested"][1]["url"]
    assert "[REDACTED]" in redacted["nested"][1]["url"]
    assert not contains_secret_canary(redacted, [canary_secret, canary_token, "secret_cookie_val"])


def test_redact_log_record_preserves_correlation_and_blocks_canary() -> None:
    """Structured log giữ correlation_id, tên sự kiện tiếng Anh và không lộ secret."""
    canary = "SUPER_SECRET_BINANCE_KEY_001"
    record = redact_log_record(
        "order_submission_failed",
        {
            "symbol": "BTCUSDT",
            "error": f"Request failed with apiKey={canary}&signature=abc123def",
            "retry_count": 1,
        },
        correlation_id="corr-abc-999",
    )
    assert record["event"] == "order_submission_failed"
    assert record["correlation_id"] == "corr-abc-999"
    assert record["payload"]["symbol"] == "BTCUSDT"
    assert record["payload"]["retry_count"] == 1
    assert canary not in record["payload"]["error"]
    assert "abc123def" not in record["payload"]["error"]
    assert not contains_secret_canary(record, [canary])
