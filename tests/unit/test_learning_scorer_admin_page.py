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
