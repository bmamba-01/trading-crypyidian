"""Kiểm kê chú giải chỉ số (UI-1): mọi chỉ số hiển thị phải có giải thích tiếng Việt từ một nguồn.

Từ điển `metric_help.HELP` là nguồn duy nhất cho nội dung tooltip. Test này chặn ba lỗi:
chỉ số trong `METRICS` thiếu chú giải, template gọi khóa không tồn tại và nội dung rỗng/giữ chỗ.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from trading_bot.admin import metric_help as metric_help_module
from trading_bot.admin.metric_help import (
    HELP,
    MetricHelp,
    metric_help,
    metric_help_or_none,
    metric_help_title,
)
from trading_bot.admin.presentation import METRICS
from trading_bot.admin.web_router import _templates

TEMPLATE_ROOT = Path("src/trading_bot/templates")
ADMIN_TEMPLATES = sorted((TEMPLATE_ROOT / "admin").glob("*.html"))
GLOSSARY = Path("docs/research/glossary-vi.md")

# Lời gọi macro/hàm trong template có khóa là hằng chuỗi: help_term("khóa", ...).
_HELP_CALL = re.compile(
    r"""\b(?:help_term|help_icon|metric_help_title)\(\s*["']([a-z0-9_]+)["']""",
)
_VIETNAMESE_DIACRITIC = re.compile(
    r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]",
    re.IGNORECASE,
)
_PLACEHOLDERS = ("TODO", "TBD", "FIXME", "XXX", "lorem", "???")

REQUIRED_KEYS = {
    # KPI trang tổng quan
    "bot_count",
    "ready_bot_count",
    "workspace_kill_state",
    "operating_mode",
    # Bot detail
    "open_positions_count",
    "position_symbol_side",
    "position_opened_at",
    "quote_asset",
    "trade_id",
    "trade_closed_at",
    "trade_quantity",
    "trade_entry_price",
    "trade_exit_price",
    # Báo cáo và hậu kiểm
    "risk_reward_ratio",
    "stop_loss",
    "take_profit",
    "leverage",
    "planned_risk_budget",
    "r_multiple",
    "trade_slippage",
    "total_cost",
    "exit_reason",
    "execution_quality",
    "quality_optimal",
    "quality_acceptable",
    "quality_deviant",
    "quality_unverified",
    "daily_pnl_calendar",
    # Biểu đồ
    "candle_ohlc",
    "candle_volume",
    "chart_timeframe",
    "chart_entry_marker",
    "chart_exit_marker",
    "chart_stop_line",
    "chart_target_line",
    "chart_candle_source",
    "equity_curve",
    # Tầng Strategy Card
    "layer_l0_data_hygiene",
    "layer_l1_candle_anatomy",
    "layer_l2_indicators",
    "layer_l3_algorithms",
    "layer_l4_context_gate",
    "layer_l5_trigger",
    "layer_l6_zones",
}


def _github_slug(heading: str) -> str:
    text = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return text.replace(" ", "-")


def _glossary_slugs() -> set[str]:
    return {
        _github_slug(line[3:])
        for line in GLOSSARY.read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    }


def _templates_object() -> Jinja2Templates:
    """Đi qua đúng hàm đăng ký filter/global của web router, không dựng môi trường riêng."""
    app = FastAPI()
    app.state.templates = Jinja2Templates(directory=str(TEMPLATE_ROOT))
    scope: dict[str, Any] = {"type": "http", "app": app, "method": "GET", "path": "/"}
    return _templates(Request(scope))


def test_every_report_metric_has_help() -> None:
    missing = [key for key, _title, _monetary in METRICS if key not in HELP]
    assert missing == [], f"Chỉ số trong METRICS thiếu chú giải: {missing}"
    assert len(METRICS) == 15


def test_required_ui_keys_have_help() -> None:
    assert sorted(REQUIRED_KEYS - HELP.keys()) == []


@pytest.mark.parametrize("key", sorted(HELP))
def test_help_entry_is_complete_vietnamese_text(key: str) -> None:
    entry = HELP[key]
    assert isinstance(entry, MetricHelp)
    for field_name in ("title_vi", "definition_vi", "reading_vi", "limits_vi"):
        value = getattr(entry, field_name)
        assert value.strip(), f"{key}.{field_name} rỗng"
        assert value == value.strip()
    for field_name in ("definition_vi", "limits_vi"):
        assert _VIETNAMESE_DIACRITIC.search(getattr(entry, field_name)), (
            f"{key}.{field_name} phải là tiếng Việt có dấu"
        )
    joined = " ".join(
        (entry.title_vi, entry.definition_vi, entry.formula_vi, entry.reading_vi, entry.limits_vi)
    )
    for placeholder in _PLACEHOLDERS:
        assert placeholder.lower() not in joined.lower(), f"{key} còn chữ giữ chỗ {placeholder}"
    assert re.fullmatch(r"[a-z0-9_]+", key)


def test_glossary_anchors_point_to_existing_sections() -> None:
    slugs = _glossary_slugs()
    broken = {
        key: entry.glossary_anchor
        for key, entry in HELP.items()
        if entry.glossary_anchor is not None and entry.glossary_anchor not in slugs
    }
    assert broken == {}


def test_lookup_is_strict_in_code_and_harmless_in_templates() -> None:
    assert metric_help("net_pnl") is HELP["net_pnl"]
    with pytest.raises(KeyError):
        metric_help("khong_ton_tai")
    assert metric_help_or_none("khong_ton_tai") is None
    assert metric_help_title("net_pnl") == HELP["net_pnl"].title_vi
    assert metric_help_title("khong_ton_tai") == ""


def test_formulas_match_reporting_definitions() -> None:
    """Chốt các điểm dễ viết sai so với `domain/portfolio/reporting.py`."""
    profit_factor = HELP["profit_factor"]
    assert "ròng" in profit_factor.formula_vi
    assert "gộp" not in profit_factor.formula_vi
    assert "không xác định" in profit_factor.limits_vi.lower()
    drawdown = HELP["max_drawdown"]
    assert "đã thực hiện" in drawdown.definition_vi
    assert "mark-to-market" in drawdown.limits_vi
    assert "hòa vốn" in HELP["win_rate_percent"].formula_vi
    assert "Asia/Ho_Chi_Minh" in HELP["daily_pnl_calendar"].formula_vi


def test_template_help_keys_exist_in_dictionary() -> None:
    assert ADMIN_TEMPLATES
    unknown: dict[str, list[str]] = {}
    used: set[str] = set()
    for path in ADMIN_TEMPLATES:
        keys = _HELP_CALL.findall(path.read_text(encoding="utf-8"))
        used.update(keys)
        bad = sorted({key for key in keys if key not in HELP})
        if bad:
            unknown[path.name] = bad
    assert unknown == {}, f"Template gọi khóa chú giải không có trong từ điển: {unknown}"
    assert len(used) >= 40, "Các trang quản trị phải dùng thành phần chú giải chung."


def test_no_legacy_css_only_tooltip_remains() -> None:
    """Tooltip `::after` cũ không nhận focus/chạm; mọi chỗ phải dùng thành phần mới."""
    offenders = [
        path.name for path in ADMIN_TEMPLATES if "data-tooltip=" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_metric_cards_render_accessible_help_for_each_metric() -> None:
    templates = _templates_object()
    values = {key: "1" for key, _title, _monetary in METRICS}
    html = templates.env.get_template("admin/metric_cards.html").render(totals={"USDT": values})

    triggers = re.findall(
        r'<button type="button" class="metric-help"[^>]*aria-describedby="([^"]+)"[^>]*>', html
    )
    assert len(triggers) == len(METRICS)
    assert len(set(triggers)) == len(triggers), "Mỗi tooltip cần id riêng"
    assert html.count('aria-expanded="false"') == len(METRICS)
    for tooltip_id in triggers:
        assert re.search(rf'<span[^>]*role="tooltip"[^>]*id="{re.escape(tooltip_id)}"', html)
    for key, title, _monetary in METRICS:
        assert f'data-metric-help="{key}"' in html
        assert title in html
        entry = HELP[key]
        for text in (entry.definition_vi, entry.limits_vi):
            assert _escaped(text) in html, f"{key}: thiếu nội dung từ điển trong tooltip"


def test_unknown_key_renders_label_without_broken_tooltip() -> None:
    templates = _templates_object()
    source = (
        '{% from "admin/_metric_help.html" import help_term %}'
        '{{ help_term("khong_ton_tai", "Nhãn thử") }}'
    )
    html = templates.env.from_string(source).render()
    assert "Nhãn thử" in html
    assert "<button" not in html
    assert 'role="tooltip"' not in html
    assert 'data-metric-help-missing="khong_ton_tai"' in html


def test_macro_escapes_dictionary_text(monkeypatch: pytest.MonkeyPatch) -> None:
    templates = _templates_object()
    monkeypatch.setitem(
        metric_help_module.HELP,
        "xss_probe",
        MetricHelp(
            title_vi="<b>t</b>",
            definition_vi="<script>alert(1)</script>",
            formula_vi="",
            reading_vi="đọc",
            limits_vi="giới hạn",
        ),
    )
    source = (
        '{% from "admin/_metric_help.html" import help_term %}{{ help_term("xss_probe", "Nhãn") }}'
    )
    html = templates.env.from_string(source).render()
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_base_template_loads_tooltip_script_with_defer() -> None:
    base = (TEMPLATE_ROOT / "admin" / "base.html").read_text(encoding="utf-8")
    assert '<script src="/static/metric-tooltip.js" defer></script>' in base
    script = Path("src/trading_bot/static/metric-tooltip.js").read_text(encoding="utf-8")
    assert "Escape" in script
    assert "aria-expanded" in script


def _escaped(text: str) -> str:
    from markupsafe import escape

    return str(escape(text))
