"""Ô chọn thay ô nhập, multi-select dựng sẵn và công tắc kiểu iOS trên Admin Web.

Kiểm hợp đồng: giá trị cấu hình chọn từ danh sách (select/multi-select), máy chủ nhận nhiều giá trị
của cùng trường chỉ ở các trường được khai báo, công tắc có ngữ nghĩa ``role="switch"`` và thư viện
multi-select tự host (không CDN) đúng phiên bản đã ghi nguồn.
"""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from test_admin_web_views import BOT_ID, WORKSPACE, AuthServiceStub, _client, _resolved

from trading_bot.admin.bot_service import serialize_bot
from trading_bot.admin.learning_paper_performance_router import paper_bot_options
from trading_bot.application.telegram_signals import TelegramSignalFilters
from trading_bot.auth.policy import Principal
from trading_bot.config.bot_settings import BotConfigOverlay

ROOT = Path(__file__).parents[2]
STATIC = ROOT / "src" / "trading_bot" / "static"
TEMPLATES = ROOT / "src" / "trading_bot" / "templates" / "admin"
TOM_SELECT_JS = STATIC / "vendor" / "tom-select-2.6.2.complete.min.js"
TOM_SELECT_CSS = STATIC / "vendor" / "tom-select-2.6.2.min.css"


def _template(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def _signal_client(filters: TelegramSignalFilters) -> object:
    client = _client()
    client.app.state.auth_service = AuthServiceStub()
    client.app.state.telegram_signal_subscription_service = SimpleNamespace(
        get_state=lambda _principal, *, bot_id: _resolved(
            SimpleNamespace(bot_id=bot_id, enabled=True, binding_linked=True, filters=filters)
        )
    )
    return client


def _select_block(html: str, select_id: str) -> str:
    match = re.search(rf'<select id="{select_id}"[^>]*>.*?</select>', html, flags=re.S)
    assert match is not None, f"Không thấy select #{select_id}"
    return match.group(0)


# --- Thư viện multi-select tự host --------------------------------------------------------------


def test_tom_select_is_self_hosted_with_recorded_checksums_and_loaded_by_the_shell() -> None:
    sources = (STATIC / "ASSET-SOURCES.md").read_text(encoding="utf-8")
    for path in (TOM_SELECT_JS, TOM_SELECT_CSS, STATIC / "vendor" / "tom-select-LICENSE.txt"):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert f"`vendor/{path.name}`: `{digest}`" in sources
    assert "Tom Select **2.6.2**" in sources
    assert TOM_SELECT_JS.read_text(encoding="utf-8").startswith("/**\n* Tom Select v2.6.2")
    # Bản CSS nền không có ảnh `data:` (CSP `default-src 'self'` chặn nguồn này).
    assert "data:" not in TOM_SELECT_CSS.read_text(encoding="utf-8")

    base = _template("base.html")
    assert '<link rel="stylesheet" href="/static/vendor/tom-select-2.6.2.min.css">' in base
    assert '<script src="/static/vendor/tom-select-2.6.2.complete.min.js" defer></script>' in base
    assert '<script src="/static/form-controls.js" defer></script>' in base
    assert base.index("tom-select-2.6.2.complete.min.js") < base.index("form-controls.js")


def test_form_controls_script_only_enhances_declared_selects_without_inner_html() -> None:
    script = (STATIC / "form-controls.js").read_text(encoding="utf-8")
    assert "select[data-multiselect]" in script
    assert "select[data-searchable]" in script
    assert "innerHTML" not in script
    assert "remove_button" in script


# --- Công tắc kiểu iOS ---------------------------------------------------------------------------


def test_boolean_controls_render_as_switches() -> None:
    login = _template("login.html")
    assert re.search(r'<input type="checkbox" id="remember-login"[^>]*class="ios-switch"', login)
    assert re.search(r'id="remember-login"[^>]*role="switch"', login)

    sources = _template("research_sources.html")
    assert sources.count('class="ios-switch" role="switch"') >= 2

    for name in ("dashboard.html", "bot_detail.html"):
        html = _template(name)
        assert re.search(
            r'<button id="toggle-realtime-btn"[^>]*role="switch"[^>]*aria-checked="true"', html
        ), name

    base = _template("base.html")
    assert re.search(r'<button id="theme-toggle"[^>]*role="switch"', base)

    css = (STATIC / "admin.css").read_text(encoding="utf-8")
    assert ".ios-switch" in css
    assert '[role="switch"][aria-checked="true"]' in css


def test_realtime_switch_scripts_toggle_aria_checked() -> None:
    for name in ("dashboard-realtime.js", "bot-detail.js"):
        script = (STATIC / name).read_text(encoding="utf-8")
        assert "aria-checked" in script, name
        assert "aria-pressed" not in script, name


def test_telegram_signal_subscription_is_a_switch_form() -> None:
    client = _signal_client(TelegramSignalFilters())
    html = client.get(f"/admin/bots/{BOT_ID}").text  # type: ignore[attr-defined]

    assert re.search(
        r'<button type="submit" name="enabled" value="false"[^>]*role="switch"'
        r'[^>]*aria-checked="true"',
        html,
    )


# --- Bộ lọc tín hiệu: multi-select thay ô nhập chữ ---


def test_signal_filters_are_multi_selects_with_catalog_options_and_saved_values() -> None:
    client = _signal_client(
        TelegramSignalFilters(
            symbols=("ETHUSDT", "ZZZTESTUSDT"),
            timeframes=("4h",),
            directions=("SHORT",),
            strategy_ids=("H03",),
            setup_codes=("H03_LONG_ENTRY",),
        )
    )
    html = client.get(f"/admin/bots/{BOT_ID}").text  # type: ignore[attr-defined]

    assert 'id="signal-filter-symbols"' in html
    assert (
        "<textarea"
        not in html.split("telegram-signal-filters-heading", 1)[1].split("Cấu hình chiến lược", 1)[
            0
        ]
    )

    symbols = _select_block(html, "signal-filter-symbols")
    assert 'name="symbols"' in symbols
    assert " multiple" in symbols
    assert "data-multiselect" in symbols
    assert '<option value="BTCUSDT"' in symbols
    assert re.search(r'<option value="ETHUSDT"[^>]*selected', symbols)
    # Giá trị đã lưu nhưng không có trong danh mục vẫn được giữ, không âm thầm mất.
    assert re.search(r'<option value="ZZZTESTUSDT"[^>]*selected', symbols)

    timeframes = _select_block(html, "signal-filter-timeframes")
    assert re.search(r'<option value="4h"[^>]*selected', timeframes)
    assert '<option value="15m"' in timeframes

    directions = _select_block(html, "signal-filter-directions")
    assert re.search(r'<option value="SHORT"[^>]*selected', directions)
    assert '<option value="LONG"' in directions

    strategies = _select_block(html, "signal-filter-strategies")
    assert re.search(r'<option value="H03"[^>]*selected', strategies)
    assert '<option value="H01"' in strategies

    setups = _select_block(html, "signal-filter-setups")
    assert re.search(r'<option value="H03_LONG_ENTRY"[^>]*selected', setups)
    assert '<option value="H01_SHORT_ENTRY"' in setups


def test_signal_filter_form_accepts_repeated_values_from_multi_selects() -> None:
    client = _client()
    auth = AuthServiceStub()
    saved: list[TelegramSignalFilters] = []

    async def set_filters(
        _principal: Principal, *, bot_id: UUID, filters: TelegramSignalFilters
    ) -> object:
        assert bot_id == BOT_ID
        saved.append(filters)
        return object()

    client.app.state.auth_service = auth
    client.app.state.telegram_signal_subscription_service = SimpleNamespace(set_filters=set_filters)

    response = client.post(
        f"/admin/bots/{BOT_ID}/telegram-signal-filters",
        content=(
            "csrf_token=csrf-token-test-123&symbols=BTCUSDT&symbols=ETHUSDT"
            "&timeframes=1h&timeframes=4h&directions=LONG"
        ),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    assert saved == [
        TelegramSignalFilters(
            symbols=("BTCUSDT", "ETHUSDT"), timeframes=("1h", "4h"), directions=("LONG",)
        )
    ]


def test_fields_outside_the_multi_value_allowlist_still_reject_duplicates() -> None:
    client = _client()
    client.app.state.auth_service = AuthServiceStub()
    response = client.post(
        f"/admin/bots/{BOT_ID}/telegram-signals",
        content="csrf_token=csrf-token-test-123&enabled=true&enabled=false",
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert response.status_code == 422


# --- Cấu hình bot: chọn thay vì gõ JSON ---------------------------------------------------------


def test_bot_config_draft_form_offers_selects_and_keeps_json_as_advanced_option() -> None:
    client = _client()
    client.app.state.auth_service = AuthServiceStub()
    html = client.get(f"/admin/bots/{BOT_ID}").text

    release = _select_block(html, "overlay-strategy-release")
    assert re.search(r'<option value="" selected>Kế thừa cấu hình gốc', release)
    assert '<option value="h03-v2"' in release

    symbols = _select_block(html, "overlay-universe-symbols")
    assert " multiple" in symbols
    assert "data-multiselect" in symbols
    assert '<option value="BTCUSDT"' in symbols

    timeframes = _select_block(html, "overlay-universe-timeframes")
    assert '<option value="1h"' in timeframes

    risk = _select_block(html, "overlay-risk-per_trade_risk_fraction")
    assert '<option value="" selected' in risk
    assert '<option value="0.005"' in risk

    positions = _select_block(html, "overlay-risk-max_positions")
    assert '<option value="3"' in positions

    advanced = html.split('id="overlay-json-advanced"', 1)[1]
    assert '<textarea class="bot-json" id="overlay-json" name="overlay_json"' in advanced
    assert 'name="overlay_source" value="json"' in advanced


def _draft_client() -> tuple[object, list[BotConfigOverlay]]:
    client = _client()
    client.app.state.auth_service = AuthServiceStub()
    created: list[BotConfigOverlay] = []

    async def create_draft(
        _principal: Principal,
        *,
        bot_id: UUID,
        overlay: BotConfigOverlay,
        expected_parent_version_id: UUID | None,
    ) -> object:
        assert bot_id == BOT_ID
        created.append(overlay)
        return object()

    client.app.state.bot_admin_service.create_draft = create_draft
    return client, created


def test_structured_draft_form_builds_overlay_from_selected_values() -> None:
    client, created = _draft_client()
    response = client.post(  # type: ignore[attr-defined]
        f"/admin/bots/{BOT_ID}/config-drafts",
        content=(
            "csrf_token=csrf-token-test-123&expected_parent_version_id="
            "&strategy_release_id=h03-v2&universe_symbols=ETHUSDT&universe_symbols=BTCUSDT"
            "&universe_timeframes=1h&risk_per_trade_risk_fraction=0.005&risk_max_positions=2"
            "&risk_max_notional=&risk_max_leverage="
        ),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    assert created == [
        BotConfigOverlay.model_validate(
            {
                "schema_version": 1,
                "strategy": {"release_id": "h03-v2"},
                "universe": {"symbols": ["ETHUSDT", "BTCUSDT"], "timeframes": ["1h"]},
                "risk": {"per_trade_risk_fraction": "0.005", "max_positions": 2},
            }
        )
    ]


def test_structured_draft_form_with_nothing_selected_inherits_root() -> None:
    client, created = _draft_client()
    response = client.post(  # type: ignore[attr-defined]
        f"/admin/bots/{BOT_ID}/config-drafts",
        data={"csrf_token": "csrf-token-test-123", "strategy_release_id": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    assert created == [BotConfigOverlay.model_validate({"schema_version": 1})]


def test_structured_draft_form_rejects_values_outside_the_offered_choices() -> None:
    client, created = _draft_client()
    response = client.post(  # type: ignore[attr-defined]
        f"/admin/bots/{BOT_ID}/config-drafts",
        data={"csrf_token": "csrf-token-test-123", "risk_max_positions": "abc"},
        follow_redirects=False,
    )

    assert response.status_code == 422
    assert created == []


def test_advanced_json_draft_still_works() -> None:
    client, created = _draft_client()
    response = client.post(  # type: ignore[attr-defined]
        f"/admin/bots/{BOT_ID}/config-drafts",
        data={
            "csrf_token": "csrf-token-test-123",
            "overlay_source": "json",
            "overlay_json": '{"schema_version": 1, "risk": {"max_positions": 1}}',
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    assert created == [
        BotConfigOverlay.model_validate({"schema_version": 1, "risk": {"max_positions": 1}})
    ]


# --- Biểu mẫu khác: chọn giá trị có sẵn ---


def test_bot_create_limit_is_chosen_from_presets() -> None:
    client = _client()
    client.app.state.auth_service = AuthServiceStub()
    html = client.get("/admin/bots/new").text

    limit = _select_block(html, "max_notional")
    assert 'name="max_notional"' in limit
    assert re.search(r'<option value="10000"[^>]*selected', limit)
    assert '<option value="50000"' in limit


def test_backtest_cost_and_risk_parameters_are_selects() -> None:
    html = _template("backtest_replays.html")
    for select_id, name in (
        ("starting-equity", "starting_equity"),
        ("risk-fraction", "per_trade_risk_fraction"),
        ("fee-rate", "fee_rate"),
        ("slippage", "slippage_rate"),
    ):
        assert re.search(rf'<select id="{select_id}" name="{name}"', html), select_id
        assert f'<input id="{select_id}"' not in html
    # Gói dữ liệu chưa có thì khung nến vẫn là danh sách chọn, không phải ô gõ "1h".
    assert '<input id="execution-interval"' not in html


# --- Lỗi đã ghi nhận -----------------------------------------------------------------------------


def test_paper_performance_lists_bots_from_the_real_service_shape() -> None:
    bot = SimpleNamespace(id=BOT_ID, name="Bot A", status="ENABLED")
    paper = SimpleNamespace(
        id=WORKSPACE,
        name="acct",
        mode="PAPER",
        venue="binance_usdm",
        environment="local",
        status="ACTIVE",
        max_notional=Decimal("10000"),
    )
    live = SimpleNamespace(**{**vars(paper), "mode": "LIVE"})
    rows = [
        serialize_bot(bot, paper, None),  # type: ignore[arg-type]
        serialize_bot(SimpleNamespace(id=UUID(int=1), name="Bot B", status="ENABLED"), live, None),  # type: ignore[arg-type]
    ]

    assert paper_bot_options(rows) == ({"id": str(BOT_ID), "name": "Bot A"},)


def test_dashboard_performance_metrics_are_rounded_and_laid_out_as_rows() -> None:
    client = _client()
    client.get("/admin/bots")  # nạp bộ lọc/biến toàn cục của template như khi phục vụ trang
    environment = client.app.state.templates.env
    html = environment.get_template("admin/metric_cards.html").render(
        totals={"USDT": {"net_pnl": "-269.6064", "win_rate": "66.66666667"}},
        report_metrics=(("net_pnl", "Lãi/lỗ ròng", True), ("win_rate", "Tỷ lệ thắng", False)),
        pnl_metric_keys=frozenset({"net_pnl"}),
        rounded=True,
    )
    assert "-269,61 USDT" in html
    assert "66,67" in html
    # Số đầy đủ để đối soát vẫn nằm ở title.
    assert 'title="-269,6064 USDT"' in html
    assert "66,66666667" in html

    css = (STATIC / "admin.css").read_text(encoding="utf-8")
    assert ".asset-report .metric-grid > .metric-item" in css


def test_metric_without_value_does_not_append_asset_suffix() -> None:
    client = _client()
    client.get("/admin/bots")
    html = client.app.state.templates.env.get_template("admin/metric_cards.html").render(
        totals={"USDT": {"average_win": None}},
        report_metrics=(("average_win", "Mức lãi ròng trung bình", True),),
        pnl_metric_keys=frozenset({"average_win"}),
        rounded=True,
    )
    assert "Chưa có dữ liệu USDT" not in html
    assert "Chưa có dữ liệu" in html
