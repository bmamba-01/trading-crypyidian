from pathlib import Path
from threading import Event
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


def test_ingestion_lifespan_starts_workspace_worker_and_stops_it_on_shutdown() -> None:
    workspace_id = uuid4()
    sessions = object()
    started = Event()
    stopped = Event()
    factory_calls: list[dict[str, object]] = []

    class _Worker:
        last_tick_succeeded = False

        async def run(self, stopping: object) -> None:
            self.last_tick_succeeded = True
            started.set()
            await stopping.wait()  # type: ignore[attr-defined]
            stopped.set()

    def make_worker(**kwargs: object) -> _Worker:
        factory_calls.append(kwargs)
        return _Worker()

    async def database_ready() -> bool:
        return True

    app = create_app(
        AppSettings(
            process_role="ingestion-worker",
            ingestion_workspace_id=workspace_id,
        ),
        session_factory=sessions,  # type: ignore[arg-type]
        rights_worker_factory=make_worker,
        database_probe=database_ready,
    )

    with TestClient(app) as client:
        assert started.wait(timeout=1)
        assert client.get("/health/ready").json() == {"status": "ready"}
        assert len(factory_calls) == 1
        assert factory_calls[0]["session_factory"] is sessions
        assert factory_calls[0]["workspace_id"] == workspace_id
        assert factory_calls[0]["tick_seconds"] == 5.0
        assert isinstance(factory_calls[0]["worker_id"], str)

    assert stopped.wait(timeout=1)


def test_trainer_lifespan_starts_workspace_supervisor_and_stops_it_on_shutdown() -> None:
    workspace_id = uuid4()
    sessions = object()
    started = Event()
    stopped = Event()
    factory_calls: list[dict[str, object]] = []

    class _Supervisor:
        async def run(self, stopping: object) -> None:
            started.set()
            await stopping.wait()  # type: ignore[attr-defined]
            stopped.set()

    def make_supervisor(**kwargs: object) -> _Supervisor:
        factory_calls.append(kwargs)
        return _Supervisor()

    async def database_ready() -> bool:
        return True

    app = create_app(
        AppSettings(
            process_role="trainer-worker",
            trading_mode="PAPER",
            database_user="crypidian_trainer_worker",
            database_password_file=Path("/run/secrets/database_trainer_worker_password"),
            learning_training_workspace_id=workspace_id,
        ),
        session_factory=sessions,  # type: ignore[arg-type]
        training_supervisor_factory=make_supervisor,
        database_probe=database_ready,
    )

    with TestClient(app) as client:
        assert started.wait(timeout=1)
        assert client.get("/health/ready").json() == {"status": "ready"}
        assert len(factory_calls) == 1
        assert factory_calls[0]["worker"]._sessions is sessions  # type: ignore[attr-defined]
        assert factory_calls[0]["workspace_id"] == workspace_id
        assert factory_calls[0]["poll_seconds"] == 5.0
        assert factory_calls[0]["max_backoff_seconds"] == 60.0
        assert isinstance(factory_calls[0]["worker_id"], str)

    assert stopped.wait(timeout=1)


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

    def make_bootstrap(*, sessions: object, workspace_id: UUID, **_: object) -> _Bootstrap:
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
        "standby": 0,
        "paused": 0,
        "activity_blocked": 0,
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
        "standby": 0,
        "paused": 0,
        "activity_blocked": 0,
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


def test_ingestion_lifespan_starts_live_market_worker_only_when_symbols_are_configured() -> None:
    workspace_id = uuid4()
    sessions = object()
    live_started = Event()
    live_stopped = Event()
    closed = Event()
    live_calls: list[dict[str, object]] = []

    class _RightsWorker:
        last_tick_succeeded = True

        async def run(self, stopping: object) -> None:
            await stopping.wait()  # type: ignore[attr-defined]

    class _LiveWorker:
        is_healthy = False

        async def run(self, stopping: object) -> None:
            live_started.set()
            await stopping.wait()  # type: ignore[attr-defined]
            live_stopped.set()

    class _LiveRuntime:
        worker = _LiveWorker()

        async def aclose(self) -> None:
            closed.set()

    def make_live(**kwargs: object) -> _LiveRuntime:
        live_calls.append(kwargs)
        return _LiveRuntime()

    async def database_ready() -> bool:
        return True

    app = create_app(
        AppSettings(
            process_role="ingestion-worker",
            ingestion_workspace_id=workspace_id,
            ingestion_live_symbols="BTCUSDT, ETHUSDT",
            ingestion_live_timeframes="15m,1h",
            ingestion_live_warmup_bars=250,
        ),
        session_factory=sessions,  # type: ignore[arg-type]
        rights_worker_factory=lambda **_: _RightsWorker(),
        live_market_ingestion_factory=make_live,
        database_probe=database_ready,
    )

    with TestClient(app) as client:
        assert live_started.wait(timeout=1)
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "not_ready", "pending": ["live_market_ingestion"]}
        _LiveRuntime.worker.is_healthy = True
        assert client.get("/health/ready").json() == {"status": "ready"}

    assert live_stopped.wait(timeout=1)
    assert closed.wait(timeout=1)
    assert len(live_calls) == 1
    assert live_calls[0]["session_factory"] is sessions
    assert live_calls[0]["workspace_id"] == workspace_id
    assert live_calls[0]["warmup_bars"] == 250
    assert live_calls[0]["subscriptions"] == (
        ("BTCUSDT", "15m"),
        ("BTCUSDT", "1h"),
        ("ETHUSDT", "15m"),
        ("ETHUSDT", "1h"),
    )


def test_ingestion_lifespan_keeps_live_market_worker_off_by_default() -> None:
    live_calls: list[dict[str, object]] = []

    class _RightsWorker:
        last_tick_succeeded = True

        async def run(self, stopping: object) -> None:
            await stopping.wait()  # type: ignore[attr-defined]

    async def database_ready() -> bool:
        return True

    app = create_app(
        AppSettings(process_role="ingestion-worker", ingestion_workspace_id=uuid4()),
        session_factory=object(),  # type: ignore[arg-type]
        rights_worker_factory=lambda **_: _RightsWorker(),
        live_market_ingestion_factory=lambda **kwargs: live_calls.append(kwargs),
        database_probe=database_ready,
    )

    with TestClient(app) as client:
        assert client.get("/health/ready").json() == {"status": "ready"}

    assert live_calls == []
