"""Playwright kiểm trang hàng đợi training trên desktop và mobile."""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import uvicorn
from playwright.sync_api import Page, expect

from trading_bot.adapters.database.repositories.learning_training_admin import (
    LearningTrainingArtifactResult,
    LearningTrainingFoldOption,
    LearningTrainingJobSnapshot,
)
from trading_bot.app import create_app
from trading_bot.application.learning_training_admin import LearningTrainingSubmission
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings

pytest_plugins = ("tests.e2e.browser_fixtures",)

ROOT = Path(__file__).resolve().parents[2]
SCREENSHOTS = ROOT / "tests" / "e2e" / "evidence"
WORKSPACE_ID = UUID("bba2a6ed-7b24-43d3-a297-2ac617be105f")
MEMBERSHIP_ID = UUID("4c1fd40f-a99c-4a5c-a713-4ec54f33ddfd")
USER_ID = UUID("9b55d8b2-8f86-4ed7-9941-3310ec1d18db")
DATASET_ID = UUID("a24c4a55-7176-4bcc-9f50-297ed57d17e2")
FOLD_ID = "a" * 64
MANIFEST_HASH = "b" * 64
NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


def _fold() -> LearningTrainingFoldOption:
    return LearningTrainingFoldOption(
        normalized_dataset_id=DATASET_ID,
        manifest_hash=MANIFEST_HASH,
        split_policy_version="split-v2",
        fold_plan_hash="c" * 64,
        approval_hash="d" * 64,
        fold_id=FOLD_ID,
        training_samples=120,
        validation_samples=30,
        feature_count=8,
        profile_hash="e" * 64,
    )


def _job(*, cancel_requested: bool = False) -> LearningTrainingJobSnapshot:
    return LearningTrainingJobSnapshot(
        job_id=UUID("c52a6938-2165-4b48-98bb-03b6ed5c49e1"),
        attempt_id=UUID("12d3660c-6c65-44b3-99d0-b17e95d0ff57"),
        normalized_dataset_id=DATASET_ID,
        manifest_hash=MANIFEST_HASH,
        fold_plan_hash="c" * 64,
        fold_id=FOLD_ID,
        status="queued",
        stage="queued",
        progress=0,
        attempt_count=0,
        lease_generation=0,
        cancel_requested=cancel_requested,
        error_code=None,
        training_samples=120,
        validation_samples=30,
        feature_count=8,
        created_at=NOW,
        updated_at=NOW,
    )


class _TrainingFacade:
    def __init__(self) -> None:
        self.submissions: list[dict[str, object]] = []
        self.job: LearningTrainingJobSnapshot | None = None

    async def list_eligible_folds(self) -> tuple[LearningTrainingFoldOption, ...]:
        return (_fold(),)

    async def list_jobs(self) -> tuple[LearningTrainingJobSnapshot, ...]:
        return (self.job,) if self.job is not None else ()

    async def enqueue(
        self,
        *,
        normalized_dataset_id: UUID,
        fold_id: str,
        idempotency_key: UUID,
    ) -> LearningTrainingSubmission:
        self.submissions.append(
            {
                "normalized_dataset_id": normalized_dataset_id,
                "fold_id": fold_id,
                "idempotency_key": idempotency_key,
            }
        )
        self.job = _job()
        return LearningTrainingSubmission(
            attempt_id=self.job.attempt_id,
            job_id=self.job.job_id,
            normalized_dataset_id=normalized_dataset_id,
            manifest_hash=MANIFEST_HASH,
            fold_id=fold_id,
            trial_index=0,
            seed=0,
            status="queued",
            duplicate=False,
            error_code=None,
        )

    async def cancel(self, *, job_id: UUID) -> LearningTrainingJobSnapshot:
        assert self.job is not None
        assert job_id == self.job.job_id
        self.job = replace(self.job, cancel_requested=True, updated_at=NOW)
        return self.job


class _TrainingService:
    def __init__(self) -> None:
        self.facade = _TrainingFacade()

    def for_principal(self, principal: Principal) -> _TrainingFacade:
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
            mfa_verified_at=NOW,
        ),
        email="owner@example.test",
    )


def _serve_app() -> tuple[str, uvicorn.Server, threading.Thread, socket.socket, _TrainingService]:
    application = create_app(AppSettings(app_env="test"))
    application.dependency_overrides[current_identity] = lambda: _identity()
    application.state.auth_service = _AuthService()
    service = _TrainingService()
    application.state.learning_training_admin_service = service
    from tests.e2e.test_learning_model_registry_ui import _RegistryService

    application.state.learning_model_registry_service = _RegistryService()

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
        name="learning-training-playwright",
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.025)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=3)
        raise RuntimeError("Admin web chưa khởi động trong thời hạn E2E.")
    return f"http://127.0.0.1:{port}", server, thread, listener, service


def _stop_app(server: uvicorn.Server, thread: threading.Thread, listener: socket.socket) -> None:
    server.should_exit = True
    thread.join(timeout=5)
    listener.close()


def _assert_layout(page: Page, *, width: int, height: int) -> None:
    page.set_viewport_size({"width": width, "height": height})
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => resolve()))")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
        width,
        page.evaluate("({scroll:document.documentElement.scrollWidth,inner:innerWidth})"),
    )
    for button in page.get_by_role("button").all():
        if button.is_visible():
            bounds = button.bounding_box()
            assert bounds is not None, (width, bounds)
            assert bounds["height"] >= 44, (width, bounds)


def test_learning_training_admin_enqueues_cancels_and_is_responsive(browser_page: Page) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    app_url, server, thread, listener, service = _serve_app()
    try:
        browser_page.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith(app_url + "/") else route.abort()
            ),
        )
        response = browser_page.goto(f"{app_url}/admin/learning/training", wait_until="load")
        assert response is not None
        assert response.status == 200
        expect(browser_page.get_by_role("heading", name="Hàng đợi huấn luyện")).to_be_visible()
        expect(browser_page.get_by_text("Mô hình mới luôn ở trạng thái ABSTAIN")).to_be_visible()
        expect(browser_page.get_by_label("Lát dữ liệu đã duyệt")).to_be_visible()
        assert browser_page.locator("#learning-training").get_attribute("data-csrf-token") == (
            "playwright-local-csrf-token"
        )
        assert "Be Vietnam Pro" in browser_page.locator("body").evaluate(
            "element => getComputedStyle(element).fontFamily"
        )

        _assert_layout(browser_page, width=1280, height=900)

        with browser_page.expect_response(
            lambda item: (
                item.request.method == "POST"
                and item.url.endswith("/api/v1/learning-training/jobs")
            )
        ) as enqueue_response:
            browser_page.get_by_role("button", name="Đưa vào hàng đợi").click()
        assert enqueue_response.value.status == 201
        assert enqueue_response.value.request.headers["x-csrf-token"] == (
            "playwright-local-csrf-token"
        )
        request_body = enqueue_response.value.request.post_data_json
        assert set(request_body) == {"normalized_dataset_id", "fold_id"}
        assert request_body == {"normalized_dataset_id": str(DATASET_ID), "fold_id": FOLD_ID}
        assert UUID(enqueue_response.value.request.headers["idempotency-key"])
        assert len(service.facade.submissions) == 1
        expect(browser_page.get_by_text("Đã đưa tác vụ huấn luyện vào hàng đợi.")).to_be_visible()
        expect(browser_page.get_by_text("Mã tác vụ")).to_be_visible()
        assert browser_page.get_by_role("button", name="Yêu cầu dừng").is_visible()

        snapshot = browser_page.evaluate(
            """async () => {
              const response = await fetch('/api/v1/learning-training/jobs', {
                headers: {Accept: 'application/json'}, cache: 'no-store'
              });
              return {
                status: response.status,
                cacheControl: response.headers.get('cache-control'),
                body: await response.json()
              };
            }"""
        )
        assert snapshot["status"] == 200
        assert snapshot["cacheControl"] == "no-store"
        serialized_snapshot = str(snapshot["body"]).lower()
        assert all(
            term not in serialized_snapshot for term in ("features", "label_value", "holdout")
        )

        strict_rejection = browser_page.evaluate(
            """async ({datasetId, foldId}) => {
              const response = await fetch('/api/v1/learning-training/jobs', {
                method: 'POST',
                headers: {
                  'Content-Type': 'application/json',
                  'X-CSRF-Token': 'playwright-local-csrf-token',
                  'Idempotency-Key': crypto.randomUUID()
                },
                body: JSON.stringify({
                  normalized_dataset_id: datasetId,
                  fold_id: foldId,
                  training_samples: 999999,
                  actor_membership_id: 'caller-controlled'
                })
              });
              return response.status;
            }""",
            {"datasetId": str(DATASET_ID), "foldId": FOLD_ID},
        )
        assert strict_rejection == 422
        assert len(service.facade.submissions) == 1

        _assert_layout(browser_page, width=1280, height=900)
        browser_page.screenshot(path=str(SCREENSHOTS / "wp06-training-desktop.png"), full_page=True)
        _assert_layout(browser_page, width=390, height=844)
        browser_page.screenshot(path=str(SCREENSHOTS / "wp06-training-mobile.png"), full_page=True)
        with browser_page.expect_response(
            lambda item: item.request.method == "POST" and item.url.endswith("/cancel")
        ) as cancel_response:
            browser_page.get_by_role("button", name="Yêu cầu dừng").click()
        assert cancel_response.value.status == 200
        assert cancel_response.value.request.headers["x-csrf-token"] == (
            "playwright-local-csrf-token"
        )
        expect(
            browser_page.get_by_text("Đã gửi yêu cầu dừng; chờ tiến trình huấn luyện xác nhận.")
        ).to_be_visible()
        assert browser_page.get_by_role("button", name="Yêu cầu dừng").count() == 0
    finally:
        _stop_app(server, thread, listener)


def test_succeeded_training_hands_off_only_artifact_metadata_to_registry(
    browser_page: Page,
) -> None:
    artifact_id = UUID("99cb93f0-dc18-4525-8c15-06acb60810b2")
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    app_url, server, thread, listener, service = _serve_app()
    posts: list[str] = []
    browser_page.on(
        "request", lambda request: posts.append(request.url) if request.method == "POST" else None
    )
    try:
        service.facade.job = replace(
            _job(),
            status="succeeded",
            stage="complete",
            progress=100,
            artifact_result=LearningTrainingArtifactResult(
                artifact_id=artifact_id,
                artifact_hash="f" * 64,
                artifact_schema_version="cpu-artifact-v1",
                trainer_version="cpu-trainer-v1",
                approval_hash="d" * 64,
                batch_hash="e" * 64,
                feature_schema_hash="1" * 64,
                attempt_content_hash="2" * 64,
            ),
        )
        browser_page.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith(app_url + "/") else route.abort()
            ),
        )
        browser_page.set_viewport_size({"width": 1280, "height": 800})
        response = browser_page.goto(f"{app_url}/admin/learning/training", wait_until="load")
        assert response is not None
        assert response.status == 200
        card = browser_page.locator(".learning-training-card")
        expect(card.get_by_text(str(artifact_id), exact=True)).to_be_visible()
        expect(card.get_by_text("ABSTAIN · Chưa hiệu chuẩn", exact=True)).to_be_visible()
        expect(card.get_by_text("f" * 64, exact=True)).to_be_visible()
        expect(card.get_by_text("cpu-trainer-v1", exact=True)).to_be_visible()
        link = card.get_by_role("link", name="Mở biểu mẫu đăng ký artifact")
        assert link.get_attribute("href") == f"/admin/learning/models?artifact_id={artifact_id}"
        snapshot = browser_page.request.get(f"{app_url}/api/v1/learning-training/jobs")
        assert snapshot.status == 200
        assert snapshot.headers["cache-control"] == "no-store"
        metadata = snapshot.json()["jobs"][0]["artifact_result"]
        assert metadata["artifact_id"] == str(artifact_id)
        assert metadata["scorer_state"] == "abstain"
        assert metadata["calibrated"] is False
        assert all(
            term not in snapshot.text()
            for term in ("canonical_artifact", "weights", '"features"', '"samples"')
        )
        card.get_by_text("Nguồn gốc dữ liệu và lượt chạy", exact=True).click()
        expect(card.get_by_text(str(DATASET_ID), exact=True)).to_be_visible()
        expect(card.get_by_text("d" * 64, exact=True)).to_be_visible()
        _assert_layout(browser_page, width=1280, height=800)
        browser_page.screenshot(
            path=str(SCREENSHOTS / "wp06-training-artifact-desktop.png"), full_page=True
        )
        _assert_layout(browser_page, width=390, height=844)
        browser_page.screenshot(
            path=str(SCREENSHOTS / "wp06-training-artifact-mobile.png"), full_page=True
        )
        # Polling phải giữ cùng metadata và đường chuyển sang bước duyệt riêng.
        assert service.facade.job.artifact_result is not None
        service.facade.job = replace(
            service.facade.job,
            artifact_result=replace(service.facade.job.artifact_result, artifact_hash="3" * 64),
        )
        expect(card.get_by_text("3" * 64, exact=True)).to_be_visible()
        expect(card.get_by_text("ABSTAIN · Chưa hiệu chuẩn", exact=True)).to_be_visible()
        expect(card.get_by_text(str(DATASET_ID), exact=True)).to_be_visible()
        link.click()
        expect(browser_page.get_by_label("Mã artifact", exact=True)).to_have_value(str(artifact_id))
        expect(
            browser_page.get_by_role("button", name="Đăng ký artifact", exact=True)
        ).to_be_visible()
        assert posts == []
    finally:
        _stop_app(server, thread, listener)
