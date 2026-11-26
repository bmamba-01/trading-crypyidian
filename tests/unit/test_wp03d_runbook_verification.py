"""Unit test xác minh độ đầy đủ & an toàn của runbook WP-03D (CRY-295)."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK_PATH = REPO_ROOT / "docs/operations/wp03d-venue-execution-runbook.md"


def test_wp03d_runbook_covers_all_required_scenarios_and_safety_rules() -> None:
    """Xác minh runbook WP-03D đủ 6 kịch bản Task 7 bước 7, phân loại 503 và không lộ secret."""
    text = RUNBOOK_PATH.read_text(encoding="utf-8")

    # 1. Đủ 6 kịch bản vận hành bắt buộc
    for idx in range(1, 7):
        assert f"Kịch bản {idx}" in text, f"Thiếu Kịch bản {idx} trong runbook WP-03D"

    # 2. Phân loại HTTP 429, 418 và 503 (Unknown error vs Service Unavailable/Internal error)
    assert "429" in text
    assert "418" in text
    assert "503" in text
    assert "Unknown error" in text
    assert "Service Unavailable" in text
    assert "Internal error" in text

    # 3. Liên kết đầy đủ các reason code và cổng readiness của WP-03D
    for code in (
        "STARTUP_RECONCILIATION_REQUIRED",
        "STREAM_GAP_UNCLOSABLE",
        "EXTERNAL_ACTIVITY_UNRECONCILED",
        "VENUE_PROTECTION_MISSING",
        "VENUE_POSITION_QUANTITY_MISMATCH",
        "ORDER_ABSENT",
        "OPEN_EXPOSURE_RECORDED",
        "EXTERNAL_STATE_REMOVED_BY_OPERATOR",
    ):
        assert code in text, f"Thiếu reason code {code} trong runbook WP-03D"

    # 4. Nêu rõ các nguyên tắc bất biến: không tự adopt/hủy/flatten, cap breach không tự flatten
    assert "không tự adopt" in text.lower()
    assert "không tự flatten" in text.lower()
    assert "listenKey" in text

    # 5. Không chứa secret/private key
    assert "BEGIN PRIVATE KEY" not in text
    assert "BEGIN RSA PRIVATE KEY" not in text
    assert not re.search(r"signature=[a-fA-F0-9]{32,64}", text)
