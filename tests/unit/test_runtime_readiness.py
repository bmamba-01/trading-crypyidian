"""MB05.5 ruling 2/26: `/health/ready` 200 `ready`/`degraded`, 503 chỉ khi không phục vụ được."""

from __future__ import annotations

import re
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.config.settings import AppSettings

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _result(
    workspace_id: UUID,
    statuses: dict[UUID, str],
    *,
    ready: bool = True,
    database_ready: bool = True,
    error_code: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        workspace_id=workspace_id,
        ready=ready,
        database_ready=database_ready,
        error_code=error_code,
        account_states={
            account_id: SimpleNamespace(status=status, reason_code=None)
            for account_id, status in statuses.items()
        },
        active_accounts={
            account_id: SimpleNamespace(account_id=account_id)
            for account_id, status in statuses.items()
            if status == "READY"
        },
    )


def _client(result: SimpleNamespace, workspace_id: UUID) -> tuple[TestClient, object]:
    class _Bootstrap:
        async def bootstrap(self) -> SimpleNamespace:
            return result

    app = create_app(
        AppSettings(process_role="trading-runtime", runtime_workspace_id=workspace_id),
        session_factory=object(),  # type: ignore[arg-type]
        paper_runtime_bootstrap_factory=lambda **_: _Bootstrap(),
    )
    return TestClient(app), app


def test_all_bots_ready_returns_200_ready_with_counters() -> None:
    workspace_id = uuid4()
    client, _ = _client(_result(workspace_id, {uuid4(): "READY", uuid4(): "READY"}), workspace_id)
    with client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["workspace_control"] == "active"
    assert body["paper_accounts"] == {
        "total": 2,
        "ready": 2,
        "recovery_required": 0,
        "standby": 0,
        "paused": 0,
        "activity_blocked": 0,
        "blocked_by_workspace": 0,
    }
    assert not _UUID.search(response.text)


def test_bot_without_checkpoint_degrades_but_keeps_serving() -> None:
    workspace_id = uuid4()
    client, _ = _client(
        _result(workspace_id, {uuid4(): "READY", uuid4(): "RECOVERY_REQUIRED"}), workspace_id
    )
    with client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["paper_accounts"]["ready"] == 1
    assert body["paper_accounts"]["recovery_required"] == 1
    assert body["paper_accounts"]["blocked_by_workspace"] == 0
    assert not _UUID.search(response.text)


def test_standby_paused_killed_and_activity_blocked_bots_degrade_readiness() -> None:
    workspace_id = uuid4()
    ready_id, paused_id, blocked_id, standby_id = uuid4(), uuid4(), uuid4(), uuid4()
    client, app = _client(
        _result(
            workspace_id,
            {ready_id: "READY", paused_id: "READY", blocked_id: "READY", standby_id: "STANDBY"},
        ),
        workspace_id,
    )
    with client:
        registry = app.state.paper_registry  # type: ignore[attr-defined]
        registry.set_control_flags(paused_id, bot_paused=True, workspace_killed=False)
        registry.set_activity_blocked(blocked_id, True)
        degraded = client.get("/health/ready").json()
        for account_id in (ready_id, paused_id, blocked_id, standby_id):
            registry.set_control_flags(account_id, bot_paused=False, workspace_killed=True)
        killed = client.get("/health/ready")

    assert degraded["status"] == "degraded"
    assert degraded["paper_accounts"]["standby"] == 1
    assert degraded["paper_accounts"]["paused"] == 1
    assert degraded["paper_accounts"]["activity_blocked"] == 1
    assert killed.status_code == 200
    assert killed.json()["status"] == "degraded"
    assert killed.json()["workspace_control"] == "killed"


def test_zero_ready_bots_still_200_degraded_when_process_can_serve() -> None:
    workspace_id = uuid4()
    client, _ = _client(_result(workspace_id, {uuid4(): "RECOVERY_REQUIRED"}), workspace_id)
    with client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["paper_accounts"]["ready"] == 0


def test_workspace_condition_failure_returns_503() -> None:
    workspace_id = uuid4()
    client, _ = _client(
        _result(
            workspace_id,
            {uuid4(): "BLOCKED_BY_WORKSPACE"},
            ready=False,
            error_code="SETTINGS_NOT_EFFECTIVE",
        ),
        workspace_id,
    )
    with client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["paper_runtime_error_code"] == "SETTINGS_NOT_EFFECTIVE"
    assert response.json()["paper_accounts"]["blocked_by_workspace"] == 1


def test_database_unavailable_returns_503() -> None:
    workspace_id = uuid4()
    client, _ = _client(
        _result(
            workspace_id, {}, ready=False, database_ready=False, error_code="DATABASE_UNAVAILABLE"
        ),
        workspace_id,
    )
    with client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert "database" in response.json()["pending"]


def test_bootstrap_exception_returns_503_with_safe_code() -> None:
    workspace_id = uuid4()

    class _Broken:
        async def bootstrap(self) -> object:
            raise RuntimeError("boom secret")

    app = create_app(
        AppSettings(process_role="trading-runtime", runtime_workspace_id=workspace_id),
        session_factory=object(),  # type: ignore[arg-type]
        paper_runtime_bootstrap_factory=lambda **_: _Broken(),
    )
    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["paper_runtime_error_code"] == "PAPER_BOOTSTRAP_FAILED"
    assert "boom" not in response.text


def test_account_invalidation_degrades_only_that_bot_without_process_flag() -> None:
    workspace_id = uuid4()
    first, second = uuid4(), uuid4()
    client, app = _client(_result(workspace_id, {first: "READY", second: "READY"}), workspace_id)
    with client:
        app.state.paper_application._invalidate_account(  # type: ignore[attr-defined]
            first, "PAPER_PROFILE_DRIFT"
        )
        response = client.get("/health/ready")
        service = app.state.paper_application  # type: ignore[attr-defined]

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["paper_accounts"]["recovery_required"] == 1
    assert response.json()["paper_accounts"]["ready"] == 1
    assert app.state.readiness["paper_runtime"] is True  # type: ignore[attr-defined]
    assert service.is_available_for(second)
    assert not service.is_available_for(first)
