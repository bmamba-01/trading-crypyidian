"""Playwright kiểm admin split approval desktop/mobile và payload API."""

from __future__ import annotations

import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import uvicorn
from playwright.sync_api import Page, expect

from trading_bot.adapters.database.repositories.learning_split_candidates import (
    LearningSplitCandidateReview,
)
from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings
from trading_bot.domain.learning.approval import LearningSplitApproval

pytest_plugins = ("tests.e2e.browser_fixtures",)

ROOT = Path(__file__).resolve().parents[2]
SCREENSHOTS = ROOT / "tests" / "e2e" / "evidence"
WORKSPACE_ID = UUID("bba2a6ed-7b24-43d3-a297-2ac617be105f")
MEMBERSHIP_ID = UUID("4c1fd40f-a99c-4a5c-a713-4ec54f33ddfd")
USER_ID = UUID("9b55d8b2-8f86-4ed7-9941-3310ec1d18db")
CANDIDATE_ID = UUID("9bf03bdb-9e35-5e18-b643-2e404083e3a9")
TRIAL_ID = UUID("ed50b745-c38b-4f77-9b30-5b7116ad2c70")
NOW = datetime.now(UTC)
MANIFEST_HASH = "b" * 64
FOLD_HASH = "c" * 64
PROOF_HASH = "d" * 64


def _candidate(*, approved_at: datetime | None = None) -> LearningSplitCandidateReview:
    return LearningSplitCandidateReview(
        candidate_id=CANDIDATE_ID,
        trial_record_id=TRIAL_ID,
        proof_hash=PROOF_HASH,
        manifest_hash=MANIFEST_HASH,
        split_policy_version="wp06-walk-forward-v1",
        fold_plan_hash=FOLD_HASH,
        evaluation_cutoff=NOW + timedelta(days=1),
        created_at=NOW,
        preview={
            "data_start": NOW.isoformat(),
            "data_end": (NOW + timedelta(days=5)).isoformat(),
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "sample_count": 160,
            "exclusion_count": 2,
            "development_count": 120,
            "holdout_count": 40,
            "fold_sample_count": 120,
            "folds": [
                {
                    "fold_id": FOLD_HASH,
                    "train": {
                        "start": NOW.isoformat(),
                        "end": (NOW + timedelta(days=2)).isoformat(),
                        "count": 70,
                    },
                    "validation": {
                        "start": (NOW + timedelta(days=2)).isoformat(),
                        "end": (NOW + timedelta(days=3)).isoformat(),
                        "count": 25,
                    },
                    "test": {
                        "start": (NOW + timedelta(days=3)).isoformat(),
                        "end": (NOW + timedelta(days=4)).isoformat(),
                        "count": 25,
                    },
                }
            ],
        },
        approved_at=approved_at,
    )


class _SplitFacade:
    def __init__(self) -> None:
        self.approvals: list[dict[str, object]] = []

    async def list_candidates(self) -> tuple[LearningSplitCandidateReview, ...]:
        return (
            (_candidate(approved_at=self.approvals[0]["approved_at"]),)
            if self.approvals
            else (_candidate(),)
        )

    async def approve(
        self, *, candidate_id: UUID, reason: str, idempotency_key: UUID
    ) -> LearningSplitApproval:
        assert candidate_id == CANDIDATE_ID
        approved_at = datetime.now(UTC)
        self.approvals.append(
            {
                "candidate_id": candidate_id,
                "reason": reason,
                "idempotency_key": idempotency_key,
                "approved_at": approved_at,
            }
        )
        return LearningSplitApproval(
            workspace_id=WORKSPACE_ID,
            manifest_hash=MANIFEST_HASH,
            split_policy_version="wp06-walk-forward-v1",
            fold_plan_hash=FOLD_HASH,
            actor_membership_id=MEMBERSHIP_ID,
            idempotency_key=idempotency_key,
            reason=reason,
            approved_at=approved_at,
            mfa_verified_at=approved_at,
        )


class _SplitService:
    def __init__(self) -> None:
        self.facade = _SplitFacade()

    def for_principal(self, principal: Principal) -> _SplitFacade:
        assert principal.workspace_id == WORKSPACE_ID
        return self.facade


class _AuthService:
    async def issue_csrf_token(self, _cookie: str | None) -> str:
        return "playwright-local-csrf-token"

    async def verify_csrf(self, _cookie: str | None, token: str | None) -> None:
        assert token == "playwright-local-csrf-token"


def _identity() -> AuthIdentity:
    return AuthIdentity(
        principal=Principal(
            user_id=USER_ID,
            membership_id=MEMBERSHIP_ID,
            workspace_id=WORKSPACE_ID,
            roles=frozenset({Role.OWNER}),
            mfa_verified_at=datetime.now(UTC),
        ),
        email="owner@example.test",
    )


def _serve_app() -> tuple[str, uvicorn.Server, threading.Thread, socket.socket, _SplitService]:
    application = create_app(AppSettings(app_env="test"))
    application.dependency_overrides[current_identity] = lambda: _identity()
    application.state.auth_service = _AuthService()
    service = _SplitService()
    application.state.learning_split_admin_service = service

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(application, log_level="error", lifespan="off"))
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        daemon=True,
        name="learning-split-playwright",
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.025)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=3)
        listener.close()
        raise RuntimeError("Playwright test server did not start")
    return f"http://127.0.0.1:{port}", server, thread, listener, service


def test_split_approval_ui_is_vietnamese_responsive_and_metadata_only(page: Page) -> None:
    base_url, server, thread, listener, service = _serve_app()
    try:
        page.set_viewport_size({"width": 1280, "height": 1000})
        page.goto(f"{base_url}/admin/learning/splits", wait_until="networkidle")
        expect(page.get_by_role("heading", name="Duyệt phân chia dữ liệu")).to_be_visible()
        expect(page.get_by_text("Candidate từ trial", exact=False)).to_be_visible()
        expect(page.get_by_text("70 mẫu", exact=False)).to_be_visible()
        body_text = page.locator("body").inner_text().lower()
        assert "label_value" not in body_text
        assert "feature_snapshot" not in body_text
        assert "sample_id" not in body_text
        assert "holdout payload" not in body_text
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        family = page.locator("body").evaluate("node => getComputedStyle(node).fontFamily")
        assert "Be Vietnam Pro" in family
        button = page.get_by_role("button", name="Phê duyệt kế hoạch split")
        assert button.bounding_box()["height"] >= 44
        SCREENSHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SCREENSHOTS / "wp06-splits-desktop.png"), full_page=True)

        page.set_viewport_size({"width": 390, "height": 844})
        page.reload(wait_until="networkidle")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        button = page.get_by_role("button", name="Phê duyệt kế hoạch split")
        assert button.bounding_box()["height"] >= 44
        page.screenshot(path=str(SCREENSHOTS / "wp06-splits-mobile.png"), full_page=True)

        page.get_by_label("Lý do phê duyệt").fill(
            "Đã kiểm tra khoảng thời gian, cỡ mẫu và vùng holdout."
        )
        api_requests: list[str] = []
        page_errors: list[str] = []
        page.on(
            "request",
            lambda request: (
                api_requests.append(request.url)
                if "/api/v1/learning-splits/candidates/" in request.url
                else None
            ),
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        assert page.evaluate(
            "Boolean(document.querySelector('.learning-split-candidates').dataset.csrfToken)"
        )
        assert page.evaluate(
            "document.querySelector('.learning-split-candidates').contains("
            "document.querySelector('[data-approve-form]'))"
        )
        button.click()
        page.wait_for_timeout(500)
        assert api_requests, f"Không phát request; page errors: {page_errors}"
        expect(page.get_by_text("Đã phê duyệt lúc", exact=False)).to_be_visible()
        assert len(service.facade.approvals) == 1
        submitted = service.facade.approvals[0]
        assert submitted["reason"] == "Đã kiểm tra khoảng thời gian, cỡ mẫu và vùng holdout."
        assert isinstance(submitted["idempotency_key"], UUID)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
