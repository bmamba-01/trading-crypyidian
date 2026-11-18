"""Kiểm định hợp đồng mật độ dùng chung và disclosure của các trang Admin."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
TEMPLATES = ROOT / "src/trading_bot/templates/admin"
STYLES = ROOT / "src/trading_bot/static/pages/admin-density.css"


def test_global_density_styles_load_after_page_styles_and_keep_touch_targets() -> None:
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert base.index("{% block head_extra %}") < base.index(
        'href="/static/pages/admin-density.css"'
    )
    css = STYLES.read_text(encoding="utf-8")
    assert "--admin-control-height: 2.25rem" in css
    assert "--admin-control-height-touch: 2.75rem" in css
    assert ".btn--icon" in css
    assert ":focus-visible" in css


def test_long_secondary_panels_have_named_native_disclosures() -> None:
    required = {
        "market_scanner.html": "admin-disclosure--scanner-history",
        "signal_outcomes.html": "admin-disclosure--signal-breakdown",
        "ai_analysis_detail.html": "admin-disclosure--ai-context",
        "run_report.html": "admin-disclosure--report-runs",
        "risk_appetite.html": "admin-disclosure--risk-history",
    }
    for filename, class_name in required.items():
        source = (TEMPLATES / filename).read_text(encoding="utf-8")
        assert re.search(
            rf'<details class="[^"]*\b{re.escape(class_name)}\b[^"]*"', source
        )
        assert "<summary>" in source


def test_repeated_filter_actions_use_named_icon_buttons() -> None:
    checks = {
        "market_scanner.html": "aria-label=\"Xem kết quả quét\"",
        "ai_analyses.html": "aria-label=\"Lọc lượt phân tích\"",
        "signal_outcomes.html": "aria-label=\"Lọc kết quả tín hiệu\"",
        "live.html": "aria-label=\"Áp dụng bộ lọc bot\"",
    }
    for filename, accessible_name in checks.items():
        source = (TEMPLATES / filename).read_text(encoding="utf-8")
        assert "btn--icon\"" in source
        assert accessible_name in source
        assert "icons.action(\"" in source
