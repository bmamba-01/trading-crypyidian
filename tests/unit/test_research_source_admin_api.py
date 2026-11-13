"""Kiểm API quản trị policy nguồn ở ranh giới xác thực và payload."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_bot.admin.research_source_router import router as research_source_router
from trading_bot.app import create_app
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings
from trading_bot.domain.research.source_registry import SourceBudget, SourceRegistryEntry

SOURCE = SourceRegistryEntry.discover(
    source_id="calendar",
    publisher="Publisher",
    endpoint_url="https://example.org/calendar",
    access_method="GET",
    auth_scope="public",
    budget=SourceBudget(timedelta(hours=1), 10, 1000, Decimal("1"), "USD"),
    actor_id=str(uuid4()),
    at=datetime.now(UTC),
    reason="Candidate",
)


def identity(role: Role = Role.OWNER) -> AuthIdentity:
    return AuthIdentity(
        principal=Principal(uuid4(), uuid4(), uuid4(), frozenset({role}), datetime.now(UTC)),
        email="owner@example.test",
    )


class RecordingFacade:
    def __init__(self):
        self.calls = []
        self.error = None

    async def list_sources(self):
        self.calls.append(("list",))
        if self.error:
            raise self.error
        return (SOURCE,)

    async def get_source(self, source_id):
        self.calls.append(("get", source_id))
        if self.error:
            raise self.error
        return (SOURCE, None) if source_id == SOURCE.source_id else None

    async def create_source(self, command, key):
        self.calls.append(("create", command, key))
        if self.error:
            raise self.error
        return SOURCE.current

    async def submit_pending(self, source_id, command, key):
        self.calls.append(("submit", source_id, command, key))
        if self.error:
            raise self.error
        return SOURCE.current

    async def change_disabled_state(self, source_id, state, reason, key):
        self.calls.append(("disable", source_id, state, reason, key))
        if self.error:
            raise self.error
        return SOURCE.current

    async def approve_pending(self, source_id, pending_id, command, key):
        self.calls.append(("approve", source_id, pending_id, command, key))
        if self.error:
            raise self.error
        return SOURCE.current


class RecordingService:
    def __init__(self):
        self.facade = RecordingFacade()
        self.principals = []

    def for_principal(self, principal):
        self.principals.append(principal)
        return self.facade


def client(service=None, *, actor=None, csrf=True):
    app = create_app(AppSettings(app_env="test"))
    service = service or RecordingService()
    app.state.source_policy_admin_service = service
    actor = actor or identity()
    app.dependency_overrides[current_identity] = lambda: actor
    if csrf:
        app.dependency_overrides[require_csrf] = lambda: actor
    return TestClient(app), service, actor


def bare_client(service=None, *, actor=None):
    app = FastAPI()
    app.include_router(research_source_router)
    service = service or RecordingService()
    app.state.source_policy_admin_service = service
    actor = actor or identity()
    app.dependency_overrides[current_identity] = lambda: actor
    app.dependency_overrides[require_csrf] = lambda: actor
    return TestClient(app), service


def create_payload():
    return {
        "source_id": "calendar",
        "publisher": "Publisher",
        "endpoint_url": "https://example.org/calendar",
        "access_method": "GET",
        "auth_scope": "public",
        "reason": "Candidate",
        "budget": {
            "window_seconds": 3600,
            "max_requests": 10,
            "max_bytes": 1000,
            "max_cost": "1",
            "cost_unit": "USD",
        },
    }


def test_list_detail_no_store_and_principal_scope():
    http, service, actor = client()
    listed = http.get("/api/v1/research-sources")
    detail = http.get("/api/v1/research-sources/calendar")
    assert listed.status_code == detail.status_code == 200
    assert listed.headers["Cache-Control"] == detail.headers["Cache-Control"] == "no-store"
    assert listed.json()[0]["source_id"] == "calendar"
    assert detail.json()["revisions"][0]["state"] == "discovered"
    assert service.principals == [actor.principal, actor.principal]
    missing = http.get("/api/v1/research-sources/missing")
    assert missing.status_code == 404
    assert missing.headers["Cache-Control"] == "no-store"


def test_mutation_requires_uuid_key_and_rejects_identity_injection_and_verified():
    http, service, _ = client()
    path = "/api/v1/research-sources"
    body = create_payload()
    assert http.post(path, json=body).status_code == 422
    assert http.post(path, headers={"Idempotency-Key": "bad"}, json=body).status_code == 422
    for field in ("workspace_id", "actor_id", "approved_by", "reviewer_id"):
        assert (
            http.post(
                path, headers={"Idempotency-Key": str(uuid4())}, json=body | {field: str(uuid4())}
            ).status_code
            == 422
        )
    assert (
        http.post(
            path + "/calendar/revisions",
            headers={"Idempotency-Key": str(uuid4())},
            json={"state": "verified", "reason": "Direct"},
        ).status_code
        == 422
    )
    numeric_budget = create_payload()
    numeric_budget["budget"]["max_cost"] = 0.1
    assert (
        http.post(path, headers={"Idempotency-Key": str(uuid4())}, json=numeric_budget).status_code
        == 422
    )
    assert service.facade.calls == []


def test_mutation_dispatch_and_error_mapping():
    http, service, actor = client()
    key = str(uuid4())
    headers = {"Idempotency-Key": key}
    assert (
        http.post("/api/v1/research-sources", headers=headers, json=create_payload()).status_code
        == 201
    )
    assert service.facade.calls[-1][0] == "create"
    assert service.principals[-1] is actor.principal
    path = "/api/v1/research-sources/calendar/revisions"
    assert (
        http.post(
            path,
            headers=headers,
            json=pending_payload() | {"budget": create_payload()["budget"]},
        ).status_code
        == 201
    )
    assert service.facade.calls[-1][0] == "submit"
    assert service.facade.calls[-1][2].budget.max_requests == 10
    assert (
        http.post(path, headers=headers, json={"state": "blocked", "reason": "Blocked"}).status_code
        == 201
    )
    assert service.facade.calls[-1][0] == "disable"
    assert (
        http.post(
            path + "/pending/approve",
            headers=headers,
            json={
                "allowed_purposes": ["metadata"],
                "valid_until": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "reason": "Approved",
            },
        ).status_code
        == 201
    )
    assert service.facade.calls[-1][0] == "approve"
    for error, expected in (
        (PermissionError("secret"), 403),
        (LookupError("secret"), 404),
        (ValueError("secret"), 409),
    ):
        service.facade.error = error
        response = http.post(path, headers=headers, json={"state": "blocked", "reason": "Blocked"})
        assert response.status_code == expected
        assert "secret" not in response.text


def test_authenticated_csrf_and_role_denial():
    http, service, _ = client(actor=identity(Role.OPERATOR))
    denied = http.get("/api/v1/research-sources")
    assert denied.status_code == 403
    assert denied.headers["Cache-Control"] == "no-store"
    assert service.facade.calls == []
    app = create_app(AppSettings(app_env="test"))
    app.state.source_policy_admin_service = service
    assert TestClient(app).get("/api/v1/research-sources").status_code == 503
    http, _, _ = client(csrf=False)
    assert http.post(
        "/api/v1/research-sources", headers={"Idempotency-Key": str(uuid4())}, json=create_payload()
    ).status_code in (401, 403, 503)


def test_composition_admin_web_only_and_no_ingestion_factory():
    sessions = async_sessionmaker()
    admin = create_app(
        AppSettings(app_env="test", process_role="admin-web"), session_factory=sessions
    )
    with TestClient(admin):
        service = admin.state.source_policy_admin_service
        assert service is not None
        assert service._repository._observer._quota_sessions is None
    worker_settings = (
        AppSettings(
            app_env="test", process_role="ingestion-worker", ingestion_workspace_id=uuid4()
        ),
        AppSettings(
            app_env="test",
            process_role="trainer-worker",
            trading_mode="PAPER",
            database_user="crypidian_trainer_worker",
            database_password_file=Path("/tmp/trainer-test-secret"),
            learning_training_workspace_id=uuid4(),
        ),
        AppSettings(
            app_env="test",
            process_role="backtest-worker",
            backtest_worker_workspace_id=uuid4(),
            database_user="crypidian_backtest_worker",
            backtest_replay_database_user="crypidian_replay_writer",
            backtest_replay_database_password_file=Path("/tmp/replay-writer-test-secret"),
        ),
        AppSettings(
            app_env="test",
            process_role="signal-worker",
            signal_workspace_id=uuid4(),
            telegram_bot_token_file="/tmp/telegram-test-secret",
        ),
    )
    for settings in worker_settings:
        worker = create_app(settings, session_factory=sessions)
        assert not any(
            getattr(route, "path", None) == "/api/v1/research-sources" for route in worker.routes
        )


def test_auth_and_csrf_dependencies_reject_before_service_dispatch():
    http, service, _ = client()
    app = http.app

    def deny_auth():
        raise HTTPException(status_code=401, detail="Cần đăng nhập.")

    app.dependency_overrides[current_identity] = deny_auth
    assert http.get("/api/v1/research-sources").status_code == 401
    app.dependency_overrides.pop(current_identity)

    def deny_csrf():
        raise HTTPException(status_code=403, detail="CSRF không hợp lệ.")

    app.dependency_overrides[require_csrf] = deny_csrf
    response = http.post(
        "/api/v1/research-sources", headers={"Idempotency-Key": str(uuid4())}, json=create_payload()
    )
    assert response.status_code == 403
    assert service.facade.calls == []


def test_revision_and_approval_reject_identity_fields_and_disabled_extra_data():
    http, service, _ = client()
    headers = {"Idempotency-Key": str(uuid4())}
    revision_url = "/api/v1/research-sources/calendar/revisions"
    for field in ("workspace_id", "actor_id", "approved_by", "reviewer_id"):
        response = http.post(
            revision_url,
            headers=headers,
            json={"state": "rights_pending", "reason": "Submitted", field: str(uuid4())},
        )
        assert response.status_code == 422
        response = http.post(
            revision_url + "/pending/approve",
            headers=headers,
            json={"allowed_purposes": ["metadata"], "reason": "Approved", field: str(uuid4())},
        )
        assert response.status_code == 422
    response = http.post(
        revision_url,
        headers=headers,
        json={"state": "blocked", "reason": "Blocked", "publisher": "Changed"},
    )
    assert response.status_code == 422
    assert (
        http.post(revision_url, json={"state": "blocked", "reason": "Blocked"}).status_code == 422
    )
    assert service.facade.calls == []


def test_analyst_can_read_and_approve_but_cannot_write():
    http, service, _ = client(actor=identity(Role.ANALYST_REVIEWER))
    assert http.get("/api/v1/research-sources").status_code == 200
    key = {"Idempotency-Key": str(uuid4())}
    assert (
        http.post("/api/v1/research-sources", headers=key, json=create_payload()).status_code == 403
    )
    approved = http.post(
        "/api/v1/research-sources/calendar/revisions/pending/approve",
        headers=key,
        json={"allowed_purposes": ["metadata"], "reason": "Approved"},
    )
    assert approved.status_code == 201
    assert service.facade.calls[-1][0] == "approve"


def test_invalid_endpoint_and_expiry_are_request_validation_errors():
    http, service, _ = client()
    key = {"Idempotency-Key": str(uuid4())}
    assert (
        http.post(
            "/api/v1/research-sources",
            headers=key,
            json=create_payload() | {"endpoint_url": "http://example.org/"},
        ).status_code
        == 422
    )
    assert (
        http.post(
            "/api/v1/research-sources/calendar/revisions/pending/approve",
            headers=key,
            json={
                "allowed_purposes": ["metadata"],
                "valid_until": "2026-11-06T00:00:00",
                "reason": "Approved",
            },
        ).status_code
        == 422
    )
    assert service.facade.calls == []


def test_get_service_errors_and_unavailable_service_are_no_store():
    http, service = bare_client()
    for error, code in ((LookupError("db detail"), 404), (ValueError("db detail"), 409)):
        service.facade.error = error
        for path in ("/api/v1/research-sources", "/api/v1/research-sources/calendar"):
            response = http.get(path)
            assert response.status_code == code
            assert response.headers["Cache-Control"] == "no-store"
            assert "db detail" not in response.text
    http.app.state.source_policy_admin_service = None
    unavailable = http.get("/api/v1/research-sources")
    assert unavailable.status_code == 503
    assert unavailable.headers["Cache-Control"] == "no-store"


def test_get_missing_and_permission_errors_are_no_store_without_global_middleware():
    http, _ = bare_client()
    missing = http.get("/api/v1/research-sources/missing")
    assert missing.status_code == 404
    assert missing.headers["Cache-Control"] == "no-store"
    denied_http, _ = bare_client(actor=identity(Role.OPERATOR))
    denied = denied_http.get("/api/v1/research-sources")
    assert denied.status_code == 403
    assert denied.headers["Cache-Control"] == "no-store"


def test_observation_outage_is_retryable_and_missing_source_remains_404():
    from trading_bot.adapters.database.source_observer import SourceObservationIncompleteError

    service = RecordingService()
    http, _ = bare_client(service)
    service.facade.error = SourceObservationIncompleteError("fixture post-commit outage")
    result = http.post(
        "/api/v1/research-sources",
        json=create_payload(),
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert result.status_code == 503
    assert result.headers["cache-control"] == "no-store"
    assert "thử lại" in result.json()["detail"].lower()
    assert "fixture" not in result.text
    service.facade.error = LookupError("missing")
    missing = http.get("/api/v1/research-sources/calendar")
    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "no-store"


def test_upload_endpoint_exists_and_returns_no_store_even_for_validation_and_auth():
    http, service = bare_client()
    path = "/api/v1/research-sources/calendar/revisions/pending/license-evidence"
    response = http.post(
        path,
        content=b"evidence",
        headers={"Content-Type": "image/png", "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 415
    assert response.headers["cache-control"] == "no-store"
    for headers in ({}, {"Idempotency-Key": "bad"}):
        response = http.post(path, content=b"x", headers=headers)
        assert response.status_code == 422
        assert response.headers["cache-control"] == "no-store"
    denied_http, _ = bare_client(actor=identity(Role.OPERATOR))
    denied = denied_http.post(
        path, content=b"x", headers={"Content-Type": "text/plain", "Idempotency-Key": str(uuid4())}
    )
    assert denied.status_code == 403
    assert denied.headers["cache-control"] == "no-store"
    assert service.facade.calls == []


def pending_payload():
    scope = {
        "source_id": "calendar",
        "provider": "PROVIDER",
        "product": "CALENDAR",
        "venue": "PUBLIC",
        "contract": "RESEARCH",
        "symbols": ["BTCUSDT"],
        "intervals": ["1h"],
        "data_start": "2026-01-01T00:00:00Z",
        "data_end": "2027-01-01T00:00:00Z",
        "allowed_purposes": ["metadata"],
        "allowed_outputs": ["aggregate_report"],
        "derived_use": ["aggregate_reporting"],
        "max_retention_days": None,
        "attribution": "Nguồn fixture",
    }
    return {
        "state": "rights_pending",
        "reason": "Submitted",
        "evidence_ref": "archive:fixture",
        "license_scope": scope,
        "attribution": scope["attribution"],
        **{name: scope[name] for name in ("provider", "product", "venue", "contract")},
    }


def test_pending_schema_rejects_missing_duplicate_mismatched_or_untyped_scope():
    http, service = bare_client()
    payload = pending_payload()
    path = "/api/v1/research-sources/calendar/revisions"
    headers = {"Idempotency-Key": str(uuid4())}
    for body in (
        {"state": "rights_pending", "reason": "Missing"},
        payload | {"provider": "OTHER"},
        payload | {"license_scope": payload["license_scope"] | {"symbols": ["BTCUSDT", "BTCUSDT"]}},
        payload | {"license_scope": payload["license_scope"] | {"symbols": ["*"]}},
        payload | {"license_scope": payload["license_scope"] | {"max_retention_days": True}},
        payload | {"license_scope": payload["license_scope"] | {"unexpected": "secret"}},
    ):
        assert http.post(path, json=body, headers=headers).status_code == 422
    assert service.facade.calls == []


def test_upload_auth_and_csrf_no_store_with_bare_router():
    http, service = bare_client()
    path = "/api/v1/research-sources/calendar/revisions/pending/license-evidence"

    def denied():
        raise HTTPException(403, "CSRF không hợp lệ.")

    http.app.dependency_overrides[require_csrf] = denied
    response = http.post(
        path,
        content=b"private",
        headers={"Idempotency-Key": str(uuid4()), "Content-Type": "text/plain"},
    )
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert service.facade.calls == []


async def test_raw_asgi_disconnect_discards_partial_body_without_evidence():
    import json

    from tests.unit.test_source_policy_admin import BUDGET, Repository, pending_command

    from trading_bot.application.source_policy_admin import (
        SourceCreateCommand,
        SourcePolicyAdminService,
    )

    repository = Repository()
    service = SourcePolicyAdminService(repository)
    actor = identity()
    facade = service.for_principal(actor.principal)
    await facade.create_source(
        SourceCreateCommand(
            "calendar", "Publisher", "https://example.org/", "GET", "public", BUDGET, "Candidate"
        ),
        uuid4(),
    )
    pending = await facade.submit_pending("calendar", pending_command("calendar"), uuid4())
    app = FastAPI()
    app.include_router(research_source_router)
    app.state.source_policy_admin_service = service
    app.dependency_overrides[require_csrf] = lambda: actor
    messages = iter(
        [
            {"type": "http.request", "body": b"partial private evidence", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    responses = []

    async def receive():
        return next(messages)

    async def send(message):
        responses.append(message)

    await app(
        {
            "type": "http",
            "http_version": "1.1",
            "scheme": "http",
            "method": "POST",
            "path": (
                f"/api/v1/research-sources/calendar/revisions/"
                f"{pending.policy.policy_id}/license-evidence"
            ),
            "query_string": b"",
            "headers": [
                (b"content-type", b"text/plain"),
                (b"idempotency-key", str(uuid4()).encode()),
            ],
            "server": ("fixture", 80),
            "client": ("test", 1234),
            "root_path": "",
        },
        receive,
        send,
    )
    assert responses[0]["status"] == 400
    assert (b"cache-control", b"no-store") in responses[0]["headers"]
    assert json.loads(responses[1]["body"])["detail"] == "Upload bị gián đoạn."
    assert repository.evidence == []
