"""Playwright kiểm trang vận hành scorer tiếng Việt trên desktop và mobile."""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Page, expect

from trading_bot.adapters.database.repositories.learning_scoring_deployments import (
    ScorerDeploymentConflict,
)
from trading_bot.admin.learning_scoring_page_router import router as scoring_page_router
from trading_bot.app import create_app
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings
from trading_bot.domain.learning.scorer_report_summary import ScorerReportSummary
from trading_bot.domain.learning.scorer_score_event_summary import (
    ScorerScoreEventPage,
    ScorerScoreEventSummary,
)
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentAdminSnapshot,
    ScorerDeploymentEvent,
    ScorerDeploymentHealth,
    ScorerDeploymentMode,
)

pytest_plugins = ("tests.e2e.browser_fixtures",)

ROOT = Path(__file__).resolve().parents[2]
SCREENSHOTS = ROOT / "tests" / "e2e" / "evidence"
WORKSPACE_ID = UUID("bba2a6ed-7b24-43d3-a297-2ac617be105f")
MEMBERSHIP_ID = UUID("4c1fd40f-a99c-4a5c-a713-4ec54f33ddfd")
USER_ID = UUID("9b55d8b2-8f86-4ed7-9941-3310ec1d18db")


class _AuthService:
    def __init__(self) -> None:
        self.verified_tokens: list[str | None] = []

    async def issue_csrf_token(self, _cookie: str | None) -> str:
        return "playwright-scorer-csrf-token"

    async def verify_csrf(self, _cookie: str | None, token: str | None) -> None:
        self.verified_tokens.append(token)
        if token != "playwright-scorer-csrf-token":
            raise AssertionError("Token CSRF không khớp form scorer.")


class _ScorerDeploymentFacade:
    def __init__(self, principal: Principal) -> None:
        self.principal = principal
        self.snapshot = ScorerDeploymentAdminSnapshot(
            workspace_id=WORKSPACE_ID,
            mode=ScorerDeploymentMode.SHADOW,
            deployment_event_id=uuid4(),
            deployment_revision=2,
            active_pointer_version=4,
            health=ScorerDeploymentHealth.RIGHTS_RECHECK_REQUIRED,
            runtime_usable=False,
        )
        self.calls: list[dict[str, object]] = []
        self.fail_next_request = False

    async def load_admin_scoring_deployment(self) -> ScorerDeploymentAdminSnapshot:
        return self.snapshot

    async def set_mode(
        self,
        *,
        mode: ScorerDeploymentMode,
        evaluation_report_id: UUID | None = None,
        expected_pointer_version: int,
        expected_deployment_revision: int,
        idempotency_key: UUID,
        reason: str,
    ) -> ScorerDeploymentEvent:
        if self.fail_next_request:
            self.fail_next_request = False
            raise ScorerDeploymentConflict("stale scorer revision")
        self.calls.append(
            {
                "mode": mode,
                "evaluation_report_id": evaluation_report_id,
                "expected_pointer_version": expected_pointer_version,
                "expected_deployment_revision": expected_deployment_revision,
                "idempotency_key": idempotency_key,
                "reason": reason,
                "actor_membership_id": self.principal.membership_id,
            }
        )
        assert mode is ScorerDeploymentMode.DISABLED
        now = time_now()
        event = ScorerDeploymentEvent(
            event_id=uuid4(),
            workspace_id=self.principal.workspace_id,
            revision=expected_deployment_revision + 1,
            mode=mode,
            target=None,
            actor_membership_id=self.principal.membership_id,
            mfa_verified_at=self.principal.mfa_verified_at,
            mfa_method="TOTP",
            expected_pointer_version=expected_pointer_version,
            expected_deployment_revision=expected_deployment_revision,
            reason=reason,
            idempotency_key=idempotency_key,
            previous_event_id=self.snapshot.deployment_event_id,
            previous_event_hash="a" * 64,
            created_at=now,
        )
        self.snapshot = ScorerDeploymentAdminSnapshot(
            workspace_id=self.principal.workspace_id,
            mode=ScorerDeploymentMode.DISABLED,
            deployment_event_id=event.event_id,
            deployment_revision=event.revision,
            active_pointer_version=expected_pointer_version,
            health=ScorerDeploymentHealth.DISABLED,
            runtime_usable=False,
        )
        return event


class _ScorerDeploymentService:
    def __init__(self, principal: Principal) -> None:
        self.facade = _ScorerDeploymentFacade(principal)
        self.principals: list[Principal] = []

    def for_principal(self, principal: Principal) -> _ScorerDeploymentFacade:
        assert principal.workspace_id == WORKSPACE_ID
        self.principals.append(principal)
        return self.facade


class _ScorerReportReadModel:
    def __init__(self) -> None:
        self.workspaces: list[UUID] = []
        self.summary: object | None = None

    async def load_latest_summary(self, workspace_id: UUID) -> ScorerReportSummary:
        self.workspaces.append(workspace_id)
        if self.summary is not None:
            return self.summary
        return _report_summary()


def _report_summary() -> ScorerReportSummary:
    return ScorerReportSummary(
        evaluation_report_id=UUID("f1e2d3c4-b5a6-4798-8c7d-6e5f4a3b2c10"),
        created_at=datetime(2026, 11, 10, 8, 30, tzinfo=UTC),
        evaluated_at=datetime(2026, 11, 10, 8, 25, tzinfo=UTC),
        status="INCONCLUSIVE",
        report_hash="a" * 64,
        proof_hash="b" * 64,
        scorer_artifact_hash="c" * 64,
        base_artifact_hash="d" * 64,
        policy_hash="e" * 64,
        policy_version="test-policy-v1",
        manifest_hash="f" * 64,
        fold_plan_hash="1" * 64,
        fold_id="fold-test-01",
        vector_schema_version="scorer-vector-v2",
        vector_schema_hash="2" * 64,
        candidate_completeness_verified=False,
        scored_candidate_coverage_verified=False,
        test_sample_count=9,
        opportunity_count=90,
        class_support=((-1, 3), (0, 2), (1, 4)),
        confusion_matrix=((1, 0, 1, 1), (0, 2, 0, 0), (0, 0, 3, 1)),
        coverage=0.75,
        abstain_count=2,
        log_loss=0.41,
        brier_score=0.18,
        expected_calibration_error=0.08,
        false_veto_count=1,
        false_veto_rate=0.25,
        candidate_retention=0.67,
        candidate_count=6,
        scored_candidate_count=4,
        approved_candidate_count=3,
        no_candidate_count=80,
        input_excluded_count=2,
        not_run_count=2,
    )


class _ScorerScoreEventReadModel:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, int]] = []

    async def load_page(self, workspace_id: UUID, *, page: int, page_size: int = 20):
        self.calls.append((workspace_id, page))
        if page == 1:
            item = ScorerScoreEventSummary(
                event_id=UUID("f583002a-80ea-414e-b3d8-6d09f230e8bb"),
                created_at=datetime(2026, 11, 10, 8, 40, tzinfo=UTC),
                mode="SHADOW",
                decision="VETO",
                reason_code="LOW_CONFIDENCE",
                probability=0.21,
                confidence=0.72,
                uncertainty=0.28,
            )
            return ScorerScoreEventPage(items=(item,), page=page, has_next=True)
        item = ScorerScoreEventSummary(
            event_id=UUID("f583002a-80ea-414e-b3d8-6d09f230e8bc"),
            created_at=datetime(2026, 11, 10, 8, 35, tzinfo=UTC),
            mode="PAPER_GATE",
            decision="ABSTAIN",
            reason_code="MODEL_UNAVAILABLE",
            probability=None,
            confidence=None,
            uncertainty=None,
        )
        return ScorerScoreEventPage(items=(item,), page=page, has_next=False)


def time_now():
    return datetime.now(UTC)


def _identity() -> AuthIdentity:
    return AuthIdentity(
        principal=Principal(
            user_id=USER_ID,
            membership_id=MEMBERSHIP_ID,
            workspace_id=WORKSPACE_ID,
            roles=frozenset({Role.ANALYST_REVIEWER}),
            mfa_verified_at=time_now(),
            mfa_method=MfaMethod.TOTP,
        ),
        email="reviewer@example.test",
    )


def _serve_app() -> tuple[
    str,
    uvicorn.Server,
    threading.Thread,
    socket.socket,
    _ScorerDeploymentService,
    _AuthService,
    _ScorerReportReadModel,
    _ScorerScoreEventReadModel,
]:
    application: FastAPI = create_app(AppSettings(app_env="test"))
    identity = _identity()
    application.dependency_overrides[current_identity] = lambda: identity
    auth_service = _AuthService()
    application.state.auth_service = auth_service
    service = _ScorerDeploymentService(identity.principal)
    application.state.learning_scoring_deployment_service = service
    report_read_model = _ScorerReportReadModel()
    application.state.learning_scoring_report_read_model = report_read_model
    score_event_read_model = _ScorerScoreEventReadModel()
    application.state.learning_scoring_score_event_read_model = score_event_read_model
    if not any(
        getattr(route, "path", None) == "/admin/learning/scoring" for route in application.routes
    ):
        application.include_router(scoring_page_router)

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
        name="learning-scorer-admin-playwright",
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.025)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=3)
        raise RuntimeError("Admin web chưa khởi động trong thời hạn E2E.")
    return (
        f"http://127.0.0.1:{port}",
        server,
        thread,
        listener,
        service,
        auth_service,
        report_read_model,
        score_event_read_model,
    )


def _stop_app(server: uvicorn.Server, thread: threading.Thread, listener: socket.socket) -> None:
    server.should_exit = True
    thread.join(timeout=5)
    listener.close()


def _assert_layout(page: Page, *, width: int, height: int) -> None:
    page.set_viewport_size({"width": width, "height": height})
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => resolve()))")
    layout = page.evaluate(
        """() => ({
          scroll: document.documentElement.scrollWidth,
          inner: window.innerWidth,
          matrixChain: (() => {
            const chain = [];
            let element = document.querySelector("#learning-scoring-confusion-matrix");
            while (element) {
              const rect = element.getBoundingClientRect();
              chain.push({
                tag: element.tagName,
                id: element.id,
                className: typeof element.className === "string" ? element.className : "",
                left: Math.round(rect.left),
                right: Math.round(rect.right),
                width: Math.round(rect.width),
                scrollWidth: element.scrollWidth,
                clientWidth: element.clientWidth,
                minWidth: getComputedStyle(element).minWidth,
                overflowX: getComputedStyle(element).overflowX,
              });
              element = element.parentElement;
            }
            return chain;
          })(),
          overflowing: Array.from(document.querySelectorAll("body *"))
            .map(element => {
              const rect = element.getBoundingClientRect();
              const style = getComputedStyle(element);
              return {
                tag: element.tagName,
                id: element.id,
                className: typeof element.className === "string" ? element.className : "",
                right: Math.round(rect.right),
                width: Math.round(rect.width),
                scrollWidth: element.scrollWidth,
                clientWidth: element.clientWidth,
                minWidth: style.minWidth,
                overflowX: style.overflowX,
              };
            })
            .filter(element => element.right > window.innerWidth + 1)
            .slice(0, 12),
        })"""
    )
    assert layout["scroll"] <= width, (width, layout)
    minimum_target_height = 44 if width < 768 else 36
    for button in page.get_by_role("button").all():
        if button.is_visible():
            bounds = button.bounding_box()
            assert bounds is not None, (width, bounds)
            assert bounds["height"] >= minimum_target_height, (width, bounds)


def test_scorer_admin_is_locked_responsive_and_uses_cas_disable(
    browser_page: Page,
) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    (
        app_url,
        server,
        thread,
        listener,
        service,
        auth_service,
        report_read_model,
        score_event_read_model,
    ) = _serve_app()
    try:
        browser_page.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith(app_url + "/") else route.abort()
            ),
        )
        response = browser_page.goto(f"{app_url}/admin/learning/scoring", wait_until="load")
        assert response is not None
        assert response.status == 200
        assert response.headers["cache-control"] == "no-store"
        expect(
            browser_page.get_by_role("heading", name="Điều phối chấm điểm mô hình")
        ).to_be_visible()
        expect(
            browser_page.get_by_text("Chưa đủ điều kiện báo cáo và ủy quyền để bật chế độ")
        ).to_be_visible()
        expect(
            browser_page.get_by_text(
                "SHADOW và PAPER_GATE đang khóa theo các điều kiện được liệt kê bên dưới."
            )
        ).to_be_visible()
        expect(browser_page.get_by_text("Cần xác minh lại quyền hiện hành")).to_be_visible()
        expect(browser_page.get_by_text("Chỉ quan sát").first).to_be_visible()
        expect(
            browser_page.get_by_role("heading", name="Báo cáo scorer TEST gần nhất")
        ).to_be_visible()
        expected_report_id = (
            report_read_model.summary.evaluation_report_id
            if report_read_model.summary is not None
            else _report_summary().evaluation_report_id
        )
        expect(
            browser_page.get_by_role("link", name="Mở thẻ mô hình và độ trễ PAPER")
        ).to_have_attribute(
            "href",
            f"/admin/learning/model-card?report_id={expected_report_id}",
        )
        matrix = browser_page.get_by_role("table", name="Ma trận nhầm lẫn trên mẫu TEST")
        expect(matrix).to_be_visible()
        expect(matrix.get_by_role("columnheader", name="Không chấm được (ABSTAIN)")).to_be_visible()
        expect(matrix.get_by_role("rowheader", name="PnL âm")).to_be_visible()
        expect(matrix.get_by_role("rowheader", name="PnL trung tính")).to_be_visible()
        expect(matrix.get_by_role("rowheader", name="PnL dương")).to_be_visible()
        expect(
            browser_page.get_by_text("Ma trận này không phải ma trận hành động PAPER.")
        ).to_be_visible()
        expect(browser_page.get_by_text("Chưa kết luận", exact=True)).to_be_visible()
        expect(browser_page.get_by_text("Báo cáo INCONCLUSIVE")).to_be_visible()
        expect(browser_page.get_by_text("FP lớp PnL dương", exact=True)).to_be_visible()
        expect(browser_page.get_by_text("FN lớp PnL dương", exact=True)).to_be_visible()
        expect(
            browser_page.get_by_text("False-veto theo hành động scorer", exact=True)
        ).to_be_visible()
        expect(
            browser_page.get_by_text("Không tạo ứng viên (NO_CANDIDATE)", exact=True)
        ).to_be_visible()
        expect(
            browser_page.get_by_text("Đầu vào bị loại (INPUT_EXCLUDED)", exact=True)
        ).to_be_visible()
        inventory_counts = browser_page.locator("#learning-scoring-opportunity-counts")
        expect(
            inventory_counts.locator(".kv").filter(has_text="Không tạo ứng viên").locator("dd")
        ).to_have_text("80")
        expect(
            inventory_counts.locator(".kv").filter(has_text="Đầu vào bị loại").locator("dd")
        ).to_have_text("2")
        expect(
            browser_page.get_by_text(
                "NO_CANDIDATE không đồng nghĩa với tín hiệu bị bỏ lỡ, lệnh hoặc trade"
            )
        ).to_be_visible()
        expect(browser_page.get_by_text("không tạo nhãn PnL")).to_be_visible()
        expect(browser_page.get_by_text("1 / 4 mẫu PnL dương", exact=True)).to_be_visible()
        diagnostics = browser_page.locator("#learning-scoring-positive-class-errors")
        expect(
            diagnostics.locator(".kv").filter(has_text="FP lớp PnL dương").locator("dd")
        ).to_have_text("1")
        expect(
            diagnostics.locator(".kv").filter(has_text="FN lớp PnL dương").locator("dd")
        ).to_have_text("1")
        expect(browser_page.get_by_text("FN gồm cả trường hợp ABSTAIN")).to_be_visible()
        expect(
            browser_page.get_by_text("VETO và ABSTAIN không đồng nghĩa với lỗi phân lớp")
        ).to_be_visible()
        expect(browser_page.get_by_text("75.0%", exact=True)).to_have_count(0)
        expect(browser_page.get_by_text("8.0%", exact=True)).to_have_count(0)
        expect(browser_page.get_by_text("67.0%", exact=True)).to_have_count(0)
        expect(
            browser_page.locator(".kv")
            .filter(has_text="Cơ hội chưa chạy strategy (NOT_RUN)")
            .locator("dd")
        ).to_have_text("2")
        expect(browser_page.get_by_role("button", name="Tắt chấm điểm")).to_be_visible()
        expect(browser_page.get_by_label("Chế độ muốn áp dụng")).to_be_visible()
        expect(browser_page.locator("#scorer-mode option[value='SHADOW']")).to_be_disabled()
        expect(browser_page.locator("#scorer-mode option[value='PAPER_GATE']")).to_be_disabled()
        expect(browser_page.get_by_text("INCONCLUSIVE; cần ACCEPTED_SHADOW_ONLY")).to_be_visible()
        expect(browser_page.get_by_text("runtime_usable=false").first).to_be_visible()
        expect(
            browser_page.get_by_text("SHADOW chỉ quan sát và ghi điểm, không thay đổi quyết định.")
        ).to_be_visible()
        expect(
            browser_page.get_by_text("PAPER_GATE vẫn đi qua các cổng RiskEngine thông thường")
        ).to_be_visible()
        expect(browser_page.locator("#learning-scoring-status")).to_be_hidden()
        assert browser_page.locator("select").count() == 1
        assert "feature_schema_hash" not in browser_page.content()
        assert "test_payload" not in browser_page.content()
        expect(browser_page.get_by_text("Từ chối ứng viên")).to_be_visible()
        expect(browser_page.get_by_text("LOW_CONFIDENCE")).to_be_visible()
        expect(browser_page.get_by_text("21.0%")).to_be_visible()
        assert "Báo cáo scorer TEST gần nhất" in browser_page.content()
        assert "AAAAAAAA" not in browser_page.content()
        assert "aaaaaaaaaaaaaaaa…" in browser_page.content()
        assert "sample_ids" not in browser_page.content()
        assert "score event" not in browser_page.content().lower()
        assert "feature vector" not in browser_page.content().lower()
        assert "workspace" not in browser_page.content().lower()

        font_family = browser_page.locator("body").evaluate(
            "element => getComputedStyle(element).fontFamily"
        )
        assert "Be Vietnam Pro" in font_family
        browser_page.keyboard.press("Tab")
        expect(browser_page.locator(".skip-link")).to_be_focused()
        _assert_layout(browser_page, width=1280, height=900)
        deployment_metadata = browser_page.locator(".lx-kpis .kpi-subtext--keep")
        expect(deployment_metadata).to_be_visible()
        assert (
            deployment_metadata.evaluate("element => getComputedStyle(element).whiteSpace")
            == "normal"
        )
        assert deployment_metadata.evaluate("element => element.scrollWidth <= element.clientWidth")
        assert browser_page.locator("#learning-scoring-confusion-matrix tbody tr").count() == 3
        browser_page.screenshot(path=str(SCREENSHOTS / "wp06-scoring-desktop.png"), full_page=True)
        browser_page.set_viewport_size({"width": 390, "height": 844})
        browser_page.reload(wait_until="load")
        _assert_layout(browser_page, width=390, height=844)
        mobile_health_value = browser_page.locator(".lx-kpis .kpi-value--text").nth(1)
        mobile_health_font_size = float(
            mobile_health_value.evaluate(
                "element => getComputedStyle(element).fontSize"
            ).removesuffix("px")
        )
        assert mobile_health_font_size <= 17
        expect(
            browser_page.locator(".kv")
            .filter(has_text="Cơ hội chưa chạy strategy (NOT_RUN)")
            .locator("dd")
        ).to_have_text("2")
        expect(browser_page.locator("#learning-scoring-confusion-matrix-wrap")).to_be_hidden()
        mobile_matrix_rows = browser_page.locator(".lx-confusion__mobile-row")
        assert mobile_matrix_rows.count() == 3
        first_matrix_row = mobile_matrix_rows.first
        expect(first_matrix_row.get_by_text("Nhãn thực tế: PnL âm")).to_be_visible()
        assert first_matrix_row.locator("dd").all_inner_texts() == ["1", "0", "1", "1"]
        expect(first_matrix_row.get_by_text("Không chấm được (ABSTAIN)")).to_be_visible()
        expect(browser_page.locator("#learning-scoring-positive-class-errors")).to_be_visible()
        expect(browser_page.locator("#learning-scoring-opportunity-counts")).to_be_visible()
        expect(
            browser_page.locator("#learning-scoring-opportunity-counts")
            .locator(".kv")
            .filter(has_text="Không tạo ứng viên")
            .locator("dd")
        ).to_have_text("80")
        expect(browser_page.get_by_text("1 / 4 mẫu PnL dương", exact=True)).to_be_visible()
        browser_page.screenshot(path=str(SCREENSHOTS / "wp06-scoring-mobile.png"), full_page=True)

        report_read_model.summary = replace(
            _report_summary(), not_run_count=None, input_excluded_count=4, confusion_matrix=None
        )
        browser_page.reload(wait_until="load")
        expect(
            browser_page.locator("#learning-scoring-opportunity-counts .kv")
            .filter(has_text="Chưa chạy hoặc bị loại (proof v2 gộp)")
            .locator("dd")
        ).to_have_text("4")
        expect(
            browser_page.get_by_text("Ma trận nhầm lẫn chưa có trong báo cáo cũ.")
        ).to_be_visible()
        expect(browser_page.locator("#learning-scoring-confusion-matrix")).to_have_count(0)
        expect(
            browser_page.get_by_text("Chưa chạy hoặc bị loại (proof v2 gộp)", exact=True)
        ).to_be_visible()
        expect(
            browser_page.get_by_text("proof v2 gộp EVALUATED + NOT_RUN và INPUT_EXCLUDED")
        ).to_be_visible()
        expect(
            browser_page.get_by_text("Đầu vào bị loại (INPUT_EXCLUDED)", exact=True)
        ).to_have_count(0)
        _assert_layout(browser_page, width=390, height=844)
        report_read_model.summary = None
        browser_page.reload(wait_until="load")
        score_event_read_model.calls.clear()

        browser_page.get_by_role("link", name="Sự kiện cũ hơn").click()
        expect(browser_page).to_have_url(f"{app_url}/admin/learning/scoring?score_event_page=2")
        expect(browser_page.get_by_text("Không chấm được", exact=True)).to_be_visible()
        expect(browser_page.get_by_text("MODEL_UNAVAILABLE")).to_be_visible()
        assert score_event_read_model.calls == [
            (WORKSPACE_ID, 2),
        ]

        reason = browser_page.get_by_label("Lý do thay đổi")
        reason.fill("Quyền hiện hành chưa xác minh, tắt deployment scorer.")
        expect(browser_page.get_by_role("button", name="Tắt chấm điểm")).to_be_enabled()
        service.facade.fail_next_request = True
        with browser_page.expect_response(
            lambda item: (
                item.request.method == "POST"
                and item.url.endswith("/api/v1/learning-scorer/deployment")
            )
        ) as failed_response:
            browser_page.get_by_role("button", name="Tắt chấm điểm").click()
        assert failed_response.value.status == 409
        expect(browser_page.get_by_role("alert")).to_have_text(
            "Deployment scorer đã đổi hoặc chưa đủ quyền hiện hành để bật."
        )
        first_idempotency_key = failed_response.value.request.headers["idempotency-key"]
        with browser_page.expect_request(
            lambda item: (
                item.method == "POST" and item.url.endswith("/api/v1/learning-scorer/deployment")
            )
        ) as disable_request:
            with browser_page.expect_response(
                lambda item: (
                    item.request.method == "POST"
                    and item.url.endswith("/api/v1/learning-scorer/deployment")
                )
            ) as disable_response:
                browser_page.get_by_role("button", name="Tắt chấm điểm").click()
        request = disable_request.value
        assert request.headers["x-csrf-token"] == "playwright-scorer-csrf-token"
        assert UUID(request.headers["idempotency-key"])
        assert request.headers["idempotency-key"] == first_idempotency_key
        assert request.post_data_json == {
            "mode": "DISABLED",
            "expected_pointer_version": 4,
            "expected_deployment_revision": 2,
            "reason": "Quyền hiện hành chưa xác minh, tắt deployment scorer.",
        }
        assert disable_response.value.status == 200
        assert disable_response.value.headers["cache-control"] == "no-store"
        assert auth_service.verified_tokens == [
            "playwright-scorer-csrf-token",
            "playwright-scorer-csrf-token",
        ]
        assert len(service.facade.calls) == 1
        assert service.facade.calls[0]["actor_membership_id"] == MEMBERSHIP_ID
        assert service.facade.calls[0]["expected_pointer_version"] == 4
        assert service.facade.calls[0]["expected_deployment_revision"] == 2
        assert isinstance(service.facade.calls[0]["idempotency_key"], UUID)
        expect(browser_page.get_by_text("Đang tắt").first).to_be_visible()
        assert len(service.principals) == 8
        assert all(principal.workspace_id == WORKSPACE_ID for principal in service.principals)
        assert report_read_model.workspaces == [WORKSPACE_ID] * 6
    finally:
        _stop_app(server, thread, listener)


def test_active_mode_lock_prevents_request_even_with_eligible_report(
    browser_page: Page,
) -> None:
    report_id = UUID("ad2d5800-f3e7-4e57-9818-3a64ae69a9a6")
    summary_with_id = replace(
        _report_summary(),
        evaluation_report_id=report_id,
        status="ACCEPTED_SHADOW_ONLY",
    )
    (
        app_url,
        server,
        thread,
        listener,
        service,
        _auth_service,
        report_read_model,
        _score_event_read_model,
    ) = _serve_app()
    try:
        service.facade.snapshot = ScorerDeploymentAdminSnapshot(
            workspace_id=WORKSPACE_ID,
            mode=ScorerDeploymentMode.DISABLED,
            deployment_event_id=None,
            deployment_revision=0,
            active_pointer_version=4,
            health=ScorerDeploymentHealth.DISABLED,
            runtime_usable=False,
        )
        report_read_model.summary = summary_with_id
        browser_page.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith(app_url + "/") else route.abort()
            ),
        )
        response = browser_page.goto(f"{app_url}/admin/learning/scoring", wait_until="load")
        assert response is not None
        assert response.status == 200
        expect(browser_page.locator("#scorer-mode option[value='SHADOW']")).to_be_disabled()
        expect(browser_page.locator("#scorer-mode option[value='PAPER_GATE']")).to_be_disabled()
        assert browser_page.locator("#scorer-deployment-form").get_attribute(
            "data-evaluation-report-id"
        ) == str(report_id)
        expect(browser_page.locator("#scorer-deployment-submit")).to_be_disabled()
        submit_result = browser_page.evaluate(
            """() => {
              const requests = [];
              window.fetch = (...args) => {
                requests.push(args[0]);
                return Promise.resolve(new Response("{}", {
                  status: 200,
                  headers: {"content-type": "application/json"}
                }));
              };
              const select = document.querySelector("#scorer-mode");
              const reason = document.querySelector("#scorer-deployment-reason");
              reason.disabled = false;
              reason.value = "Đánh giá SHADOW sau khi kiểm định TEST đạt điều kiện.";
              reason.dispatchEvent(new Event("input", {bubbles: true}));
              select.value = "SHADOW";
              select.dispatchEvent(new Event("change", {bubbles: true}));
              document.querySelector("#scorer-deployment-form").dispatchEvent(
                new Event("submit", {bubbles: true, cancelable: true})
              );
              return {
                requests,
                submitDisabled: document.querySelector("#scorer-deployment-submit").disabled
              };
            }"""
        )
        assert submit_result == {"requests": [], "submitDisabled": True}
        expect(browser_page.locator("#scorer-deployment-submit")).to_be_disabled()
        expect(browser_page.get_by_role("alert")).to_be_hidden()
        assert service.facade.snapshot.mode is ScorerDeploymentMode.DISABLED
        assert service.facade.calls == []
    finally:
        _stop_app(server, thread, listener)
