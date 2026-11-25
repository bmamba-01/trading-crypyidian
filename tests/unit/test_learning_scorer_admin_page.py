from __future__ import annotations

import re
from dataclasses import replace
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
    ScorerPaperEntryBlockAuditSummary,
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


class _PaperEntryBlockFacade:
    def __init__(self, eligible_decision_ids: frozenset[UUID]) -> None:
        self.eligible_decision_ids_result = eligible_decision_ids
        self.calls: list[tuple[UUID, ...]] = []

    async def eligible_decision_ids(self, decision_ids):
        values = tuple(decision_ids)
        self.calls.append(values)
        return frozenset(value for value in values if value in self.eligible_decision_ids_result)


class _PaperEntryBlockService:
    def __init__(self, eligible_decision_ids: frozenset[UUID]) -> None:
        self.facade = _PaperEntryBlockFacade(eligible_decision_ids)
        self.principals: list[Principal] = []

    def for_principal(self, principal: Principal) -> _PaperEntryBlockFacade:
        self.principals.append(principal)
        return self.facade


def _summary() -> ScorerReportSummary:
    return ScorerReportSummary(
        evaluation_report_id=UUID("d1f2a6cc-d185-4a16-949b-844688cd4edb"),
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
    active_modes_enabled: bool = False,
    score_event_page: ScorerScoreEventPage | None = None,
    eligible_decision_ids: frozenset[UUID] = frozenset(),
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
    application.state.settings = AppSettings(
        app_env="test",
        scorer_active_modes_enabled=active_modes_enabled,
    )
    application.state.templates = Jinja2Templates(
        directory=str(Path(__file__).parents[2] / "src" / "trading_bot" / "templates")
    )
    application.state.auth_service = _AuthService()
    service = _ScorerDeploymentService(snapshot or _snapshot())
    application.state.learning_scoring_deployment_service = service
    report_read_model = _ScorerReportReadModel(report_summary)
    application.state.learning_scoring_report_read_model = report_read_model
    score_event_read_model = _ScorerScoreEventReadModel(
        score_event_page or ScorerScoreEventPage(items=(), page=1, has_next=False)
    )
    application.state.learning_scoring_score_event_read_model = score_event_read_model
    application.state.learning_paper_entry_block_service = _PaperEntryBlockService(
        eligible_decision_ids
    )
    application.dependency_overrides[current_identity] = lambda: identity
    return TestClient(application), service, principal


def test_scoring_admin_page_is_no_store_metadata_only_and_keeps_modes_locked() -> None:
    client, service, principal = _client(snapshot=_snapshot())

    response = client.get("/admin/learning/scoring")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "Điều phối chấm điểm mô hình" in response.text
    assert "Đang tắt" in response.text
    assert "chưa đủ điều kiện báo cáo và ủy quyền" in response.text.lower()
    assert "chỉ quan sát" in response.text.lower()
    assert "khóa" in response.text.lower()
    assert "data-feature-value" not in response.text
    assert "feature_schema_hash" not in response.text
    assert "test_payload" not in response.text.lower()
    assert "data-score-event" not in response.text
    assert "Chưa có báo cáo scorer TEST đã lưu" in response.text
    assert "Chưa có sự kiện điểm chấm trong không gian làm việc này." in response.text
    assert 'id="scorer-mode"' in response.text
    assert 'option value="SHADOW" disabled' in response.text
    assert 'option value="PAPER_GATE" disabled' in response.text
    assert "máy chủ sẽ xác minh lại quyền hiện hành" in response.text.lower()
    assert "PAPER_GATE vẫn đi qua các cổng RiskEngine thông thường" in response.text
    assert 'name="actor' not in response.text
    assert service.principals == [principal]


def test_scoring_admin_page_shows_legacy_report_without_confusion_matrix() -> None:
    legacy_summary = replace(
        _summary(), not_run_count=None, input_excluded_count=4, confusion_matrix=None
    )
    client, _, _ = _client(report_summary=legacy_summary)

    response = client.get("/admin/learning/scoring")

    assert response.status_code == 200
    assert "Chưa chạy hoặc bị loại (proof v2 gộp)" in response.text
    assert "Proof v2 gộp EVALUATED + NOT_RUN và INPUT_EXCLUDED" in response.text
    assert "Đầu vào bị loại (INPUT_EXCLUDED)" not in response.text
    assert "Ma trận nhầm lẫn chưa có trong báo cáo cũ." in response.text
    assert 'id="learning-scoring-confusion-matrix"' not in response.text


def test_scoring_admin_page_only_shows_cas_disable_for_recent_totp_approver() -> None:
    client, _service, _principal = _client(
        roles=frozenset({Role.ANALYST_REVIEWER}),
        snapshot=_snapshot(ScorerDeploymentMode.SHADOW),
    )

    response = client.get("/admin/learning/scoring")

    assert response.status_code == 200
    assert 'id="scorer-deployment-form"' in response.text
    assert 'name="mode"' in response.text
    assert 'data-expected-pointer-version="2"' in response.text
    assert 'data-expected-deployment-revision="1"' in response.text
    assert 'data-csrf-token="scorer-page-csrf"' in response.text
    assert 'value="DISABLED"' in response.text
    assert 'data-evaluation-report-id="' in response.text
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
    assert 'id="scorer-deployment-form"' in response.text
    assert re.search(r'<select[^>]*id="scorer-mode"[^>]*disabled', response.text)
    assert re.search(r'<button[^>]*id="scorer-deployment-submit"[^>]*disabled', response.text)
    assert "cần xác thực totp gần đây" in response.text.lower()


def test_scoring_admin_page_hides_mutation_without_recent_totp() -> None:
    client, _service, _principal = _client(
        roles=frozenset({Role.ANALYST_REVIEWER}),
        mfa_verified_at=datetime.now(UTC) - timedelta(minutes=20),
        snapshot=_snapshot(ScorerDeploymentMode.SHADOW),
    )

    response = client.get("/admin/learning/scoring")

    assert response.status_code == 200
    assert 'id="scorer-deployment-form"' in response.text
    assert re.search(r'<select[^>]*id="scorer-mode"[^>]*disabled', response.text)
    assert re.search(r'<button[^>]*id="scorer-deployment-submit"[^>]*disabled', response.text)
    assert "xác thực totp gần đây" in response.text.lower()


def test_scoring_admin_page_shows_policy_and_rights_gates_for_active_modes() -> None:
    client, _service, _principal = _client(report_summary=_summary())

    response = client.get("/admin/learning/scoring")

    assert response.status_code == 200
    assert 'option value="SHADOW" disabled' in response.text
    assert 'option value="PAPER_GATE" disabled' in response.text
    assert "INCONCLUSIVE" in response.text
    assert "candidate_completeness_verified" not in response.text
    assert "scored_candidate_coverage_verified" not in response.text
    assert "Báo cáo chưa xác minh đủ bằng chứng về mọi cơ hội TEST" in response.text
    assert "Báo cáo chưa xác minh đủ độ phủ cơ hội được chấm" in response.text
    assert "runtime_usable=false" in response.text


def test_accepted_shadow_report_shows_exact_id_and_leaves_rights_to_server_recheck() -> None:
    report_id = UUID("ad2d5800-f3e7-4e57-9818-3a64ae69a9a6")
    summary = replace(
        _summary(),
        status="ACCEPTED_SHADOW_ONLY",
        evaluation_report_id=report_id,
    )
    client, _service, _principal = _client(
        report_summary=summary,
        active_modes_enabled=True,
    )

    response = client.get("/admin/learning/scoring")

    assert response.status_code == 200
    assert 'option value="SHADOW" disabled' not in response.text
    assert f'data-evaluation-report-id="{report_id}"' in response.text
    assert f'name="evaluation_report_id" value="{report_id}"' in response.text
    assert "Có chế độ đạt điều kiện báo cáo và ủy quyền" in response.text
    assert "máy chủ sẽ xác minh lại quyền hiện hành" in response.text.lower()
    assert "health=DISABLED" in response.text
    assert "runtime_usable=false" in response.text


def test_accepted_shadow_report_does_not_unlock_active_mode_by_default() -> None:
    summary = replace(
        _summary(),
        status="ACCEPTED_SHADOW_ONLY",
        evaluation_report_id=uuid4(),
    )
    client, _service, _principal = _client(report_summary=summary)

    response = client.get("/admin/learning/scoring")

    assert response.status_code == 200
    assert 'option value="SHADOW" disabled' in response.text
    assert 'option value="PAPER_GATE" disabled' in response.text
    assert "đang khóa theo cấu hình triển khai" in response.text.lower()


def test_scoring_admin_page_shows_only_persisted_aggregate_report_metadata() -> None:
    client, _service, _principal = _client(report_summary=_summary())

    response = client.get("/admin/learning/scoring")

    assert response.status_code == 200
    assert "Báo cáo scorer TEST gần nhất" in response.text
    assert "Chưa kết luận" in response.text
    assert "Báo cáo INCONCLUSIVE" in response.text
    assert "FP lớp PnL dương" in response.text
    assert "FN lớp PnL dương" in response.text
    assert "False-veto theo hành động scorer" in response.text
    assert "Không tạo ứng viên (NO_CANDIDATE)" in response.text
    assert "Đầu vào bị loại (INPUT_EXCLUDED)" in response.text
    assert "NO_CANDIDATE không đồng nghĩa với tín hiệu bị bỏ lỡ, lệnh hoặc trade" in response.text
    assert "không tạo nhãn PnL" in response.text
    assert "1 / 4 mẫu PnL dương" in response.text
    assert "FP lớp PnL dương</dt><dd>1</dd>" in response.text
    assert "FN lớp PnL dương</dt><dd>1</dd>" in response.text
    assert "FN gồm cả trường hợp ABSTAIN" in response.text
    assert "VETO và ABSTAIN không đồng nghĩa với lỗi phân lớp" in response.text
    assert "75.0%" not in response.text
    assert "8.0%" not in response.text
    assert "67.0%" not in response.text
    assert "3 pnl âm · 2 pnl trung tính · 4 pnl dương" in response.text.lower()
    assert "Ma trận nhầm lẫn trên mẫu TEST" in response.text
    assert "Nhãn thực tế" in response.text
    assert "ABSTAIN" in response.text
    assert "không phải ma trận hành động paper" in response.text.lower()
    assert "class_predictions" not in response.text
    assert 'id="learning-scoring-confusion-matrix"' in response.text
    assert "Không tạo ứng viên (NO_CANDIDATE)" in response.text
    assert "Đầu vào bị loại (INPUT_EXCLUDED)" in response.text
    assert "Cơ hội chưa chạy strategy (NOT_RUN)" in response.text
    assert "NO_CANDIDATE không đồng nghĩa với tín hiệu bị bỏ lỡ, lệnh hoặc trade" in response.text
    assert "không tạo nhãn PnL" in response.text
    assert "Số cơ hội được duyệt" in response.text
    assert "aaaaaaaaaaaaaaaa…" in response.text
    assert "bbbbbbbbbbbbbbbb…" in response.text
    assert "ffffffffffffffff…" in response.text


def test_scoring_admin_page_does_not_split_legacy_v2_input_excluded_count() -> None:
    legacy_summary = replace(_summary(), not_run_count=None, input_excluded_count=4)
    client, _service, _principal = _client(report_summary=legacy_summary)

    response = client.get("/admin/learning/scoring")

    assert response.status_code == 200
    assert "Chưa chạy hoặc bị loại (proof v2 gộp)" in response.text
    assert "Proof v2 gộp EVALUATED + NOT_RUN và INPUT_EXCLUDED" in response.text
    assert "Chưa chạy hoặc bị loại (proof v2 gộp)</dt><dd>4</dd>" in response.text
    assert "Đầu vào bị loại (INPUT_EXCLUDED)" not in response.text
    assert "NOT_RUN)" not in response.text
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


def _paper_gate_approve_event(
    *,
    decision_id: UUID,
    mode: str = "PAPER_GATE",
    decision: str = "APPROVE",
    audit: ScorerPaperEntryBlockAuditSummary | None = None,
) -> ScorerScoreEventSummary:
    return ScorerScoreEventSummary(
        event_id=UUID("8f63e50f-0edc-4cb4-ae2b-6d1b2a414204"),
        created_at=datetime(2026, 11, 18, 8, 40, tzinfo=UTC),
        mode=mode,
        decision=decision,
        reason_code="LOW_CONFIDENCE" if decision == "VETO" else None,
        probability=0.21 if decision == "VETO" else 0.72,
        confidence=0.72,
        uncertainty=0.28,
        bot_id=uuid4(),
        bot_decision_id=decision_id,
        paper_entry_block=audit,
    )


def test_scoring_admin_page_shows_block_form_only_after_service_confirms_eligibility() -> None:
    decision_id = UUID("19273e49-7090-4873-b8ab-05e481e563e3")
    page = ScorerScoreEventPage(
        items=(_paper_gate_approve_event(decision_id=decision_id),),
        page=1,
        has_next=False,
    )
    client, _service, principal = _client(
        snapshot=_snapshot(ScorerDeploymentMode.PAPER_GATE),
        score_event_page=page,
        eligible_decision_ids=frozenset({decision_id}),
    )

    response = client.get("/admin/learning/scoring")

    assert response.status_code == 200
    assert (
        'action="/admin/learning/scoring/events/8f63e50f-0edc-4cb4-ae2b-6d1b2a414204/paper-block"'
    ) in response.text
    assert 'name="reason"' in response.text
    assert 'name="confirm"' in response.text
    assert "xác thực totp gần đây" in response.text.lower()
    block_service = client.app.state.learning_paper_entry_block_service
    assert block_service.principals == [principal]
    assert block_service.facade.calls == [(decision_id,)]


def test_scoring_admin_page_shows_operator_audit_separately_from_scorer_decision() -> None:
    audit = ScorerPaperEntryBlockAuditSummary(
        actor_user_id=UUID("8664a146-5248-437e-8a5a-13185811ea4d"),
        mfa_verified_at=datetime(2026, 11, 18, 8, 39, tzinfo=UTC),
        reason="Đã có thông tin sự kiện mới.",
        created_at=datetime(2026, 11, 18, 8, 40, tzinfo=UTC),
    )
    page = ScorerScoreEventPage(
        items=(
            _paper_gate_approve_event(
                decision_id=UUID("19273e49-7090-4873-b8ab-05e481e563e3"), audit=audit
            ),
        ),
        page=1,
        has_next=False,
    )
    client, _service, _principal = _client(
        snapshot=_snapshot(ScorerDeploymentMode.PAPER_GATE),
        score_event_page=page,
    )

    response = client.get("/admin/learning/scoring")

    assert response.status_code == 200
    assert "Duyệt ứng viên" in response.text
    assert "Entry PAPER bị chặn thủ công" in response.text
    assert "Đã có thông tin sự kiện mới." in response.text
    assert "8664a146-5248-43…" in response.text
    assert '/paper-block"' not in response.text
