from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import get_auth_service, require_csrf
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings


def test_reviewer_cannot_create_risk_profile_through_direct_api_call() -> None:
    principal = Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=uuid4(),
        roles=frozenset({Role.ANALYST_REVIEWER}),
        mfa_verified_at=datetime.now(UTC),
    )
    app = create_app(AppSettings(app_env="test"))
    app.dependency_overrides[require_csrf] = lambda: AuthIdentity(
        principal=principal,
        email="reviewer@example.test",
    )
    app.dependency_overrides[get_auth_service] = lambda: cast(object, object())

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/risk-profiles",
            json={"name": "Denied", "max_risk_fraction": "0.01", "settings": {}},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "Không đủ quyền thay đổi hồ sơ rủi ro"
