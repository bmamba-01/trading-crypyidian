"""SEC-26: tắt OpenAPI ở production và chỉ mount router admin ở process `admin-web`."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.config.settings import AppSettings, ProcessRole

WORKSPACE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

NON_ADMIN_ROLES: tuple[ProcessRole, ...] = (
    "trading-runtime",
    "execution-gateway",
    "ingestion-worker",
    "signal-worker",
    "market-signal-worker",
    "trainer-worker",
    "learning-shadow-observer",
    "learning-evaluator",
    "backtest-worker",
)

ALL_ROLES: tuple[ProcessRole, ...] = ("admin-web", *NON_ADMIN_ROLES)


def _settings(app_env: str, role: ProcessRole) -> AppSettings:
    kwargs: dict[str, Any] = {"app_env": app_env, "process_role": role}
    if (
        role == "admin-web"
        and app_env == "production"
        and "allowed_hosts" in AppSettings.model_fields
    ):
        kwargs["allowed_hosts"] = "testserver"
    if role == "trading-runtime":
        kwargs["runtime_workspace_id"] = WORKSPACE
    elif role == "ingestion-worker":
        kwargs["ingestion_workspace_id"] = WORKSPACE
    elif role == "signal-worker":
        kwargs["signal_workspace_id"] = WORKSPACE
        kwargs["telegram_bot_token_file"] = Path("/tmp/fake-telegram-token")
    elif role == "market-signal-worker":
        kwargs["market_signal_workspace_id"] = WORKSPACE
    elif role == "trainer-worker":
        kwargs["database_user"] = "crypidian_trainer_worker"
        kwargs["database_password_file"] = Path("/tmp/fake-trainer-pw")
        kwargs["learning_training_workspace_id"] = WORKSPACE
    elif role == "learning-shadow-observer":
        kwargs["database_user"] = "crypidian_learning_shadow_observer"
        kwargs["database_password_file"] = Path(
            "/run/secrets/database_learning_shadow_observer_password"
        )
        kwargs["learning_shadow_observer_workspace_id"] = WORKSPACE
    elif role == "learning-evaluator":
        kwargs["database_user"] = "crypidian_learning_evaluator"
        kwargs["database_password_file"] = Path("/tmp/fake-evaluator-pw")
    elif role == "backtest-worker":
        kwargs["backtest_worker_workspace_id"] = WORKSPACE
        kwargs["database_user"] = "crypidian_backtest_worker"
        kwargs["backtest_replay_database_user"] = "crypidian_replay_writer"
        kwargs["backtest_replay_database_password_file"] = Path("/tmp/fake-replay-pw")
    return AppSettings(**kwargs)


def _route_paths(app: Any) -> set[str]:
    paths: set[str] = set()
    for route in app.routes:
        if hasattr(route, "effective_route_contexts"):
            for context in route.effective_route_contexts():
                path = getattr(context, "path", None)
                if path:
                    paths.add(path)
        else:
            path = getattr(route, "path", None)
            if path:
                paths.add(path)
    return paths


@pytest.mark.parametrize("role", ALL_ROLES)
def test_openapi_and_docs_are_disabled_in_production_for_every_role(role: ProcessRole) -> None:
    app = create_app(_settings("production", role))

    assert app.openapi_url is None
    assert app.docs_url is None
    assert app.redoc_url is None
    client = TestClient(app)
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert client.get(path).status_code == 404


@pytest.mark.parametrize("app_env", ["local", "test"])
def test_openapi_stays_available_outside_production_only_for_admin_web(app_env: str) -> None:
    app = create_app(_settings(app_env, "admin-web"))

    assert app.openapi_url == "/openapi.json"
    assert TestClient(app).get("/openapi.json").status_code == 200
    assert app.docs_url is None
    assert app.redoc_url is None


@pytest.mark.parametrize("role", NON_ADMIN_ROLES)
@pytest.mark.parametrize("app_env", ["local", "test", "production"])
def test_admin_routers_and_openapi_are_not_mounted_outside_admin_web(
    role: ProcessRole, app_env: str
) -> None:
    app = create_app(_settings(app_env, role))
    paths = _route_paths(app)

    assert app.openapi_url is None
    assert paths == {"/health/live", "/health/ready"}
    client = TestClient(app)
    for path in (
        "/openapi.json",
        "/docs",
        "/redoc",
        "/api/v1/auth/login",
        "/api/v1/auth/me",
        "/api/v1/settings",
        "/api/v1/bots",
        "/api/v1/bot-grants/review",
        "/admin",
        "/admin/login",
        "/static/admin.css",
    ):
        assert client.get(path).status_code == 404


def test_admin_web_mounts_auth_settings_grants_and_admin_ui() -> None:
    app = create_app(_settings("test", "admin-web"))
    paths = _route_paths(app)

    assert "/api/v1/auth/login" in paths
    assert "/api/v1/settings/current" in paths
    assert "/api/v1/bots" in paths
    assert "/api/v1/bot-grants" in paths
    assert "/api/v1/bot-grants/review" in paths
    assert "/admin" in paths
    assert "/static" in paths
    assert {"/health/live", "/health/ready"} <= paths
