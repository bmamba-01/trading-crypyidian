"""Kiểm hành trình owner → reviewer với service policy thật, không gọi provider."""

from __future__ import annotations

import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import uvicorn
from fastapi import Request
from playwright.sync_api import Page, expect

from trading_bot.app import create_app
from trading_bot.application.source_policy_admin import SourcePolicyAdminService
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity
from trading_bot.config.settings import AppSettings
from trading_bot.domain.research.source_registry import (
    SourceQuotaSnapshot,
    SourceRegistryEntry,
    SourceUsage,
)

pytest_plugins = ("tests.e2e.browser_fixtures",)
SCREENSHOTS = Path(__file__).resolve().parent / "evidence"


class Repository:
    """Adapter bộ nhớ thay DB; mọi transition/quyền vẫn qua service thật."""

    def __init__(self):
        self.entries = {}
        self.evidence = []

    # Bằng chứng giấy phép (WP06 license evidence) — cùng hợp đồng với fake ở unit test.
    async def append_license_evidence(self, *, workspace_id, evidence):
        self.evidence.append(evidence)
        return evidence

    async def evidence_for_pending(self, *, workspace_id, source_id, policy_id):
        return next(
            (e for e in self.evidence if e.source_id == source_id and e.policy_id == policy_id),
            None,
        )

    async def evidence_by_id(self, *, workspace_id, source_id, evidence_id):
        return next(
            (e for e in self.evidence if e.source_id == source_id and e.evidence_id == evidence_id),
            None,
        )

    async def get_quota_snapshot(self, *, workspace_id, source_id):
        return None

    async def list_entries(self, *, workspace_id):
        return tuple(self.entries.values())

    async def get_source_snapshot(self, *, workspace_id, source_id):
        entry = self.entries.get(source_id)
        if entry is None:
            return None
        now = datetime.now(UTC)
        quota = SourceQuotaSnapshot(
            source_id,
            entry.current.sequence,
            now,
            now,
            entry.current.budget,
            SourceUsage(now, 2, 128, "0.25", "USD"),
        )
        return entry, quota

    async def get_entry(self, *, workspace_id, source_id):
        return self.entries.get(source_id)

    async def find_revisions_by_idempotency_key(self, *, workspace_id, key):
        return tuple(
            r
            for e in self.entries.values()
            for r in e.revisions
            if r.policy.policy_id.startswith(f"policy:{key.hex}:")
        )

    async def append_revision_and_observe(self, *, workspace_id, revision):
        from trading_bot.adapters.database.repositories.source_registry import (
            SourceRegistryAppendResult,
        )

        entry = self.entries.get(revision.policy.source_id)
        revisions = entry.revisions if entry else ()
        duplicate = any(r.policy.policy_id == revision.policy.policy_id for r in revisions)
        if not duplicate:
            self.entries[revision.policy.source_id] = SourceRegistryEntry(
                revision.policy.source_id, (*revisions, revision)
            )
        return SourceRegistryAppendResult(revision, duplicate)


class AuthService:
    async def issue_csrf_token(self, cookie):
        return "browser-form-csrf"

    async def verify_csrf(self, cookie, token):
        if token != "browser-form-csrf":
            raise AuthenticationFailure(403, "CSRF không hợp lệ.")


def serve_app():
    app = create_app(AppSettings(app_env="test"))
    workspace = uuid4()
    owner = AuthIdentity(
        Principal(uuid4(), uuid4(), workspace, frozenset({Role.OWNER}), datetime.now(UTC)),
        "owner@example.test",
    )
    reviewer = AuthIdentity(
        Principal(
            uuid4(), uuid4(), workspace, frozenset({Role.ANALYST_REVIEWER}), datetime.now(UTC)
        ),
        "reviewer@example.test",
    )

    async def actor(request: Request):
        return reviewer if request.cookies.get("e2e_actor") == "reviewer" else owner

    app.dependency_overrides[current_identity] = actor
    app.state.auth_service = AuthService()
    repository = Repository()
    app.state.source_policy_admin_service = SourcePolicyAdminService(repository)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.025)
    assert server.started
    return f"http://127.0.0.1:{listener.getsockname()[1]}", server, thread, listener, repository


def assert_layout(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for control in page.locator(
        "input:not([type=hidden]),select,textarea,button,summary,nav a"
    ).all():
        if control.is_visible():
            assert control.bounding_box()["height"] >= 44


def upload_evidence(page: Page, content: bytes) -> None:
    """Chủ sở hữu tải tài liệu quyền (WP06) để máy chủ tính SHA-256; trang tự tải lại khi xong."""
    upload = page.get_by_role("form", name="Tải lên bằng chứng quyền")
    upload.locator("#license-evidence-file").set_input_files(
        files=[{"name": "terms.txt", "mimeType": "text/plain", "buffer": content}]
    )
    with page.expect_navigation():
        upload.get_by_role("button", name="Tải lên và tính SHA-256").click()
    expect(page.locator("#license-evidence-digest")).to_have_count(1)


@pytest.mark.parametrize(
    ("width", "height", "suffix"), [(390, 844, "mobile"), (1280, 800, "desktop")]
)
def test_source_policy_owner_reviewer_flow(browser_page: Page, width, height, suffix):
    page = browser_page
    page.set_viewport_size({"width": width, "height": height})
    url, server, thread, listener, repository = serve_app()
    try:
        page.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith(url + "/") else route.abort()
            ),
        )
        page.goto(url + "/admin/research-sources")
        page.get_by_text("Tạo nguồn ứng viên", exact=True).first.click()
        form = page.get_by_role("form", name="Tạo nguồn ứng viên")
        values = {
            "source_id": "offline-calendar",
            "publisher": "Nhà xuất bản ngoại tuyến",
            "endpoint_url": "https://example.test/calendar",
            "reason": "Đăng ký ứng viên để rà soát quyền",
            "attribution": "not approved",
        }
        for name, value in values.items():
            form.locator(f'[name="{name}"]').fill(value)
        assert_layout(page)
        form.get_by_role("button", name="Tạo nguồn ứng viên").click()
        expect(page.get_by_role("heading", name="Chi tiết nguồn: offline-calendar")).to_be_visible()
        page.get_by_text("Gửi policy chờ duyệt", exact=True).click()
        pending = page.get_by_role("form", name="Gửi policy chờ duyệt")
        pending.locator('[name="evidence_ref"]').fill(
            "https://example.test/terms/<script>alert(1)</script>"
        )
        pending.get_by_label("Hiển thị", exact=True).check()
        pending.get_by_label("Nghiên cứu kiểm thử lịch sử", exact=True).check()
        pending.locator('[name="valid_until"]').fill("2027-01-01T00:00:00+00:00")
        # Phạm vi giấy phép (WP06 license scope) là bắt buộc khi gửi duyệt;
        # provider/product/venue/contract phải là token ASCII viết hoa
        # (domain/research/license_scope.py).
        for name, value in {
            "scope_provider": "OFFLINE_PUBLISHER",
            "scope_product": "EVENT_CALENDAR",
            "scope_venue": "OFFLINE",
            "scope_contract": "NONE",
            "scope_symbols": "BTCUSDT",
            "scope_intervals": "1h",
            "scope_data_start": "2026-01-01T00:00:00+00:00",
            "scope_data_end": "2026-12-31T00:00:00+00:00",
            "scope_attribution": "not approved",
        }.items():
            pending.locator(f'[name="{name}"]').fill(value)
        pending.locator('input[name^="scope_output_"]').first.check()
        pending.locator('input[name^="scope_derived_"]').first.check()
        pending.locator('[name="reason"]').fill("Đề nghị duyệt hai mục đích có thời hạn")
        assert_layout(page)
        pending.get_by_role("button", name="Gửi chờ duyệt").focus()
        page.keyboard.press("Enter")
        expect(page.locator("[data-source-state]")).to_have_text("Chờ duyệt quyền")
        # WP06: chủ sở hữu tải tài liệu quyền để máy chủ tính SHA-256 trước khi reviewer duyệt.
        upload_evidence(page, b"Offline calendar terms for research display only.")
        assert len(repository.evidence) == 1
        page.get_by_text("Duyệt quyền độc lập", exact=True).click()
        approval = page.get_by_role("form", name="Duyệt quyền độc lập")
        approval.locator('[name="license_evidence_id"]').select_option(index=1)
        approval.locator('[name="reason"]').fill("Thử tự duyệt")
        assert_layout(page)
        with page.expect_response(
            lambda r: r.url.endswith("/approve") and r.request.method == "POST"
        ) as denied:
            approval.get_by_role("button", name="Duyệt quyền", exact=True).click()
        assert denied.value.status == 403
        assert len(repository.entries["offline-calendar"].revisions) == 2
        page.context.add_cookies([{"name": "e2e_actor", "value": "reviewer", "url": url}])
        page.goto(url + "/admin/research-sources?source_id=offline-calendar")
        expect(page.get_by_text("Tạo nguồn ứng viên", exact=True)).to_have_count(0)
        expect(page.get_by_text("Gửi policy chờ duyệt", exact=True)).to_have_count(0)
        page.get_by_text("Duyệt quyền độc lập", exact=True).click()
        approval = page.get_by_role("form", name="Duyệt quyền độc lập")
        expect(approval.get_by_label("Hiển thị", exact=True)).to_be_checked()
        expect(approval.get_by_label("Nghiên cứu kiểm thử lịch sử", exact=True)).to_be_checked()
        expect(approval.get_by_label("Siêu dữ liệu", exact=True)).not_to_be_checked()
        expect(approval.locator('[name="valid_until"]')).to_have_value("2027-01-01T00:00:00+00:00")
        expect(approval.locator('[name="expiry_action"]')).to_have_value("preserve")
        approval.locator('[name="license_evidence_id"]').select_option(index=1)
        approval.locator('[name="reason"]').fill("Đã đối chiếu tham chiếu theo quyền được giao")
        approval.get_by_role("button", name="Duyệt quyền", exact=True).click()
        expect(page.locator("[data-source-state]")).to_have_text("Đã duyệt quyền")
        # Khối đầu là metadata nguồn; khối sau là phạm vi giấy phép (cũng ẩn ghi công chưa duyệt).
        for metadata in page.locator(".source-metadata").all():
            expect(metadata).to_contain_text("Chưa được duyệt")
        assert "not approved" not in page.content()
        assert repository.entries["offline-calendar"].current.attribution == "not approved"
        expect(
            page.get_by_text(
                "Evidence tham chiếu do người quản trị cung cấp — "
                "chưa được hệ thống xác minh pháp lý",
                exact=True,
            )
        ).to_be_visible()
        expect(page.locator(".source-history > li")).to_have_count(3)
        expect(page.get_by_text("Số yêu cầu: 2 / 10", exact=False)).to_be_visible()
        current = repository.entries["offline-calendar"].current.policy
        assert {str(p) for p in current.allowed_purposes} == {"display", "backtest_research"}
        assert current.valid_until.isoformat() == "2027-01-01T00:00:00+00:00"
        expect(page.locator(".source-history")).to_contain_text("Hiển thị")
        expect(page.locator(".source-history")).to_contain_text("Nghiên cứu kiểm thử lịch sử")
        expect(page.locator('a[href^="https://example.test"]')).to_have_count(0)
        # evidence_ref chứa <script> phải bị escape:
        # không có script nội tuyến, mọi script là tệp tĩnh.
        expect(page.locator("script:not([src])")).to_have_count(0)
        assert all(
            src.startswith("/static/")
            for src in page.locator("script[src]").evaluate_all(
                "els => els.map(e => e.getAttribute('src'))"
            )
        )
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        for control in page.locator(
            "input:not([type=hidden]),select,textarea,button,summary,nav a"
        ).all():
            if control.is_visible():
                assert control.bounding_box()["height"] >= 44
        assert (
            page.locator("body")
            .evaluate("el => getComputedStyle(el).fontFamily")
            .startswith('"Be Vietnam Pro"')
        )
        assert (
            page.request.get(url + "/admin/research-sources").headers["cache-control"] == "no-store"
        )
        page.context.clear_cookies()
        page.goto(url + "/admin/research-sources?source_id=offline-calendar")
        page.get_by_text("Gửi policy chờ duyệt", exact=True).click()
        pending = page.get_by_role("form", name="Gửi policy chờ duyệt")
        expect(pending.get_by_label("Hiển thị", exact=True)).to_be_checked()
        expect(pending.get_by_label("Nghiên cứu kiểm thử lịch sử", exact=True)).to_be_checked()
        expect(pending.locator('[name="valid_until"]')).to_have_value("2027-01-01T00:00:00+00:00")
        pending.locator('[name="evidence_ref"]').fill("terms-review")
        pending.locator('[name="reason"]').fill("Đề nghị rà soát thời hạn")
        pending.get_by_role("button", name="Gửi chờ duyệt").click()
        expect(page.locator("[data-source-state]")).to_have_text("Chờ duyệt quyền")
        # Tham chiếu evidence đổi nên cần tài liệu mới khớp pending hiện hành.
        upload_evidence(page, b"Terms review: display and backtest research until revoked.")
        assert len(repository.evidence) == 2
        page.context.add_cookies([{"name": "e2e_actor", "value": "reviewer", "url": url}])
        page.reload()
        page.get_by_text("Duyệt quyền độc lập", exact=True).click()
        approval = page.get_by_role("form", name="Duyệt quyền độc lập")
        approval.locator('[name="license_evidence_id"]').select_option(index=1)
        expect(approval.locator('[name="expiry_action"]')).to_have_value("preserve")
        approval.get_by_label("Xử lý thời hạn", exact=True).select_option("clear")
        approval.locator('[name="reason"]').fill("Chủ động bỏ thời hạn sau rà soát")
        assert_layout(page)
        approval.get_by_role("button", name="Duyệt quyền", exact=True).click()
        expect(page.locator(".source-history > li")).to_have_count(5)
        assert repository.entries["offline-calendar"].current.policy.valid_until is None
        assert_layout(page)
        SCREENSHOTS.mkdir(exist_ok=True)
        page.screenshot(path=str(SCREENSHOTS / f"wp05a-source-policy-{suffix}.png"), full_page=True)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
