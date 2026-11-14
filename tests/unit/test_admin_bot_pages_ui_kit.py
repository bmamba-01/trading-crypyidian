"""Nhóm trang Bot theo UI kit v1: danh sách, tạo bot, chi tiết bot.

Kiểm các hợp đồng trình bày: không bịa mã cặp, màu trạng thái theo mã máy, thao tác chỉ hiện
cho vai trò được phép, CSS riêng của trang nạp qua head_extra và trạng thái rỗng trung thực.
"""

from datetime import UTC, datetime
from uuid import UUID

from test_admin_web_views import BOT_ID, WORKSPACE, AuthServiceStub, _client

from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity


def _as_role(client: object, *roles: Role) -> None:
    principal = Principal(
        workspace_id=WORKSPACE,
        membership_id=UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc"),
        user_id=UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57"),
        roles=frozenset(roles),
        mfa_verified_at=datetime.now(UTC),
    )
    client.app.dependency_overrides[current_identity] = lambda: AuthIdentity(  # type: ignore[attr-defined]
        principal=principal, email="member@example.test"
    )


def test_bot_list_renders_scoped_summary_table_and_page_stylesheet() -> None:
    page = _client().get("/admin/bots")

    assert page.status_code == 200
    html = page.text
    assert '<link rel="stylesheet" href="/static/pages/bots.css">' in html
    # Số đếm lấy từ danh sách đã lọc theo quyền (1 bot ENABLED, READY, cho phép vào lệnh).
    assert 'id="bots-total">1<' in html
    assert 'id="bots-enabled">1<' in html
    assert 'id="bots-ready">1<' in html
    assert 'id="bots-entry-allowed">1<' in html
    assert '<span class="badge badge-success bot-badge">Đã bật</span>' in html
    assert f'href="/admin/bots/{BOT_ID}"' in html
    assert 'href="/admin/bots/new"' in html
    # Bot thật không có trường symbol: không được hiển thị mã cặp mặc định.
    assert "BTCUSDT" not in html


def test_bot_list_hides_create_link_for_non_owner_and_shows_empty_state() -> None:
    client = _client()
    _as_role(client, Role.OPERATOR)

    async def no_bots(_principal: object) -> list[dict[str, object]]:
        return []

    client.app.state.bot_admin_service.list_bots = no_bots
    html = client.get("/admin/bots").text

    assert 'href="/admin/bots/new"' not in html
    assert "Chưa có bot được cấp quyền xem." in html
    assert 'id="bots-total">0<' in html


def test_bot_create_page_explains_limits_and_next_steps() -> None:
    client = _client()
    client.app.state.auth_service = AuthServiceStub()
    html = client.get("/admin/bots/new").text

    assert '<label for="name">Tên bot</label>' in html
    assert '<label for="max_notional">Giới hạn giá trị danh nghĩa</label>' in html
    assert 'aria-describedby="max-notional-help"' in html
    assert "trạng thái <strong>Đã tắt</strong>" in html
    assert ">Tạo bot mô phỏng</button>" in html
    assert 'href="/admin/bots" class="btn btn-outline">Hủy</a>' in html


def test_bot_detail_status_tone_follows_machine_code_and_no_fabricated_symbol() -> None:
    client = _client()
    client.app.state.auth_service = AuthServiceStub()
    html = client.get(f"/admin/bots/{BOT_ID}").text

    # ENABLED là trạng thái tốt: không được tô đỏ như bản cũ (nhánh else = danger).
    assert '<span class="badge badge-success bot-badge">Đã bật</span>' in html
    assert 'badge-danger bot-badge">Đã bật' not in html
    # Không có lệnh/vị thế thì mục mã đã giao dịch ghi "Chưa có dữ liệu", không đoán BTCUSDT.
    assert "BTCUSDT" not in html
    assert 'class="kv kv--wide"' in html
    # Hàng nhãn–giá trị giữ nguyên chữ mà E2E Compose dựa vào.
    assert ">Đã bật / Đang hoạt động / Mô phỏng</dd>" in html
    assert 'id="bot-state-summary"' in html
    assert "Trạng thái mở vị thế</dt>" in html
    # ID viết tắt + nút sao chép, giá trị đầy đủ vẫn có trong văn bản.
    assert f'data-copy="{BOT_ID}"' in html
    assert f'<span class="visually-hidden">{BOT_ID}</span>' in html
    # Nhãn trạng thái cho JS cập nhật 5 giây được nhúng dạng JSON (không dùng script nội tuyến).
    assert '<script id="bot-status-labels" type="application/json">' in html
    assert '"control_revision": 4' in html


def test_bot_detail_owner_sees_disable_form_but_operator_does_not() -> None:
    owner = _client()
    owner.app.state.auth_service = AuthServiceStub()
    owner_html = owner.get(f"/admin/bots/{BOT_ID}").text
    assert f'action="/admin/bots/{BOT_ID}/disable"' in owner_html
    assert f'action="/admin/bots/{BOT_ID}/pause"' in owner_html

    operator = _client()
    operator.app.state.auth_service = AuthServiceStub()
    _as_role(operator, Role.OPERATOR)
    operator_html = operator.get(f"/admin/bots/{BOT_ID}").text
    assert f'action="/admin/bots/{BOT_ID}/disable"' not in operator_html
    assert "Chỉ chủ sở hữu (owner) được tắt hoặc bật lại bot." in operator_html
    assert f'action="/admin/bots/{BOT_ID}/pause"' in operator_html
    assert f'action="/admin/bots/{BOT_ID}/resume"' in operator_html


def test_bot_detail_trading_area_uses_tabs_and_keeps_operations_visible() -> None:
    client = _client()
    client.app.state.auth_service = AuthServiceStub()
    html = client.get(f"/admin/bots/{BOT_ID}").text

    assert '<div class="bot-tabs" data-tabs>' in html
    for tab in ("tab-bot-chart", "tab-bot-positions", "tab-bot-trades"):
        assert f'id="{tab}"' in html
    # Panel không đặt hidden sẵn (tabs.js tự ẩn khi chạy), nên không JS vẫn đọc được.
    assert (
        'role="tabpanel" id="panel-bot-trades" aria-labelledby="tab-bot-trades" tabindex="0">'
        in html
    )
    # Khối vận hành nằm ngoài tab để luôn thao tác được sau khi trang tải lại.
    tabs_end = html.index('id="panel-bot-trades"')
    assert html.index('id="bot-control-heading"') > tabs_end
    assert "data-tabs" not in html[html.index('class="bot-ops-grid"') :]
