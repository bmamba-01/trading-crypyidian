from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from trading_bot.admin.learning_paper_performance_router import router
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import require_csrf
from trading_bot.auth.service import AuthIdentity


class _MarkedDatabaseError(SQLAlchemyError):
    def __init__(self, marker: str) -> None:
        super().__init__(marker)
        self.orig = SimpleNamespace(diag=SimpleNamespace(message_primary=marker))


class _PerformanceFacade:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def assess(self, **_kwargs):
        raise self.error


class _PerformanceService:
    def __init__(self, error: Exception) -> None:
        self.facade = _PerformanceFacade(error)

    def for_principal(self, _principal):
        return self.facade


def _client(error: Exception) -> TestClient:
    principal = Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=uuid4(),
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=None,
    )
    identity = AuthIdentity(principal=principal, email="owner@example.test")
    app = FastAPI()
    app.include_router(router)
    app.state.learning_paper_performance_service = _PerformanceService(error)
    app.dependency_overrides[require_csrf] = lambda: identity
    return TestClient(app)


def _assess(client: TestClient):
    return client.post(
        "/api/v1/learning/paper-performance/assess",
        json={"bot_id": str(uuid4())},
        headers={"Idempotency-Key": str(uuid4())},
    )


def test_paper_performance_business_rights_marker_is_a_conflict() -> None:
    response = _assess(
        _client(_MarkedDatabaseError("WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED"))
    )

    assert response.status_code == 409
    assert response.headers["Cache-Control"] == "no-store"
    assert "WP06_" not in response.text


def test_paper_performance_database_outage_is_unavailable() -> None:
    response = _assess(_client(SQLAlchemyError("database unavailable")))

    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert "database unavailable" not in response.text
