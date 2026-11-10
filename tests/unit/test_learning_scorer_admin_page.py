from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from trading_bot.admin.learning_scoring_page_router import router
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
    ScorerDeploymentHealth,
    ScorerDeploymentMode,
)

WORKSPACE_ID = UUID("bba2a6ed-7b24-43d3-a297-2ac617be105f")


class _AuthService:
    async def issue_csrf_token(self, _cookie: str | None) -> str:
        return "scorer-page-csrf"


class _ScorerDeploymentFacade:
    def __init__(self, snapshot: ScorerDeploymentAdminSnapshot) -> None:
        self.snapshot = snapshot

    async def load_admin_scoring_deployment(self) -> ScorerDeploymentAdminSnapshot:
        return self.snapshot


class _ScorerDeploymentService:
    def __init__(self, snapshot: ScorerDeploymentAdminSnapshot) -> None:
        self.facade = _ScorerDeploymentFacade(snapshot)
        self.principals: list[Principal] = []

    def for_principal(self, principal: Principal) -> _ScorerDeploymentFacade:
        self.principals.append(principal)
        assert principal.workspace_id == WORKSPACE_ID
        return self.facade


class _ScorerReportReadModel:
    def __init__(self, summary: ScorerReportSummary | None) -> None:
        self.summary = summary
        self.workspaces: list[UUID] = []

    async def load_latest_summary(self, workspace_id: UUID) -> ScorerReportSummary | None:
        self.workspaces.append(workspace_id)
        return self.summary


class _ScorerScoreEventReadModel:
    def __init__(self, page: ScorerScoreEventPage) -> None:
        self.result = page
        self.calls: list[tuple[UUID, int]] = []

    async def load_page(self, workspace_id: UUID, *, page: int, page_size: int = 20):
        self.calls.append((workspace_id, page))
        return ScorerScoreEventPage(
            items=self.result.items,
            page=page,
            has_next=self.result.has_next,
        )


def _summary() -> ScorerReportSummary:
    return ScorerReportSummary(
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
        coverage=0.75,
        abstain_count=2,
        log_loss=0.41,
        brier_score=0.18,
        expected_calibration_error=0.08,
        false_veto_rate=0.25,
        candidate_retention=0.67,
        candidate_count=6,
        scored_candidate_count=4,
        approved_candidate_count=3,
    )


def _snapshot(
    mode: ScorerDeploymentMode = ScorerDeploymentMode.DISABLED,
) -> ScorerDeploymentAdminSnapshot:
    active = mode is not ScorerDeploymentMode.DISABLED
    return ScorerDeploymentAdminSnapshot(
        workspace_id=WORKSPACE_ID,
        mode=mode,
        deployment_event_id=uuid4() if active else None,
        deployment_revision=1 if active else 0,
        active_pointer_version=2 if active else 0,
        health=(
            ScorerDeploymentHealth.RIGHTS_RECHECK_REQUIRED
            if active
            else ScorerDeploymentHealth.DISABLED
        ),
        runtime_usable=False,
    )


def _client(
    *,
    roles: frozenset[Role] = frozenset({Role.OWNER}),
    mfa_verified_at: datetime | None = None,
    mfa_method: MfaMethod | None = MfaMethod.TOTP,
    snapshot: ScorerDeploymentAdminSnapshot | None = None,
    report_summary: ScorerReportSummary | None = None,
) -> tuple[TestClient, _ScorerDeploymentService, Principal]:
    principal = Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        roles=roles,
        mfa_verified_at=mfa_verified_at or datetime.now(UTC),
        mfa_method=mfa_method,
    )
    identity = AuthIdentity(principal=principal, email="reviewer@example.test")
    application = FastAPI()
    application.include_router(router)
    application.state.settings = AppSettings(app_env="test")
    application.state.templates = Jinja2Templates(
        directory=str(Path(__file__).parents[2] / "src" / "trading_bot" / "templates")
    )
    application.state.auth_service = _AuthService()
    service = _ScorerDeploymentService(snapshot or _snapshot())
    application.state.learning_scoring_deployment_service = service
    report_read_model = _ScorerReportReadModel(report_summary)
    application.state.learning_scoring_report_read_model = report_read_model
    score_event_read_model = _ScorerScoreEventReadModel(
        ScorerScoreEventPage(items=(), page=1, has_next=False)
    )
    application.state.learning_scoring_score_event_read_model = score_event_read_model
    application.dependency_overrides[current_identity] = lambda: identity
    return TestClient(application), service, principal


def test_scoring_admin_page_is_no_store_metadata_only_and_keeps_modes_locked() -> None:
    client, service, principal = _client(snapshot=_snapshot())

    response = client.get("/admin/learning/scoring")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "Điều phối chấm điểm mô hình" in response.text
    assert "Đang tắt" in response.text
    assert "quyền runtime chưa được xác minh" in response.text.lower()
    assert "chỉ quan sát" in response.text.lower()
    assert "khóa" in response.text.lower()
    assert "data-feature-value" not in response.text
    assert "feature_schema_hash" not in response.text
    assert "test_payload" not in response.text.lower()
    assert "data-score-event" not in response.text
    assert "Chưa có báo cáo scorer TEST đã lưu" in response.text
    assert "Chưa có sự kiện điểm chấm trong không gian làm việc này." in response.text
    assert 'name="mode"' not in response.text
    assert 'name="actor' not in response.text
    assert service.principals == [principal]


def test_scoring_admin_page_only_shows_cas_disable_for_recent_totp_approver() -> None:
    client, _service, _principal = _client(
        roles=frozenset({Role.ANALYST_REVIEWER}),
        snapshot=_snapshot(ScorerDeploymentMode.SHADOW),
    )

    response = client.get("/admin/learning/scoring")

    assert response.status_code == 200
    assert 'id="scorer-disable-form"' in response.text
    assert 'data-expected-pointer-version="2"' in response.text
    assert 'data-expected-deployment-revision="1"' in response.text
    assert 'data-csrf-token="scorer-page-csrf"' in response.text
    assert "Tắt chấm điểm" in response.text
    assert "rights" not in response.text.lower() or "quyền" in response.text.lower()


def test_scoring_admin_page_keeps_ordinary_viewer_read_only() -> None:
    client, _service, _principal = _client(
        roles=frozenset({Role.ANALYST_REVIEWER}),
        mfa_method=None,
        snapshot=_snapshot(ScorerDeploymentMode.SHADOW),
    )

    response = client.get("/admin/learning/scoring")

    assert response.status_code == 200
    assert 'id="scorer-disable-form"' not in response.text
    assert "cần xác thực totp gần đây" in response.text.lower()


def test_scoring_admin_page_hides_mutation_without_recent_totp() -> None:
    client, _service, _principal = _client(
        roles=frozenset({Role.ANALYST_REVIEWER}),
        mfa_verified_at=datetime.now(UTC) - timedelta(minutes=20),
        snapshot=_snapshot(ScorerDeploymentMode.SHADOW),
    )

    response = client.get("/admin/learning/scoring")

    assert response.status_code == 200
    assert 'id="scorer-disable-form"' not in response.text
    assert "xác thực totp gần đây" in response.text.lower()


def test_scoring_admin_page_shows_only_persisted_aggregate_report_metadata() -> None:
    client, _service, _principal = _client(report_summary=_summary())

    response = client.get("/admin/learning/scoring")

    assert response.status_code == 200
    assert "Báo cáo scorer TEST gần nhất" in response.text
    assert "Chưa kết luận" in response.text
    assert "Độ phủ chấm điểm" in response.text
    assert "75.0%" in response.text
    assert "Sai số hiệu chuẩn kỳ vọng (ECE)" in response.text
    assert "8.0%" in response.text
    assert "3 pnl âm · 2 pnl trung tính · 4 pnl dương" in response.text.lower()
    assert "Số cơ hội được duyệt" in response.text
    assert "aaaaaaaaaaaaaaaa…" in response.text
    assert "bbbbbbbbbbbbbbbb…" in response.text
    assert "ffffffffffffffff…" in response.text
    assert "candidate_completeness_verified" not in response.text
    assert "sample_ids" not in response.text
    assert "test_payload" not in response.text.lower()
    assert "data-score-event" not in response.text


def test_scoring_admin_page_shows_sanitized_event_history_and_page_navigation() -> None:
    client, _service, _principal = _client(report_summary=_summary())
    event_read_model = client.app.state.learning_scoring_score_event_read_model
    event_read_model.result = ScorerScoreEventPage(
        items=(
            ScorerScoreEventSummary(
                event_id=UUID("f583002a-80ea-414e-b3d8-6d09f230e8bb"),
                created_at=datetime(2026, 11, 10, 8, 40, tzinfo=UTC),
                mode="SHADOW",
                decision="VETO",
                reason_code="LOW_CONFIDENCE",
                probability=0.21,
                confidence=0.72,
                uncertainty=0.28,
            ),
        ),
        page=2,
        has_next=False,
    )

    response = client.get("/admin/learning/scoring?score_event_page=2")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "Lịch sử điểm chấm" in response.text
    assert "Chỉ quan sát" in response.text
    assert "Từ chối ứng viên" in response.text
    assert "LOW_CONFIDENCE" in response.text
    assert "21.0%" in response.text
    assert 'href="/admin/learning/scoring?score_event_page=1"' in response.text
    assert "event_payload" not in response.text
    assert "canonical_event" not in response.text
    assert event_read_model.calls == [(WORKSPACE_ID, 2)]


def test_scoring_admin_page_requires_learning_model_read_permission() -> None:
    client, service, _principal = _client(roles=frozenset({Role.OPERATOR}))

    response = client.get("/admin/learning/scoring")

    assert response.status_code == 403
    assert service.principals == []
