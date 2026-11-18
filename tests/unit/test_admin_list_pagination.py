"""Kiểm tra hợp đồng chung popup chi tiết và phân trang danh sách Admin."""

from pathlib import Path

from test_admin_web_views import _client

ROOT = Path(__file__).resolve().parents[2]


def test_admin_base_loads_shared_pagination_controls() -> None:
    html = _client().get("/admin/bots").text

    assert '<script src="/static/admin-pagination.js" defer></script>' in html
    assert "admin-pagination" in html


def test_pagination_starts_at_twenty_and_has_accessible_controls() -> None:
    source = (ROOT / "src/trading_bot/static/admin-pagination.js").read_text(encoding="utf-8")

    assert "DEFAULT_PAGE_SIZE = 20" in source
    assert 'makeButton("Trang trước"' in source
    assert 'makeButton("Trang sau"' in source
    assert 'setAttribute("aria-live", "polite")' in source
    assert 'closest("[data-admin-pagination-nav]")' in source
    assert "window.crypidianPagination" in source


def test_bot_list_has_quick_detail_dialog_and_full_page_link() -> None:
    html = _client().get("/admin/bots").text

    assert 'data-dialog-open="bot-detail-' in html
    assert '<dialog id="bot-detail-' in html
    assert 'href="/admin/bots/' in html


def test_bot_trade_post_mortem_is_available_in_dialog_template() -> None:
    source = (ROOT / "src/trading_bot/templates/admin/bot_detail.html").read_text(encoding="utf-8")

    assert 'data-dialog-open="trade-detail-' in source
    assert '<dialog id="trade-detail-' in source
    assert 'class="post-mortem__reasons"' in source


def test_long_card_and_picker_collections_use_shared_pagination() -> None:
    contracts = {
        "dashboard.html": 'data-pagination-label="Bot đang theo dõi"',
        "learning_model_registry.html": 'data-pagination-label="Phiên bản mô hình"',
        "learning_paper_performance.html": 'data-pagination-label="Báo cáo hiệu năng PAPER"',
    }
    for filename, contract in contracts.items():
        source = (ROOT / "src/trading_bot/templates/admin" / filename).read_text(encoding="utf-8")
        assert "data-admin-pagination-items=" in source
        assert contract in source
