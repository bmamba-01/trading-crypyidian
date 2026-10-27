"""HTTP API báo cáo MB-06: scope tường minh, 404 không phân biệt, 403 tổng hợp, no-store."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from tests.unit import test_bot_reporting as reporting

from trading_bot.admin.bot_reports_router import router as reports_router
from trading_bot.app import create_app
from trading_bot.application.bot_reporting import (
    NOT_FOUND_MESSAGE,
    BotReportError,
    BotReportQuery,
    ReportScope,
)
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings

BOT_ID = uuid4()
WINDOW = {"start": "2026-10-01T00:00:00Z", "end": "2026-10-02T00:00:00Z"}
ACTOR = AuthIdentity(
    principal=Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=uuid4(),
        roles=frozenset({Role.ANALYST_REVIEWER}),
        mfa_verified_at=datetime.now(UTC),
    ),
    email="analyst@example.test",
)


class _Report:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def as_dict(self) -> dict[str, Any]:
        return dict(self._payload)


class RecordingReports:
    def __init__(self, error: BotReportError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, BotReportQuery, Principal]] = []

    async def get_bot_report(self, query: BotReportQuery, principal: Principal) -> _Report:
        self.calls.append(("bot", query, principal))
        if self.error is not None:
            raise self.error
        return _Report({"scope": "bot", "bot_id": str(query.bot_id), "report_hash": "h" * 64})

    async def get_workspace_report(self, query: BotReportQuery, principal: Principal) -> _Report:
        self.calls.append(("workspace", query, principal))
        if self.error is not None:
            raise self.error
        return _Report({"scope": "workspace", "report_hash": "w" * 64})


def _identity(principal: Principal) -> AuthIdentity:
    return AuthIdentity(principal=principal, email="member@example.test")


def client_with(service: object | None, *, identity: AuthIdentity = ACTOR) -> TestClient:
    app = create_app(AppSettings(app_env="test"))
    app.dependency_overrides[current_identity] = lambda: identity
    if service is not None:
        app.state.bot_reporting_service = cast(object, service)
    return TestClient(app)


def test_bot_scope_report_passes_explicit_query_from_session_workspace() -> None:
    service = RecordingReports()

    response = client_with(service).get(
        "/api/v1/reports",
        params={"scope": "bot", "bot_id": str(BOT_ID), **WINDOW},
    )

    assert response.status_code == 200
    assert response.json() == {
        "report": {"scope": "bot", "bot_id": str(BOT_ID), "report_hash": "h" * 64}
    }
    assert response.headers["Cache-Control"] == "no-store"
    kind, query, principal = service.calls[0]
    assert kind == "bot"
    assert principal is ACTOR.principal
    assert query == BotReportQuery(
        workspace_id=ACTOR.principal.workspace_id,
        bot_id=BOT_ID,
        start=datetime(2026, 10, 1, tzinfo=UTC),
        end=datetime(2026, 10, 2, tzinfo=UTC),
        mode="PAPER",
        report_version="bot-report.v1",
        scope=ReportScope.BOT,
    )


def test_workspace_scope_report_requires_literal_scope() -> None:
    service = RecordingReports()

    response = client_with(service).get(
        "/api/v1/reports", params={"scope": "workspace", "mode": "PAPER", **WINDOW}
    )

    assert response.status_code == 200
    assert response.json()["report"]["scope"] == "workspace"
    kind, query, _ = service.calls[0]
    assert (kind, query.scope, query.bot_id) == ("workspace", ReportScope.WORKSPACE, None)


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({}, "REPORT_SCOPE_REQUIRED"),
        ({"bot_id": str(BOT_ID)}, "REPORT_SCOPE_REQUIRED"),
        ({"scope": "bot"}, "BOT_ID_REQUIRED"),
        ({"scope": "workspace", "bot_id": str(BOT_ID)}, "BOT_ID_NOT_ALLOWED"),
        ({"scope": "all"}, "REPORT_SCOPE_INVALID"),
    ],
)
def test_implicit_or_ambiguous_scope_is_rejected(params: dict[str, str], code: str) -> None:
    service = RecordingReports()

    response = client_with(service).get("/api/v1/reports", params={**params, **WINDOW})

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == code
    assert service.calls == []


def test_not_found_and_forbidden_bot_are_indistinguishable_through_real_service() -> None:
    """Đi qua BotReportingService + BotGrantRepository.require thật (chỉ thay nguồn dữ liệu)."""
    reporting.reset_store()
    cases = [
        (reporting.ANALYST, reporting.BOT_B),  # analyst chưa được grant bot B
        (reporting.OPERATOR, reporting.BOT_A),  # operator có bot:read nhưng thiếu report grant
        (reporting.MULTI, reporting.BOT_A),  # nhiều role không mượn quyền workspace
        (reporting.OWNER, uuid4()),  # bot không tồn tại
        (reporting.OWNER, reporting.BOT_X),  # bot của workspace khác
        (reporting.OWNER_W2, reporting.BOT_A),  # owner W2 dò bot W1
    ]
    bodies = []
    for actor, bot_id in cases:
        client = client_with(reporting.service(), identity=_identity(actor))
        response = client.get(
            "/api/v1/reports", params={"scope": "bot", "bot_id": str(bot_id), **WINDOW}
        )
        assert response.status_code == 404, (actor.roles, bot_id)
        assert response.headers["Cache-Control"] == "no-store"
        bodies.append(response.json())
    assert all(body == bodies[0] for body in bodies)
    assert bodies[0] == {"detail": {"code": "REPORT_NOT_FOUND", "message": NOT_FOUND_MESSAGE}}
    # Đối chứng dương: cùng đường thật trả 200 khi có grant bot:report:read.
    allowed = client_with(reporting.service(), identity=_identity(reporting.ANALYST)).get(
        "/api/v1/reports", params={"scope": "bot", "bot_id": str(reporting.BOT_A), **WINDOW}
    )
    assert allowed.status_code == 200
    assert allowed.json()["report"]["bot_id"] == str(reporting.BOT_A)


@pytest.mark.parametrize(
    ("status", "code"),
    [(403, "WORKSPACE_REPORT_FORBIDDEN"), (409, "LEDGER_RECONCILIATION_FAILED")],
)
def test_service_errors_map_to_status(status: int, code: str) -> None:
    response = client_with(RecordingReports(BotReportError(status, code, "x"))).get(
        "/api/v1/reports", params={"scope": "workspace", **WINDOW}
    )

    assert response.status_code == status
    assert response.json()["detail"]["code"] == code


def test_malformed_bot_id_or_window_is_rejected_by_schema() -> None:
    service = RecordingReports()
    client = client_with(service)

    assert (
        client.get(
            "/api/v1/reports", params={"scope": "bot", "bot_id": "not-a-uuid", **WINDOW}
        ).status_code
        == 422
    )
    assert (
        client.get("/api/v1/reports", params={"scope": "bot", "bot_id": str(BOT_ID)}).status_code
        == 422
    )
    assert service.calls == []


def test_service_unavailable_returns_503() -> None:
    response = client_with(None).get(
        "/api/v1/reports", params={"scope": "bot", "bot_id": str(BOT_ID), **WINDOW}
    )

    assert response.status_code == 503


def test_report_route_requires_authenticated_session() -> None:
    app = create_app(AppSettings(app_env="test"))
    app.state.bot_reporting_service = cast(object, RecordingReports())
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/reports", params={"scope": "bot", "bot_id": str(BOT_ID), **WINDOW}
        )

    # Không có dịch vụ xác thực/phiên → không bao giờ trả dữ liệu báo cáo.
    assert response.status_code in {401, 503}
    assert "report" not in response.json()


def test_report_router_is_read_only_get() -> None:
    routes = [(route.path, route.methods) for route in reports_router.routes]  # type: ignore[attr-defined]
    assert routes == [("/api/v1/reports", {"GET"})]
    response = client_with(RecordingReports()).post(
        "/api/v1/reports", params={"scope": "bot", "bot_id": str(BOT_ID), **WINDOW}
    )
    assert response.status_code == 405
