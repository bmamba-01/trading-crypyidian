import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from trading_bot.adapters.database.engine import (
    create_database_engine,
    create_session_factory,
    database_url_from_secret,
)
from trading_bot.adapters.secrets.local_mfa import LocalMfaSecretProvider
from trading_bot.admin.settings_router import router as settings_router
from trading_bot.admin.settings_service import SettingsAdminService
from trading_bot.application.paper_runtime_application import (
    PaperRuntimeApplicationService,
)
from trading_bot.application.paper_runtime_bootstrap import PaperRuntimeBootstrapService
from trading_bot.auth.router import router as auth_router
from trading_bot.auth.security import LoginRateLimiter
from trading_bot.auth.service import AuthService
from trading_bot.config.settings import AppSettings
from trading_bot.ports.secrets import SecretProvider

logger = logging.getLogger(__name__)


def create_app(
    settings: AppSettings | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    secret_provider: SecretProvider | None = None,
    paper_runtime_bootstrap_factory: Callable[..., Any] | None = None,
    paper_decision_context_provider_factory: Callable[..., Any] | None = None,
) -> FastAPI:
    """Tạo ứng dụng mà không kết nối mạng hay tải credential khi import."""
    app_settings = settings or AppSettings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        engine: AsyncEngine | None = None
        active_session_factory = session_factory
        active_secret_provider = secret_provider
        if (
            active_session_factory is None
            and app_settings.database_user
            and app_settings.database_password_file
        ):
            database_url = database_url_from_secret(
                username=app_settings.database_user,
                password_file=app_settings.database_password_file,
                host=app_settings.database_host,
                database=app_settings.database_name,
            )
            engine = create_database_engine(database_url)
            active_session_factory = create_session_factory(engine)
        if (
            active_secret_provider is None
            and app_settings.mfa_encryption_key_file
            and app_settings.mfa_secret_directory
        ):
            active_secret_provider = LocalMfaSecretProvider(
                key_file=app_settings.mfa_encryption_key_file,
                storage_directory=app_settings.mfa_secret_directory,
            )
        application.state.auth_service = (
            AuthService(active_session_factory, active_secret_provider)
            if active_session_factory is not None and active_secret_provider is not None
            else None
        )
        application.state.settings_admin_service = (
            SettingsAdminService(active_session_factory)
            if active_session_factory is not None
            else None
        )
        if app_settings.process_role == "trading-runtime":
            application.state.paper_runtime = None
            application.state.paper_application = None
            application.state.paper_accounts = {}
            application.state.paper_account_readiness = {}
            application.state.paper_runtime_error_code = "PAPER_BOOTSTRAP_NOT_RUN"
            factory = paper_runtime_bootstrap_factory or PaperRuntimeBootstrapService
            try:
                workspace_id = app_settings.runtime_workspace_id
                if workspace_id is None:
                    raise RuntimeError("trading-runtime deployment workspace chưa cấu hình.")
                bootstrap = factory(
                    sessions=active_session_factory,
                    workspace_id=workspace_id,
                )
                result = await bootstrap.bootstrap()
            except Exception:
                result = None
            if result is None:
                application.state.paper_runtime_error_code = "PAPER_BOOTSTRAP_FAILED"
                logger.error(
                    "PAPER runtime bootstrap failed; diagnostic_code=%s",
                    application.state.paper_runtime_error_code,
                )
            else:
                application.state.paper_runtime = result
                application.state.paper_accounts = result.active_accounts
                application.state.paper_account_readiness = result.account_states
                application.state.paper_runtime_error_code = result.error_code
                application.state.readiness["database"] = result.database_ready
                application.state.readiness["paper_runtime"] = result.ready
                if result.ready:
                    try:
                        context_provider = (
                            paper_decision_context_provider_factory(
                                sessions=active_session_factory,
                                workspace_id=result.workspace_id,
                                active_accounts=result.active_accounts,
                            )
                            if paper_decision_context_provider_factory is not None
                            else None
                        )

                        def invalidate_paper_account(account_id: object, reason_code: str) -> None:
                            account_states = dict(application.state.paper_account_readiness)
                            account_states[account_id] = SimpleNamespace(
                                status="RECOVERY_REQUIRED", reason_code=reason_code
                            )
                            application.state.paper_account_readiness = account_states
                            active_accounts = dict(application.state.paper_accounts)
                            active_accounts.pop(account_id, None)
                            application.state.paper_accounts = active_accounts
                            application.state.readiness["paper_runtime"] = False
                            application.state.paper_runtime_error_code = reason_code

                        application.state.paper_application = PaperRuntimeApplicationService(
                            workspace_id=result.workspace_id,
                            sessions=active_session_factory,
                            active_accounts=result.active_accounts,
                            context_provider=context_provider,
                            account_invalidation_callback=invalidate_paper_account,
                        )
                    except Exception:
                        application.state.paper_application = None
                        application.state.paper_accounts = {}
                        application.state.readiness["paper_runtime"] = False
                        application.state.paper_runtime_error_code = (
                            "PAPER_APPLICATION_WIRING_FAILED"
                        )
                        logger.error(
                            "PAPER application wiring failed; diagnostic_code=%s",
                            application.state.paper_runtime_error_code,
                        )
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()

    app = FastAPI(
        title="Crypidian Trading Bot",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.login_limiter = LoginRateLimiter()
    app.state.readiness = {
        "database": False,
        "market_data": False,
        "exchange": False,
    }
    if app_settings.process_role == "trading-runtime":
        app.state.readiness["paper_runtime"] = False
        app.state.paper_application = None
    app.include_router(auth_router)
    app.include_router(settings_router)

    @app.exception_handler(RequestValidationError)
    async def validation_error_response(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        del request
        details = [
            {
                "loc": list(item.get("loc", ())),
                "msg": item.get("msg", "Request không hợp lệ"),
                "type": item.get("type", "value_error"),
            }
            for item in error.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": details})

    @app.get("/health/live", include_in_schema=False)
    def live_health() -> dict[str, str]:
        """Báo process còn hoạt động; không khẳng định các dependency đã sẵn sàng."""
        return {
            "status": "ok",
            "mode": app_settings.trading_mode,
            "role": app_settings.process_role,
        }

    @app.get("/health/ready", include_in_schema=False, response_model=None)
    def ready_health() -> JSONResponse | dict[str, object]:
        """Báo sẵn sàng theo vai trò; PAPER recovery không đồng nghĩa đã nối giao dịch."""
        runtime_role = app_settings.process_role == "trading-runtime"
        readiness_items = app.state.readiness.items()
        if runtime_role:
            readiness_items = (
                (name, app.state.readiness[name]) for name in ("database", "paper_runtime")
            )
            paper_application = getattr(app.state, "paper_application", None)
            if paper_application is not None and not paper_application.is_ready:
                app.state.readiness["paper_runtime"] = False
                readiness_items = (
                    (name, app.state.readiness[name]) for name in ("database", "paper_runtime")
                )
        pending = sorted(name for name, is_ready in readiness_items if not is_ready)
        account_summary = _paper_account_readiness_summary(
            getattr(app.state, "paper_account_readiness", {})
        )
        if pending:
            body: dict[str, object] = {"status": "not_ready", "pending": pending}
            if runtime_role:
                body["paper_accounts"] = account_summary
                body["paper_runtime_error_code"] = getattr(
                    app.state, "paper_runtime_error_code", "PAPER_BOOTSTRAP_NOT_RUN"
                )
            return JSONResponse(
                status_code=503,
                content=body,
            )
        if runtime_role:
            return {
                "status": "ready",
                "paper_accounts": account_summary,
                "execution_capabilities": {
                    "paper_recovery": "ready",
                    "market_dispatch": "not_configured",
                    "exchange_execution": "disabled",
                },
            }
        return {"status": "ready"}

    return app


def _paper_account_readiness_summary(account_states: object) -> dict[str, int]:
    """Tổng hợp readiness mà không trả workspace/account identity ra health API."""
    if not isinstance(account_states, Mapping):
        return {
            "total": 0,
            "ready": 0,
            "recovery_required": 0,
            "blocked_by_workspace": 0,
        }
    counts = {
        "total": len(account_states),
        "ready": 0,
        "recovery_required": 0,
        "blocked_by_workspace": 0,
    }
    for state in account_states.values():
        status = getattr(state, "status", "")
        status_value = getattr(status, "value", status)
        if status_value == "READY":
            counts["ready"] += 1
        elif status_value == "RECOVERY_REQUIRED":
            counts["recovery_required"] += 1
        elif status_value == "BLOCKED_BY_WORKSPACE":
            counts["blocked_by_workspace"] += 1
    return counts


app = create_app()
