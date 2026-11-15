"""Kiểm thử máy chủ cho UI kit v1 ở nhóm trang Chiến lược và Backtest.

Bao quát các quyết định giao diện không thấy được qua test hiện có:
- Trang nạp CSS riêng `/static/pages/*.css` và script chung `pages/strategy-ui.js`.
- Thẻ mẫu dự phòng (chưa lưu DB) được ghi rõ là giá trị giữ chỗ, không trình bày như dữ liệu
  đã kiểm chứng.
- Bảy popup giải thích tầng L0–L6 có nội dung từ từ điển chú giải và hạn chế đã biết.
- Microscope nhúng vết ban đầu dạng JSON (không thực thi) để một hàm dựng duy nhất hiển thị.
- Danh sách lượt backtest chỉ có một bản DOM cho mỗi lượt.
"""

from __future__ import annotations

import json
import re

from tests.unit.test_admin_backtest_replay_views import _client as backtest_client
from tests.unit.test_admin_strategy_cards_ui import _create_authenticated_client

MICROSCOPE_PATH = "/admin/strategy-cards/SC-BINANCE-USDM-MEAN-REVERT-002/microscope"


def test_strategy_cards_page_uses_page_assets_tabs_and_layer_dialogs() -> None:
    html = _create_authenticated_client().get("/admin/strategy-cards").text

    assert '<link rel="stylesheet" href="/static/pages/strategy.css">' in html
    assert '<script src="/static/pages/strategy-ui.js" defer></script>' in html
    assert 'role="tablist"' in html
    assert 'id="panel-layers"' in html
    for slug in ("l0", "l1", "l2", "l3", "l4", "l5", "l6"):
        assert f'<dialog id="layer-dialog-{slug}" class="glass-dialog' in html
        assert f'data-dialog-open="layer-dialog-{slug}"' in html
    assert html.count("Hạn chế đã biết") >= 7
    assert 'id="card-dialog-1"' in html
    assert 'data-dialog-open="card-dialog-1"' in html


def test_strategy_cards_page_marks_demo_fallback_values_as_placeholders() -> None:
    html = _create_authenticated_client().get("/admin/strategy-cards").text

    assert "thẻ mẫu dự phòng" in html
    assert "Thẻ mẫu · chưa lưu DB" in html
    assert "giữ chỗ" in html
    # Dialog không được nằm trong danh sách role=list (chỉ chứa listitem).
    grid = re.search(
        r'role="list" aria-label="Danh sách các thẻ chiến lược">(.*?)</div>\s*<dialog', html, re.S
    )
    assert grid is not None
    assert "<dialog" not in grid.group(1)


def test_microscope_embeds_initial_trace_as_inert_json_and_renders_layers() -> None:
    html = _create_authenticated_client().get(MICROSCOPE_PATH).text

    match = re.search(
        r'<script id="initial-trace-data" type="application/json">(.*?)</script>', html, re.S
    )
    assert match is not None
    trace = json.loads(match.group(1))
    assert trace["card_id"] == "SC-BINANCE-USDM-MEAN-REVERT-002"
    assert any(item["layer"] == "L3_ALGORITHMS" for item in trace["layer_traces"])
    assert 'id="l3-trace-content"' in html
    assert 'id="pipeline-rail"' in html
    assert "không đọc nến lịch sử" in html
    assert '<script src="/static/microscope-replay.js" defer></script>' in html


def test_backtest_page_renders_one_dom_entry_per_job_with_copy_controls() -> None:
    html = backtest_client().get("/admin/backtests").text

    assert '<link rel="stylesheet" href="/static/pages/backtests.css">' in html
    assert html.count('data-job-id="e647a8d0-775d-4b44-8cbe-6ff2a558ca57"') == 1
    assert "backtest-job-card" not in html
    assert 'data-copy="e647a8d0-775d-4b44-8cbe-6ff2a558ca57"' in html
    assert 'data-percent-hint="risk-fraction-percent"' in html
    assert "data-submit-guard" in html
    # Khớp gói dữ liệu theo tiền tố mã: hiện mã coin của gói thay vì chỉ 12 ký tự hash.
    assert 'data-coin="BTCUSDT"' in html
