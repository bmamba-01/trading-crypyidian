from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity
from trading_bot.common.http_security import SecurityHeadersMiddleware, security_headers
from trading_bot.config.settings import AppSettings

try:
    from trading_bot.admin.learning_paper_entry_block_router import router
except ImportError:
    router = APIRouter()

SCORE_EVENT_ID = uuid4()
BOT_DECISION_ID = uuid4()


class _AuthService:
    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid
        self.tokens: list[str | None] = []

    async def verify_csrf(self, _cookie: str | None, token: str | None) -> None:
        self.tokens.append(token)
        if not self.valid or token != "csrf-valid":
            raise AuthenticationFailure(403, "Mã CSRF không hợp lệ.")


class _ScoreEventReadModel:
    def __init__(self, decision_id) -> None:
        self.decision_id = decision_id
        self.calls = []

    async def get_blockable_decision_id(self, workspace_id, score_event_id):
        self.calls.append((workspace_id, score_event_id))
        return self.decision_id


class _PaperEntryBlockFacade:
    def __init__(self, event=None, error: Exception | None = None) -> None:
        self.event = event
        self.error = error
        self.calls = []

    async def block(self, *, bot_decision_id, reason, idempotency_key):
        self.calls.append((bot_decision_id, reason, idempotency_key))
        if self.error is not None:
            raise self.error
        return self.event


class _PaperEntryBlockService:
    def __init__(self, facade: _PaperEntryBlockFacade) -> None:
        self.facade = facade
        self.principals = []

    def for_principal(self, principal: Principal):
        self.principals.append(principal)
        return self.facade


def _client(
    *,
    roles: frozenset[Role] = frozenset({Role.OWNER}),
    csrf_valid: bool = True,
    error: Exception | None = None,
) -> tuple[TestClient, Principal, _PaperEntryBlockFacade, _ScoreEventReadModel, _AuthService]:
    principal = Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=uuid4(),
        roles=roles,
        mfa_verified_at=datetime.now(UTC),
        mfa_method=MfaMethod.TOTP,
    )
    identity = AuthIdentity(principal=principal, email="owner@example.test")
    facade = _PaperEntryBlockFacade(
        event=SimpleNamespace(score_event_id=SCORE_EVENT_ID, bot_decision_id=BOT_DECISION_ID),
        error=error,
    )
    service = _PaperEntryBlockService(facade)
    read_model = _ScoreEventReadModel(BOT_DECISION_ID)
    auth_service = _AuthService(valid=csrf_valid)
    application = FastAPI()
    application.include_router(router)
    application.state.settings = AppSettings(_env_file=None, app_env="test")
    application.add_middleware(
        SecurityHeadersMiddleware,
        headers=security_headers(application.state.settings),
    )
    application.state.auth_service = auth_service
    application.state.learning_paper_entry_block_service = service
    application.state.learning_scoring_score_event_read_model = read_model
    application.dependency_overrides[current_identity] = lambda: identity
    return TestClient(application), principal, facade, read_model, auth_service


def _post(client: TestClient, *, token: str = "csrf-valid", confirm: bool = True):
    return client.post(
        f"/admin/learning/scoring/events/{SCORE_EVENT_ID}/paper-block",
        json={"reason": "Kiểm tra lại tín hiệu", "confirm": confirm},
        headers={"X-CSRF-Token": token, "Idempotency-Key": str(uuid4())},
    )


def test_admin_paper_entry_block_route_scopes_score_event_and_calls_service() -> None:
    client, principal, facade, read_model, auth_service = _client()
    idempotency_key = uuid4()
    response = client.post(
        f"/admin/learning/scoring/events/{SCORE_EVENT_ID}/paper-block",
        json={"reason": "  Kiểm tra lại tín hiệu  ", "confirm": True},
        headers={
            "X-CSRF-Token": "csrf-valid",
            "Idempotency-Key": str(idempotency_key),
        },
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["status"] == "blocked"
    assert read_model.calls == [(principal.workspace_id, SCORE_EVENT_ID)]
    assert facade.calls == [(BOT_DECISION_ID, "  Kiểm tra lại tín hiệu  ", idempotency_key)]
    assert auth_service.tokens == ["csrf-valid"]


def test_admin_paper_entry_block_route_rejects_csrf_before_read_or_service() -> None:
    client, _principal, facade, read_model, _auth = _client(csrf_valid=False)

    response = _post(client)

    assert response.status_code == 403
    assert response.headers["Cache-Control"] == "no-store"
    assert read_model.calls == []
    assert facade.calls == []


def test_admin_paper_entry_block_route_requires_specialized_permission() -> None:
    client, _principal, facade, read_model, _auth = _client(
        roles=frozenset({Role.ANALYST_REVIEWER})
    )

    response = _post(client)

    assert response.status_code == 403
    assert read_model.calls == []
    assert facade.calls == []


def test_admin_paper_entry_block_route_requires_explicit_confirmation() -> None:
    client, _principal, facade, read_model, _auth = _client()

    response = _post(client, confirm=False)

    assert response.status_code == 422
    assert read_model.calls == []
    assert facade.calls == []


def test_admin_paper_entry_block_route_maps_late_or_stale_target_to_conflict() -> None:
    client, _principal, facade, _read_model, _auth = _client(
        error=ValueError("Target scorer của quyết định không còn hiệu lực.")
    )

    response = _post(client)

    assert response.status_code == 409
    assert response.headers["Cache-Control"] == "no-store"
    assert "Target scorer" not in response.text
    assert "không còn đủ điều kiện" in response.json()["detail"].lower()
    assert len(facade.calls) == 1
