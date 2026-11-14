"""Render nhóm trang Vận hành (flatten-all, Telegram, trang lỗi) theo UI kit v1.

Kiểm cấu trúc giao diện mà E2E và người vận hành dựa vào: chỉ báo bước, cảnh báo không hoàn tác,
bảng mục bị ảnh hưởng, vùng trạng thái thao tác chỉ chứa trạng thái tổng, deep link chỉ nằm trong
`href`, và gợi ý bước tiếp theo trên trang lỗi. Dữ liệu là stub cục bộ, không gọi service thật.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings

WORKSPACE = UUID("1b6f7d3e-5c55-4e2a-9b55-2f3c1b0d9e11")
MEMBERSHIP = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")
OTHER_MEMBERSHIP = UUID("c0ffee00-0000-4000-8000-000000000002")
BOT_ID = UUID("5d7c2a10-0000-4000-8000-000000000001")
OPERATION_ID = UUID("9a000000-0000-0000-0000-000000000001")
CSRF = "csrf-token-ops-123"


class _Auth:
    async def authenticate(self, _cookie: str | None) -> AuthIdentity:
        """Chỉ dùng khi không override identity: mô phỏng phiên hết hạn."""
        from trading_bot.auth.service import AuthenticationFailure

        raise AuthenticationFailure(401, "Phiên đã hết hạn")

    async def issue_csrf_token(self, _cookie: str | None) -> str:
        return CSRF

    async def verify_csrf(self, _cookie: str | None, token: str | None) -> None:
        if token != CSRF:
            from trading_bot.auth.service import AuthenticationFailure

            raise AuthenticationFailure(403, "Thiếu mã CSRF")


class _FlattenPrincipal:
    def __init__(self, targets: tuple[dict[str, str], ...], operation: dict[str, Any]) -> None:
        self._targets = targets
        self._operation = operation

    async def create_preview(self, reason: str) -> object:
        return SimpleNamespace(
            preview_id=UUID("8a000000-0000-0000-0000-000000000001"),
            preview_hash="c" * 64,
            snapshot_hash="d" * 64,
            expires_at=datetime.now(UTC) + timedelta(seconds=90),
            bot_count=1,
            position_count=sum(t["target_type"] == "POSITION" for t in self._targets),
            open_order_count=sum(t["target_type"] == "OPEN_ORDER" for t in self._targets),
            targets=self._targets,
        )

    async def get_operation(self, operation_id: UUID) -> dict[str, Any]:
        return {"id": str(operation_id), **self._operation}


class _Flatten:
    def __init__(self, principal: _FlattenPrincipal) -> None:
        self._principal = principal

    def for_principal(self, _principal: Principal) -> _FlattenPrincipal:
        return self._principal


class _Telegram:
    def __init__(self, bindings: list[dict[str, str]]) -> None:
        self._bindings = bindings

    async def list_bindings(self, _principal: Principal) -> list[dict[str, str]]:
        return self._bindings

    async def create_link(self, _principal: Principal, *, reason: str) -> object:
        return SimpleNamespace(
            url="https://t.me/crypidian_test_bot?start=" + "Z" * 43,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )


def _client(
    *,
    targets: tuple[dict[str, str], ...] = (),
    operation: dict[str, Any] | None = None,
    bindings: list[dict[str, str]] | None = None,
    override_identity: bool = True,
) -> TestClient:
    app = create_app(AppSettings(app_env="test"))
    if override_identity:
        principal = Principal(
            workspace_id=WORKSPACE,
            membership_id=MEMBERSHIP,
            user_id=UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57"),
            roles=frozenset({Role.OWNER}),
            mfa_verified_at=datetime(2026, 11, 14, 6, 13, tzinfo=UTC),
        )
        app.dependency_overrides[current_identity] = lambda: AuthIdentity(
            principal=principal, email="owner@example.test"
        )
    app.state.auth_service = _Auth()
    app.state.flatten_all_service = _Flatten(
        _FlattenPrincipal(targets, operation or {"status": "QUEUED", "items": []})
    )
    app.state.telegram_binding_service = _Telegram(bindings or [])
    return TestClient(app)


def _main(html: str) -> str:
    match = re.search(r"<main\b.*?</main>", html, re.DOTALL)
    assert match is not None
    return match.group(0)


def _target(**overrides: str) -> dict[str, str]:
    target = {
        "bot_id": str(BOT_ID),
        "target_type": "POSITION",
        "target_id": "position-btc-0001",
        "symbol": "BTCUSDT",
        "status": "OPEN",
        "quantity": "0.003",
        "mark_price": "65432.105",
        "error_code": "",
    }
    target.update(overrides)
    return target


def test_flatten_start_page_shows_step_one_irreversible_warning_and_mfa_window() -> None:
    page = _client().get("/admin/flatten")

    assert page.status_code == 200
    html = page.text
    assert '<link rel="stylesheet" href="/static/pages/ops.css">' in html
    assert '<script src="/static/pages/ops.js" defer></script>' in html
    assert html.count('aria-current="step"') == 1
    assert re.search(r'ops-step--current" aria-current="step">\s*<span[^>]*>1<', html)
    assert "Thao tác không hoàn tác" in html
    assert '<label for="flatten-reason">Lý do</label>' in html
    # Cửa sổ MFA lấy từ principal thật, không bịa thời điểm.
    assert 'data-ops-mfa-at="2026-11-14T06:13:00+00:00"' in html
    assert ">14/11 13:13<" in html
    assert len(re.findall(r"<button\b[^>]*type=\"submit\"", html)) == 1


def test_flatten_preview_renders_single_table_with_coin_chips_and_precise_quantity() -> None:
    blocked = _target(
        target_type="OPEN_ORDER",
        target_id="order-eth-0002",
        symbol="ETHUSDT",
        quantity="1.5",
        mark_price="",
        error_code="FLATTEN_MARK_STALE",
    )
    client = _client(targets=(_target(), blocked))

    response = client.post("/admin/flatten/preview", data={"csrf_token": CSRF, "reason": "đóng"})

    assert response.status_code == 200
    html = response.text
    assert html.count("<table") == 1
    assert "<caption>Các mục trong ảnh chụp trạng thái</caption>" in html
    assert 'data-coin="BTCUSDT"' in html
    assert 'data-coin="ETHUSDT"' in html
    # Giá đánh dấu gọn 2 chữ số, bản đầy đủ ở title; số lượng giữ nguyên độ chính xác.
    assert '<span title="65432.105">65.432,11</span>' in html
    assert ">0,003<" in html
    assert "Chưa có dữ liệu" in html
    assert "FLATTEN_MARK_STALE" in html
    assert "1 mục bị chặn" in html
    assert 'data-ops-countdown="' in html
    assert 'name="idempotency_key"' in html
    assert re.search(r'<label for="confirm-reason">Lý do xác nhận</label>', html)
    assert re.search(r'class="btn btn-danger">\s*<svg[^>]*>.*?</svg>Xác nhận đóng toàn bộ', html)
    assert '<script src="/static/coins.js" defer></script>' in html
    assert re.search(r'ops-step--current" aria-current="step">\s*<span[^>]*>2<', html)


def test_flatten_preview_without_targets_explains_pause_only() -> None:
    response = _client().post("/admin/flatten/preview", data={"csrf_token": CSRF, "reason": "x"})

    assert response.status_code == 200
    assert "Không có vị thế hoặc lệnh mở trong ảnh chụp trạng thái." in response.text
    assert "thao tác chỉ được ghi nhận và tạm dừng bot" in response.text


def _operation_status(html: str) -> str:
    match = re.search(r'<p id="operation-status"[^>]*>(.*?)</p>', html, re.DOTALL)
    assert match is not None
    return re.sub(r"<[^>]+>", "", match.group(1))


def test_flatten_operation_status_region_holds_only_aggregate_status_and_progress() -> None:
    items = [
        {"symbol": "BTCUSDT", "target_type": "POSITION", "quantity": "0.003", "status": s}
        | {"error_code": "FLATTEN_MARK_STALE" if s == "BLOCKED" else None}
        for s in ("COMPLETED", "COMPLETED", "BLOCKED")
    ]
    client = _client(
        operation={"status": "PARTIAL", "created_at": "2026-11-14T06:20:00+00:00", "items": items}
    )

    response = client.get(f"/admin/flatten/operations/{OPERATION_ID}")

    assert response.status_code == 200
    html = response.text
    # E2E đọc #operation-status và coi "Bị chặn" trong đó là lỗi: chỉ được chứa trạng thái tổng.
    status = _operation_status(html)
    assert "Hoàn tất một phần" in status
    assert "Bị chặn" not in status
    assert '<progress class="ops-progress__bar" max="3" value="2"' in html
    assert "2/3 mục đã hoàn tất" in html
    # PARTIAL không còn mục PENDING/PROCESSING là đã kết thúc: không tự cập nhật nữa.
    assert 'data-ops-terminal="true"' in html
    assert "data-ops-poll-note" not in html
    assert "1 mục bị chặn chưa được đóng" in html
    assert str(OPERATION_ID) in html
    assert ">14/11 13:20<" in html


def test_flatten_operation_in_progress_polls_and_handles_empty_items() -> None:
    response = _client(operation={"status": "QUEUED", "items": []}).get(
        f"/admin/flatten/operations/{OPERATION_ID}"
    )

    assert response.status_code == 200
    html = response.text
    assert "Đã xếp hàng" in _operation_status(html)
    assert 'data-ops-terminal="false"' in html
    assert "data-ops-poll-note" in html
    assert f'href="/admin/flatten/operations/{OPERATION_ID}"' in html
    assert "Không có mục cần xử lý." in html


def test_telegram_bindings_show_active_chip_owner_tag_and_one_button_per_revoke_form() -> None:
    bindings = [
        {"membership_id": str(MEMBERSHIP), "created_at": "2026-11-14T05:00:00+00:00"},
        {"membership_id": str(OTHER_MEMBERSHIP), "created_at": "2026-11-13T05:00:00+00:00"},
    ]
    response = _client(bindings=bindings).get("/admin/telegram")

    assert response.status_code == 200
    html = response.text
    assert html.count("Đang hoạt động</span>") == 2
    assert html.count("Tài khoản của bạn") == 1
    assert str(MEMBERSHIP) in html
    assert str(OTHER_MEMBERSHIP) in html
    assert ">14/11 12:00<" in html
    assert "Telegram user ID" not in html
    forms = re.findall(
        r'<form method="post" action="/admin/telegram/revoke/[^"]+".*?</form>', html, re.DOTALL
    )
    assert len(forms) == 2
    for form in forms:
        assert len(re.findall(r"<button\b", form)) == 1
        assert "Lý do thu hồi</label>" in form
    assert '<label for="link-reason">Lý do</label>' in html
    assert html.count('class="ops-howto"') == 1


def test_telegram_bindings_empty_state() -> None:
    response = _client().get("/admin/telegram")

    assert response.status_code == 200
    assert "Chưa có liên kết hoạt động." in response.text
    assert "0 liên kết" in response.text


def test_telegram_link_created_keeps_code_only_in_href_with_single_status() -> None:
    response = _client().post(
        "/admin/telegram/link", data={"csrf_token": CSRF, "reason": "liên kết"}
    )

    assert response.status_code == 200
    url = "https://t.me/crypidian_test_bot?start=" + "Z" * 43
    main = _main(response.text)
    assert main.count(url) == 1
    assert f'href="{url}"' in main
    assert 'aria-label="Mở bot Telegram trong tin nhắn riêng"' in main
    assert main.count('role="status"') == 1
    assert 'data-copy-from="#telegram-deep-link"' in main
    assert "data-copy-secret" in main
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    ("path", "data", "status", "hint"),
    [
        ("/admin/flatten/preview", {"reason": "x"}, 403, "mã chống giả mạo (CSRF)"),
        (f"/admin/flatten/operations/{OPERATION_ID}", None, 401, "để tạo phiên mới"),
    ],
)
def test_error_page_shows_status_specific_next_step(
    path: str, data: dict[str, str] | None, status: int, hint: str
) -> None:
    client = _client(override_identity=status != 401)
    response = client.post(path, data=data) if data is not None else client.get(path)

    assert response.status_code == status
    html = response.text
    assert '<div role="alert" class="error-summary">' in html
    assert f"Mã phản hồi: {status}" in html
    assert hint in html
    assert "Tải lại trang ban đầu" in html
    assert 'href="/admin/login"' in html
    assert '<link rel="stylesheet" href="/static/pages/ops.css">' in html
    assert 'class="main-nav"' not in html


def test_flatten_rejection_shows_safe_reason_code_and_hint() -> None:
    from trading_bot.admin.web_router import SAFE_ERROR_REASONS, AdminHTTPException

    error = AdminHTTPException(409, "x", reason_code="FLATTEN_PREVIEW_EXPIRED")
    assert error.reason_code == "FLATTEN_PREVIEW_EXPIRED"
    # Mã không nằm trong danh sách an toàn không bao giờ được hiển thị.
    assert AdminHTTPException(409, "x", reason_code="SQL_STATE_XX000").reason_code is None
    assert "90 giây" in SAFE_ERROR_REASONS["FLATTEN_PREVIEW_EXPIRED"]
