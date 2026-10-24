from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.config.settings import AppSettings


def test_readiness_is_blocked_until_all_dependencies_are_ready() -> None:
    app = create_app()

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "pending": ["database", "exchange", "market_data"],
    }


def test_readiness_does_not_expose_credentials() -> None:
    app = create_app()
    app.state.readiness = {
        "database": True,
        "market_data": True,
        "exchange": True,
    }

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert "secret" not in response.text.lower()


def test_runtime_startup_hydration_failure_keeps_readiness_blocked_and_reports_counts() -> None:
    workspace_id = uuid4()
    account_id = uuid4()
    bootstrap_result = SimpleNamespace(
        workspace_id=workspace_id,
        ready=False,
        database_ready=True,
        error_code="PAPER_RECOVERY_CONFLICT",
        account_states={
            account_id: SimpleNamespace(status="RECOVERY_REQUIRED"),
        },
        active_accounts={},
    )
    calls: list[UUID] = []

    class _Bootstrap:
        async def bootstrap(self) -> SimpleNamespace:
            return bootstrap_result

    def make_bootstrap(*, sessions: object, workspace_id: UUID) -> _Bootstrap:
        del sessions
        calls.append(workspace_id)
        return _Bootstrap()

    app = create_app(
        AppSettings(process_role="trading-runtime", runtime_workspace_id=workspace_id),
        session_factory=object(),  # type: ignore[arg-type]
        paper_runtime_bootstrap_factory=make_bootstrap,
    )

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert calls == [workspace_id]
    assert response.status_code == 503
    assert "paper_runtime" in response.json()["pending"]
    assert response.json()["paper_accounts"] == {
        "total": 1,
        "ready": 0,
        "recovery_required": 1,
        "blocked_by_workspace": 0,
    }
    assert str(workspace_id) not in response.text
    assert str(account_id) not in response.text


def test_runtime_readiness_becomes_ready_only_after_all_accounts_hydrate() -> None:
    workspace_id = uuid4()
    account_ids = (uuid4(), uuid4())
    bootstrap_result = SimpleNamespace(
        workspace_id=workspace_id,
        ready=True,
        database_ready=True,
        error_code=None,
        account_states={account_id: SimpleNamespace(status="READY") for account_id in account_ids},
        active_accounts={account_id: object() for account_id in account_ids},
    )

    class _Bootstrap:
        async def bootstrap(self) -> SimpleNamespace:
            return bootstrap_result

    app = create_app(
        AppSettings(process_role="trading-runtime", runtime_workspace_id=workspace_id),
        session_factory=object(),  # type: ignore[arg-type]
        paper_runtime_bootstrap_factory=lambda **_: _Bootstrap(),
    )

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["execution_capabilities"] == {
        "paper_recovery": "ready",
        "market_dispatch": "not_configured",
        "exchange_execution": "disabled",
    }
    assert response.json()["paper_accounts"] == {
        "total": 2,
        "ready": 2,
        "recovery_required": 0,
        "blocked_by_workspace": 0,
    }
    assert "workspace_id" not in response.text
    assert "settings_hash" not in response.text


def test_runtime_bootstrap_exception_exposes_only_safe_diagnostic_code(caplog) -> None:
    workspace_id = uuid4()

    class _BrokenBootstrap:
        async def bootstrap(self) -> object:
            raise RuntimeError("synthetic secret should not be logged")

    app = create_app(
        AppSettings(process_role="trading-runtime", runtime_workspace_id=workspace_id),
        session_factory=object(),  # type: ignore[arg-type]
        paper_runtime_bootstrap_factory=lambda **_: _BrokenBootstrap(),
    )

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["paper_runtime_error_code"] == "PAPER_BOOTSTRAP_FAILED"
    assert "synthetic secret" not in response.text
    assert "synthetic secret" not in caplog.text


def test_runtime_publishes_paper_application_only_after_bootstrap_succeeds() -> None:
    workspace_id = uuid4()
    account_id = uuid4()

    class _Bootstrap:
        async def bootstrap(self) -> SimpleNamespace:
            return SimpleNamespace(
                workspace_id=workspace_id,
                ready=True,
                database_ready=True,
                error_code=None,
                account_states={account_id: SimpleNamespace(status="READY")},
                active_accounts={account_id: SimpleNamespace(account_id=account_id)},
            )

    app = create_app(
        AppSettings(process_role="trading-runtime", runtime_workspace_id=workspace_id),
        session_factory=object(),  # type: ignore[arg-type]
        paper_runtime_bootstrap_factory=lambda **_: _Bootstrap(),
    )

    with TestClient(app) as client:
        ready = client.get("/health/ready")
        service = app.state.paper_application

    assert ready.status_code == 200
    assert service.is_available_for(account_id)
    assert ready.json()["execution_capabilities"] == {
        "paper_recovery": "ready",
        "market_dispatch": "not_configured",
        "exchange_execution": "disabled",
    }


def test_runtime_does_not_publish_paper_application_after_bootstrap_failure() -> None:
    workspace_id = uuid4()

    class _Bootstrap:
        async def bootstrap(self) -> SimpleNamespace:
            return SimpleNamespace(
                workspace_id=workspace_id,
                ready=False,
                database_ready=True,
                error_code="PAPER_RECOVERY_CONFLICT",
                account_states={},
                active_accounts={},
            )

    app = create_app(
        AppSettings(process_role="trading-runtime", runtime_workspace_id=workspace_id),
        session_factory=object(),  # type: ignore[arg-type]
        paper_runtime_bootstrap_factory=lambda **_: _Bootstrap(),
    )

    with TestClient(app) as client:
        ready = client.get("/health/ready")

    assert ready.status_code == 503
    assert getattr(app.state, "paper_application", None) is None
