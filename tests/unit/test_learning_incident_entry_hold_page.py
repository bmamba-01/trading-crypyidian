"""Kiểm quyền và projection tiếng Việt của trang incident hold."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from tests.unit.test_learning_incident_entry_hold import _hold

from trading_bot.app import create_app
from trading_bot.application.learning_incident_entry_hold import LearningIncidentHoldCandidate
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings

WORKSPACE_ID = UUID(int=1201)
MEMBERSHIP_ID = UUID(int=1202)
USER_ID = UUID(int=1203)


def _identity(role: Role) -> AuthIdentity:
    return AuthIdentity(
        principal=Principal(
            user_id=USER_ID,
            membership_id=MEMBERSHIP_ID,
            workspace_id=WORKSPACE_ID,
            roles=frozenset({role}),
            mfa_verified_at=datetime.now(UTC),
            mfa_method=MfaMethod.TOTP,
        ),
        email="reviewer@example.test",
    )


class _HoldFacade:
    def __init__(self, *, candidates=(), events=()) -> None:
        self.candidates = tuple(candidates)
        self.events = tuple(events)
        self.calls: list[tuple[str, int]] = []

    async def list_actionable_proposals(self, *, limit: int = 20):
        self.calls.append(("candidates", limit))
        return self.candidates[:limit]

    async def list_history(self, *, limit: int = 200):
        self.calls.append(("history", limit))
        return self.events[:limit]

    async def list_open_holds(self, *, limit: int = 100):
        self.calls.append(("open_holds", limit))
        return tuple(event for event in self.events if event.action.value == "HOLD")[:limit]


class _HoldService:
    def __init__(self, facade: _HoldFacade) -> None:
        self.facade = facade
        self.principals: list[Principal] = []

    def for_principal(self, principal: Principal) -> _HoldFacade:
        self.principals.append(principal)
        return self.facade


class _AuthService:
    async def issue_csrf_token(self, _cookie: str | None) -> str:
        return "csrf-fixture-token"


def _client(role: Role, *, candidates=(), events=()):
    app = create_app(AppSettings(app_env="test"))
    principal_identity = _identity(role)
    app.dependency_overrides[current_identity] = lambda: principal_identity
    facade = _HoldFacade(candidates=candidates, events=events)
    service = _HoldService(facade)
    app.state.learning_incident_entry_hold_service = service
    app.state.learning_observations_admin_service = None
    app.state.auth_service = _AuthService()
    return TestClient(app), service, facade, principal_identity


def test_risk_admin_sees_incident_only_panel_and_current_action_metadata() -> None:
    event = _hold()
    candidate = LearningIncidentHoldCandidate(
        proposal_id=uuid4(),
        assessment_id=uuid4(),
        assessment_hash="a" * 64,
        reason_codes=("coverage_below_threshold",),
        created_at=datetime.now(UTC),
        expected_revision=0,
        expected_deployment_revision=4,
        expected_active_pointer_version=3,
    )
    client, service, facade, identity = _client(
        Role.RISK_ADMIN, candidates=(candidate,), events=(event,)
    )

    response = client.get("/admin/learning/observations")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Xác nhận chặn lệnh vào" in response.text
    assert "Tháo chặn lệnh vào" in response.text
    assert str(candidate.assessment_id) in response.text
    assert str(candidate.proposal_id) in response.text
    assert "feature vector" not in response.text.lower()
    assert "csrf-fixture-token" in response.text
    assert "Quan sát gần đây" not in response.text
    assert service.principals == [identity.principal]
    assert facade.calls == [("history", 200), ("open_holds", 100), ("candidates", 20)]


def test_principal_without_observation_or_incident_permission_is_denied() -> None:
    client, service, facade, _identity_value = _client(Role.OPERATOR)

    response = client.get("/admin/learning/observations")

    assert response.status_code == 403
    assert service.principals == []
    assert facade.calls == []
