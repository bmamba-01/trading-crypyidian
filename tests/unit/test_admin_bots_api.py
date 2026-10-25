"""HTTP API bot: CSRF, ánh xạ lỗi, header Idempotency-Key và không lặp giá trị bí mật."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from trading_bot.admin.bot_service import BotAdminError
from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity, get_auth_service, require_csrf
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity
from trading_bot.config.settings import AppSettings

BOT_ID = uuid4()
DRAFT_ID = uuid4()
OVERLAY = {"schema_version": 1, "risk": {"max_positions": 1}}
MUTATIONS: list[tuple[str, str, dict[str, Any]]] = [
    ("POST", "/api/v1/bots", {"name": "paper-two", "max_notional": "5000"}),
    ("POST", f"/api/v1/bots/{BOT_ID}/config-drafts", {"overlay": OVERLAY}),
    (
        "PUT",
        f"/api/v1/bots/{BOT_ID}/config-drafts/{DRAFT_ID}",
        {"expected_revision": 1, "overlay": OVERLAY},
    ),
    ("POST", f"/api/v1/bots/{BOT_ID}/config-drafts/{DRAFT_ID}/validate", {}),
    (
        "POST",
        f"/api/v1/bots/{BOT_ID}/config-drafts/{DRAFT_ID}/publish",
        {"expected_revision": 1},
    ),
    (
        "POST",
        f"/api/v1/bots/{BOT_ID}/config-versions/1/rollback",
        {"expected_current_version_id": str(uuid4()), "reason": "rollback"},
    ),
]


def identity(role: Role = Role.OWNER) -> AuthIdentity:
    principal = Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=uuid4(),
        roles=frozenset({role}),
        mfa_verified_at=datetime.now(UTC),
    )
    return AuthIdentity(principal=principal, email="admin@example.test")


class RecordingService:
    """Service giả ghi tham số; có thể cấu hình lỗi nghiệp vụ trả về."""

    def __init__(self, error: BotAdminError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _handle(self, name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, kwargs))
        if self.error is not None:
            raise self.error
        return {"id": str(DRAFT_ID), "status": "DRAFT"}

    async def list_bots(self, principal: Principal) -> list[dict[str, Any]]:
        self.calls.append(("list_bots", {"principal": principal}))
        return [{"id": str(BOT_ID)}]

    async def get_bot(self, principal: Principal, **kwargs: Any) -> dict[str, Any]:
        return self._handle("get_bot", kwargs)

    async def create_bot(self, principal: Principal, **kwargs: Any) -> tuple[dict[str, Any], bool]:
        result = self._handle("create_bot", kwargs)
        return result, kwargs["name"] != "existing"

    async def create_draft(self, principal: Principal, **kwargs: Any) -> dict[str, Any]:
        return self._handle("create_draft", kwargs)

    async def update_draft(self, principal: Principal, **kwargs: Any) -> dict[str, Any]:
        return self._handle("update_draft", kwargs)

    async def validate_draft(self, principal: Principal, **kwargs: Any) -> dict[str, Any]:
        return self._handle("validate_draft", kwargs)

    async def publish(self, principal: Principal, **kwargs: Any) -> dict[str, Any]:
        return self._handle("publish", kwargs)

    async def rollback(self, principal: Principal, **kwargs: Any) -> dict[str, Any]:
        return self._handle("rollback", kwargs)


def client_with(service: RecordingService | None, *, csrf: bool = True) -> TestClient:
    app = create_app(AppSettings(app_env="test"))
    actor = identity()
    app.dependency_overrides[current_identity] = lambda: actor
    if csrf:
        app.dependency_overrides[require_csrf] = lambda: actor
    if service is not None:
        app.state.bot_admin_service = cast(object, service)
    return TestClient(app)


def test_list_bots_returns_no_store_payload() -> None:
    service = RecordingService()

    response = client_with(service).get("/api/v1/bots")

    assert response.status_code == 200
    assert response.json() == {"bots": [{"id": str(BOT_ID)}]}
    assert response.headers["Cache-Control"] == "no-store"


def test_service_unavailable_returns_503() -> None:
    response = client_with(None).get("/api/v1/bots")

    assert response.status_code == 503


@pytest.mark.parametrize(("method", "path", "body"), MUTATIONS)
def test_every_mutation_requires_csrf(method: str, path: str, body: dict[str, Any]) -> None:
    service = RecordingService()
    client = client_with(service, csrf=False)

    class AuthServiceStub:
        async def verify_csrf(self, cookie: str | None, token: str | None) -> None:
            del cookie, token
            raise AuthenticationFailure(403, "Thiếu mã CSRF")

    client.app.dependency_overrides[get_auth_service] = lambda: AuthServiceStub()  # type: ignore[attr-defined]

    response = client.request(method, path, json=body)

    assert response.status_code == 403
    assert service.calls == []


@pytest.mark.parametrize(("method", "path", "body"), MUTATIONS[1:])
@pytest.mark.parametrize("status", [403, 404, 409])
def test_business_errors_map_to_http_status(
    method: str, path: str, body: dict[str, Any], status: int
) -> None:
    service = RecordingService(BotAdminError(status, "Lỗi nghiệp vụ"))

    response = client_with(service).request(method, path, json=body)

    assert response.status_code == status
    assert response.json()["detail"] == "Lỗi nghiệp vụ"


def test_validation_errors_carry_field_details() -> None:
    errors = [{"field": "risk.max_positions", "message": "Bot không được nới giới hạn root."}]
    service = RecordingService(BotAdminError(422, "Overlay vi phạm root settings.", errors))

    response = client_with(service).post(
        f"/api/v1/bots/{BOT_ID}/config-drafts/{DRAFT_ID}/publish", json={"expected_revision": 1}
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "message": "Overlay vi phạm root settings.",
        "errors": errors,
    }


def test_idempotency_and_request_headers_are_validated_and_forwarded() -> None:
    service = RecordingService()
    client = client_with(service)
    request_id = uuid4()

    bad = client.post(
        f"/api/v1/bots/{BOT_ID}/config-drafts/{DRAFT_ID}/publish",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": "bad key!"},
    )
    good = client.post(
        f"/api/v1/bots/{BOT_ID}/config-drafts/{DRAFT_ID}/publish",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": "publish-key-0001", "X-Request-ID": str(request_id)},
    )

    assert bad.status_code == 400
    assert good.status_code == 200
    assert good.json() == {"version": {"id": str(DRAFT_ID), "status": "DRAFT"}}
    name, kwargs = service.calls[-1]
    assert name == "publish"
    assert kwargs == {
        "bot_id": BOT_ID,
        "draft_id": DRAFT_ID,
        "expected_revision": 1,
        "idempotency_key": "publish-key-0001",
        "request_id": request_id,
    }


def test_create_draft_parses_overlay_and_rejects_fields_outside_allowlist() -> None:
    service = RecordingService()
    client = client_with(service)

    created = client.post(
        f"/api/v1/bots/{BOT_ID}/config-drafts",
        json={"overlay": OVERLAY, "expected_parent_version_id": None},
    )
    venue = client.post(
        f"/api/v1/bots/{BOT_ID}/config-drafts",
        json={"overlay": {"schema_version": 1, "venue": "BINANCE_USDM"}},
    )
    workspace = client.post(
        f"/api/v1/bots/{BOT_ID}/config-drafts",
        json={"overlay": OVERLAY, "workspace_id": str(uuid4())},
    )

    assert created.status_code == 201
    assert service.calls[0][1]["overlay"].risk.max_positions == 1
    assert venue.status_code == 422
    assert workspace.status_code == 422
    assert len(service.calls) == 1


def test_secret_values_are_not_echoed_in_validation_errors() -> None:
    response = client_with(RecordingService()).post(
        f"/api/v1/bots/{BOT_ID}/config-drafts",
        json={
            "overlay": {
                "schema_version": 1,
                "strategy": {
                    "release_id": "h03-v1",
                    "parameters": {"api_secret": "DO_NOT_ECHO_THIS_VALUE"},
                },
            }
        },
    )

    assert response.status_code == 422
    assert "DO_NOT_ECHO_THIS_VALUE" not in response.text


def test_create_bot_returns_201_then_200_on_natural_key_replay() -> None:
    service = RecordingService()
    client = client_with(service)

    created = client.post("/api/v1/bots", json={"name": "paper-two", "max_notional": "5000"})
    replay = client.post("/api/v1/bots", json={"name": "existing", "max_notional": "5000"})
    floating = client.post("/api/v1/bots", json={"name": "paper-two", "max_notional": 5000.5})

    assert created.status_code == 201
    assert replay.status_code == 200
    assert floating.status_code == 422
    assert str(service.calls[0][1]["max_notional"]) == "5000"


def test_rollback_and_update_forward_typed_arguments() -> None:
    service = RecordingService()
    client = client_with(service)
    current = uuid4()

    client.post(
        f"/api/v1/bots/{BOT_ID}/config-versions/3/rollback",
        json={"expected_current_version_id": str(current), "reason": "về v3"},
    )
    client.put(
        f"/api/v1/bots/{BOT_ID}/config-drafts/{DRAFT_ID}",
        json={"expected_revision": 2, "overlay": OVERLAY},
    )

    rollback = service.calls[0][1]
    assert rollback["version"] == 3
    assert rollback["expected_current_version_id"] == current
    assert rollback["reason"] == "về v3"
    update = service.calls[1][1]
    assert update["expected_revision"] == 2
    assert isinstance(update["draft_id"], UUID)
