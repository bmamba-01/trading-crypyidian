"""Kiểm tra lịch sử registry chỉ đọc theo workspace đang xác thực."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings
from trading_bot.domain.learning.model_registry import (
    LearningModelActivePointer,
    LearningModelRegistryAction,
    LearningModelRegistryEntry,
    LearningModelRegistryEvent,
    LearningModelRegistryState,
)

NOW = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)


class _WorkspaceRegistry:
    def __init__(self, workspace_id, membership_id):
        self.workspace_id = workspace_id
        self.entry = LearningModelRegistryEntry(
            uuid4(),
            workspace_id,
            uuid4(),
            "a" * 64,
            "b" * 64,
            "c" * 64,
            "d" * 64,
            LearningModelRegistryState.DRAFT,
            1,
            None,
            None,
            membership_id,
            NOW,
        )
        self.events = (
            LearningModelRegistryEvent(
                uuid4(),
                workspace_id,
                self.entry.id,
                1,
                LearningModelRegistryAction.REGISTER,
                None,
                LearningModelRegistryState.DRAFT,
                membership_id,
                "v1",
                "registered",
                "Đã kiểm tra nguồn gốc <script>alert(1)</script>",
                uuid4(),
                None,
                None,
                None,
                None,
                None,
                NOW,
            ),
        )

    def for_principal(self, principal):
        assert principal.workspace_id == self.workspace_id
        return self

    async def list_entries(self):
        return (self.entry,)

    async def get_active_pointer(self):
        return LearningModelActivePointer(self.workspace_id, None, 0, None, NOW)

    async def list_events(self, registry_id):
        if registry_id != self.entry.id:
            raise LookupError("Không thuộc workspace hiện tại.")
        return self.events


class _AuthService:
    async def issue_csrf_token(self, _cookie):
        return "test-csrf"


def _client():
    app = create_app(AppSettings(app_env="test"))
    principal = Principal(uuid4(), uuid4(), uuid4(), frozenset({Role.OWNER}), NOW)
    service = _WorkspaceRegistry(principal.workspace_id, principal.membership_id)
    app.state.learning_model_registry_service = service
    app.state.auth_service = _AuthService()
    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal, "owner@example.test"
    )
    return TestClient(app), service, principal


def test_page_renders_scoped_event_metadata_and_escapes_reason():
    http, service, principal = _client()
    response = http.get("/admin/learning/models")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "Lịch sử thay đổi" in response.text
    assert "Chưa đăng ký → Bản nháp" in response.text
    assert str(principal.membership_id) in response.text
    assert "Chưa xác thực MFA" in response.text
    assert "12/11/2026 09:30 UTC" in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text
    api = http.get(f"/api/v1/learning-models/{service.entry.id}/events")
    assert api.status_code == 200
    assert api.json()[0]["actor_membership_id"] == str(principal.membership_id)
    assert not {"payload", "feature_values", "samples", "canonical_artifact"} & api.json()[0].keys()


def test_page_renders_empty_history_and_verified_mfa():
    http, service, _principal = _client()
    service.events = (replace(service.events[0], mfa_verified_at=NOW),)
    assert "Đã xác thực MFA" in http.get("/admin/learning/models").text
    service.events = ()
    assert "Chưa có sự kiện trong lịch sử." in http.get("/admin/learning/models").text


def test_event_api_denies_registry_outside_current_workspace():
    http, _service, _principal = _client()
    response = http.get(f"/api/v1/learning-models/{uuid4()}/events")
    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "no-store"


def test_artifact_link_only_prefills_registration_form():
    http, service, _principal = _client()
    artifact_id = uuid4()
    response = http.get(f"/admin/learning/models?artifact_id={artifact_id}")
    assert response.status_code == 200
    assert f'value="{artifact_id}"' in response.text
    assert service.entry.revision == 1
    assert http.get("/admin/learning/models?artifact_id=invalid").status_code == 422
