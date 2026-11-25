"""Playwright desktop/mobile cho model card scorer PAPER."""

from __future__ import annotations

import importlib.util
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Page, expect

from trading_bot.app import create_app
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.config.settings import AppSettings
from trading_bot.domain.learning.scorer_model_card import (
    ScorerActionLatencySummary,
    ScorerLatencyStageSummary,
)
from trading_bot.domain.learning.scorer_report_summary import ScorerReportSummary
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentAdminSnapshot,
    ScorerDeploymentHealth,
    ScorerDeploymentMode,
)

pytest_plugins = ("tests.e2e.browser_fixtures",)

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "tests" / "e2e" / "evidence"
WORKSPACE_ID = UUID("6fd1db4b-63b0-49db-bb23-0b5d707bf261")
BOT_ID = UUID("41fa3126-70fd-41a0-a34d-f799b59872d4")
REPORT_ID = UUID("58e0fa54-85a0-42be-8a92-77d7c4faaf54")


def _report() -> ScorerReportSummary:
    return ScorerReportSummary(
        evaluation_report_id=REPORT_ID,
        created_at=datetime(2026, 11, 21, 9, tzinfo=UTC),
        evaluated_at=datetime(2026, 11, 21, 8, tzinfo=UTC),
        status="INCONCLUSIVE",
        report_hash="a" * 64,
        proof_hash="b" * 64,
        scorer_artifact_hash="c" * 64,
        base_artifact_hash="d" * 64,
        policy_hash="e" * 64,
        policy_version="wp06-scorer-policy-v1",
        manifest_hash="1" * 64,
        fold_plan_hash="2" * 64,
        fold_id="fold-01",
        vector_schema_version="scorer-vector-v2",
        vector_schema_hash="3" * 64,
        candidate_completeness_verified=False,
        scored_candidate_coverage_verified=False,
        test_sample_count=9,
        opportunity_count=12,
        class_support=((-1, 3), (0, 3), (1, 3)),
        confusion_matrix=None,
        coverage=0.0,
        abstain_count=9,
        log_loss=0.0,
        brier_score=0.0,
        expected_calibration_error=0.0,
        false_veto_count=0,
        false_veto_rate=0.0,
        candidate_retention=None,
        candidate_count=0,
        scored_candidate_count=0,
        approved_candidate_count=0,
        no_candidate_count=0,
        input_excluded_count=0,
        not_run_count=0,
    )


class _Identity:
    principal = Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        roles=frozenset({Role.ANALYST_REVIEWER}),
        mfa_verified_at=datetime.now(UTC),
        mfa_method=MfaMethod.TOTP,
    )
    email = "reviewer@example.test"


class _BotAdminService:
    async def list_bots(self, principal: Principal):
        assert principal.workspace_id == WORKSPACE_ID
        return [
            {
                "id": str(BOT_ID),
                "name": "Bot PAPER kiểm định",
                "account": {"mode": "PAPER"},
            }
        ]


class _ReportReadModel:
    async def load_report_summary(self, workspace_id: UUID, report_id: UUID):
        assert workspace_id == WORKSPACE_ID
        assert report_id == REPORT_ID
        return _report()


class _ModelCardReadModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail_latency = False

    async def load_action_latency(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_latency:
            from trading_bot.adapters.database.repositories.learning_scorer_model_card import (
                LearningScorerActionLatencyUnavailable,
            )

            raise LearningScorerActionLatencyUnavailable("source rights unavailable")
        as_of = datetime(2026, 11, 21, 12, tzinfo=UTC)
        window_code = kwargs["window_code"]
        window_duration = {
            "24h": timedelta(hours=24),
            "7d": timedelta(days=7),
            "30d": timedelta(days=30),
        }[window_code]
        return ScorerActionLatencySummary(
            bot_id=BOT_ID,
            window_code=window_code,
            as_of=as_of,
            window_start=as_of - window_duration,
            candidate_decision_count=130,
            candidate_no_intent_count=8,
            future_intent_count=2,
            stages=(
                ScorerLatencyStageSummary("signal_available_to_intent", 120, 0, 45, 160),
                ScorerLatencyStageSummary("evidence_persisted_to_intent", 118, 1, 20, 95),
                ScorerLatencyStageSummary("decision_persisted_to_intent", 115, 0, 12, 72),
            ),
        )


class _DeploymentFacade:
    async def load_admin_scoring_deployment(self) -> ScorerDeploymentAdminSnapshot:
        return ScorerDeploymentAdminSnapshot(
            workspace_id=WORKSPACE_ID,
            mode=ScorerDeploymentMode.SHADOW,
            deployment_event_id=uuid4(),
            deployment_revision=2,
            active_pointer_version=4,
            health=ScorerDeploymentHealth.RIGHTS_RECHECK_REQUIRED,
            runtime_usable=False,
        )


class _DeploymentService:
    def for_principal(self, principal: Principal) -> _DeploymentFacade:
        assert principal.workspace_id == WORKSPACE_ID
        return _DeploymentFacade()


def _serve_app() -> tuple[
    str,
    uvicorn.Server,
    threading.Thread,
    socket.socket,
    _ModelCardReadModel,
]:
    if importlib.util.find_spec("trading_bot.admin.learning_scorer_model_card_router") is None:
        raise AssertionError("Chưa có trang admin model card scorer.")
    application: FastAPI = create_app(AppSettings(app_env="test"))
    identity = _Identity()
    application.dependency_overrides[current_identity] = lambda: identity
    model_card = _ModelCardReadModel()
    application.state.bot_admin_service = _BotAdminService()
    application.state.learning_scoring_report_read_model = _ReportReadModel()
    application.state.learning_scorer_model_card_read_model = model_card
    application.state.learning_scoring_deployment_service = _DeploymentService()

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
        name="learning-scorer-model-card-playwright",
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.025)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=3)
        raise RuntimeError("Admin web chưa khởi động trong thời hạn E2E.")
    return f"http://127.0.0.1:{port}", server, thread, listener, model_card


def test_scorer_model_card_shows_safe_lineage_and_responsive_latency(browser_page: Page) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    app_url, server, thread, listener, model_card = _serve_app()
    try:
        browser_page.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith(app_url + "/") else route.abort()
            ),
        )
        response = browser_page.goto(
            f"{app_url}/admin/learning/model-card?report_id={REPORT_ID}&bot_id={BOT_ID}&window=7d",
            wait_until="load",
        )
        assert response is not None
        assert response.status == 200
        assert response.headers["cache-control"] == "no-store"
        expect(
            browser_page.get_by_role("heading", name="Thẻ thông tin bộ chấm điểm")
        ).to_be_visible()
        expect(
            browser_page.get_by_role("heading", name="Trạng thái vận hành bộ chấm điểm hiện tại")
        ).to_be_visible()
        expect(browser_page.get_by_text("Chỉ quan sát", exact=True)).to_be_visible()
        expect(browser_page.get_by_text("2 / 4", exact=True)).to_be_visible()
        expect(browser_page.locator("#model-card-inconclusive-note")).to_be_visible()
        expect(browser_page.locator("#model-card-inconclusive-note")).to_contain_text(
            "chưa kết luận về chất lượng dự báo"
        )
        expect(browser_page.get_by_text("Độ phủ chấm điểm", exact=True)).to_have_count(0)
        expect(
            browser_page.get_by_role("heading", name="Độ trễ tạo ý định lệnh mô phỏng")
        ).to_be_visible()
        expect(
            browser_page.get_by_text("Tín hiệu khả dụng → ý định lệnh", exact=True)
        ).to_be_visible()
        expect(
            browser_page.get_by_text("Bằng chứng đã ghi → ý định lệnh", exact=True)
        ).to_be_visible()
        expect(
            browser_page.get_by_text("Quyết định của bộ chấm điểm đã ghi → ý định lệnh", exact=True)
        ).to_be_visible()
        expect(browser_page.locator("body")).not_to_contain_text("OrderIntent")
        expect(browser_page.get_by_text("45 ms", exact=True)).to_be_visible()
        expect(
            browser_page.get_by_text("Chưa có ý định lệnh tại thời điểm chụp", exact=True)
        ).to_be_visible()
        expect(
            browser_page.get_by_text("Ý định lệnh có mốc thời gian ở tương lai", exact=True)
        ).to_be_visible()
        expect(browser_page.get_by_label("Bot PAPER (mô phỏng)")).to_have_value(str(BOT_ID))
        assert len(model_card.calls) == 1
        assert model_card.calls[0]["principal"].workspace_id == WORKSPACE_ID
        assert model_card.calls[0]["bot_id"] == BOT_ID
        assert model_card.calls[0]["report"].evaluation_report_id == REPORT_ID

        for viewport, file_name in (
            ({"width": 1440, "height": 1000}, "wp06-model-card-desktop.png"),
            ({"width": 375, "height": 812}, "wp06-model-card-mobile.png"),
        ):
            browser_page.set_viewport_size(viewport)
            browser_page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
            dimensions = browser_page.evaluate(
                "() => ({width: innerWidth, scroll: document.documentElement.scrollWidth})"
            )
            assert dimensions["scroll"] <= dimensions["width"], dimensions
            browser_page.screenshot(path=str(EVIDENCE / file_name), full_page=True)

        for window_code, window_label in (("24h", "24 giờ"), ("30d", "30 ngày")):
            browser_page.get_by_label("Khoảng thời gian").select_option(window_code)
            browser_page.get_by_role("button", name="Xem thống kê độ trễ").click()
            expect(browser_page.locator(".model-card-snapshot")).to_contain_text(
                f"Cửa sổ: {window_label}"
            )
            assert model_card.calls[-1]["window_code"] == window_code

        model_card.fail_latency = True
        denied_response = browser_page.goto(
            f"{app_url}/admin/learning/model-card?report_id={REPORT_ID}&bot_id={BOT_ID}&window=7d",
            wait_until="load",
        )
        assert denied_response is not None
        assert denied_response.status == 200
        expect(browser_page.locator(".model-card-unavailable")).to_be_visible()
        expect(browser_page.locator(".model-card-snapshot")).to_have_count(0)
        expect(browser_page.get_by_text("45 ms", exact=True)).to_have_count(0)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
