import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
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
from trading_bot.adapters.paper.account_activity import PaperLedgerAccountActivityAdapter
from trading_bot.adapters.secrets.local_mfa import LocalMfaSecretProvider
from trading_bot.admin.bot_service import BotConfigAdminService
from trading_bot.admin.bots_router import router as bots_router
from trading_bot.admin.settings_router import router as settings_router
from trading_bot.admin.settings_service import SettingsAdminService
from trading_bot.application.account_activity_guard import AccountActivityGuard
from trading_bot.application.bot_runtime_registry import BotRuntimeRegistry
from trading_bot.application.bot_runtime_supervisor import BotRuntimeSupervisor
from trading_bot.application.paper_runtime_application import (
    PaperRuntimeApplicationService,
)
from trading_bot.application.paper_runtime_bootstrap import (
    PaperRuntimeBootstrapService,
    new_runtime_owner_id,
)
from trading_bot.auth.router import router as auth_router
from trading_bot.auth.security import LoginRateLimiter
from trading_bot.auth.service import AuthService
from trading_bot.common.http_security import SecurityHeadersMiddleware, security_headers
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
    paper_runtime_supervisor_factory: Callable[..., Any] | None = None,
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
        application.state.bot_admin_service = (
            BotConfigAdminService(active_session_factory)
            if active_session_factory is not None
            else None
        )
        supervisor: Any | None = None
        supervisor_task: asyncio.Task[None] | None = None
        if app_settings.process_role == "trading-runtime":
            supervisor, supervisor_task = await _start_paper_runtime(
                application,
                app_settings=app_settings,
                session_factory=active_session_factory,
                bootstrap_factory=paper_runtime_bootstrap_factory,
                context_provider_factory=paper_decision_context_provider_factory,
                supervisor_factory=paper_runtime_supervisor_factory,
            )
        try:
            yield
        finally:
            if supervisor is not None:
                await supervisor.stop()
            if supervisor_task is not None:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(supervisor_task, timeout=10)
            if engine is not None:
                await engine.dispose()

    app = FastAPI(
        title="Crypidian Trading Bot",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(SecurityHeadersMiddleware, headers=security_headers(app_settings))
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
    app.include_router(bots_router)

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
        """Readiness theo vai trò; runtime: 200 `ready`/`degraded`, 503 khi không phục vụ được.

        Ruling 2/26: một bot lỗi/pause/standby chỉ làm `degraded`; 503 chỉ khi DB không truy cập
        được, bootstrap/wiring thất bại hoặc điều kiện workspace lỗi. Body chỉ có bộ đếm.
        """
        runtime_role = app_settings.process_role == "trading-runtime"
        if not runtime_role:
            pending = sorted(name for name, ready in app.state.readiness.items() if not ready)
            if pending:
                return JSONResponse(
                    status_code=503, content={"status": "not_ready", "pending": pending}
                )
            return {"status": "ready"}
        registry = getattr(app.state, "paper_registry", None)
        summary = registry.summary() if registry is not None else _empty_summary()
        pending = sorted(
            name for name in ("database", "paper_runtime") if not app.state.readiness[name]
        )
        if pending:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "pending": pending,
                    "paper_accounts": summary,
                    "paper_runtime_error_code": getattr(
                        app.state, "paper_runtime_error_code", "PAPER_BOOTSTRAP_NOT_RUN"
                    ),
                },
            )
        return {
            "status": registry.status() if registry is not None else "degraded",
            "workspace_control": (
                registry.workspace_control() if registry is not None else "active"
            ),
            "paper_accounts": summary,
            "execution_capabilities": {
                "paper_recovery": "ready",
                "market_dispatch": "not_configured",
                "exchange_execution": "disabled",
            },
        }

    return app


def _empty_summary() -> dict[str, int]:
    return {
        "total": 0,
        "ready": 0,
        "recovery_required": 0,
        "standby": 0,
        "paused": 0,
        "activity_blocked": 0,
        "blocked_by_workspace": 0,
    }


async def _start_paper_runtime(
    application: FastAPI,
    *,
    app_settings: AppSettings,
    session_factory: async_sessionmaker[AsyncSession] | None,
    bootstrap_factory: Callable[..., Any] | None,
    context_provider_factory: Callable[..., Any] | None,
    supervisor_factory: Callable[..., Any] | None,
) -> tuple[Any | None, asyncio.Task[None] | None]:
    """Bootstrap theo bot, dựng registry + application, chạy supervisor (MB05.5).

    Không còn cờ readiness toàn process gắn với một account: lỗi/mất lease của một bot chỉ đổi
    trạng thái bot đó trong registry (WBS C4).
    """
    state = application.state
    state.paper_runtime = None
    state.paper_application = None
    state.paper_registry = None
    state.paper_supervisor = None
    state.paper_accounts = {}
    state.paper_account_readiness = {}
    state.paper_runtime_error_code = "PAPER_BOOTSTRAP_NOT_RUN"
    owner_id = new_runtime_owner_id()
    lease_ttl = timedelta(seconds=app_settings.runtime_lease_ttl_seconds)
    factory = bootstrap_factory or PaperRuntimeBootstrapService
    bootstrap: Any = None
    try:
        workspace_id = app_settings.runtime_workspace_id
        if workspace_id is None:
            raise RuntimeError("trading-runtime deployment workspace chưa cấu hình.")
        bootstrap = factory(
            sessions=session_factory,
            workspace_id=workspace_id,
            owner_id=owner_id,
            lease_ttl=lease_ttl,
        )
        result = await bootstrap.bootstrap()
    except Exception:
        result = None
    if result is None:
        state.paper_runtime_error_code = "PAPER_BOOTSTRAP_FAILED"
        logger.error(
            "PAPER runtime bootstrap failed; diagnostic_code=%s", state.paper_runtime_error_code
        )
        return None, None
    registry = BotRuntimeRegistry.from_bootstrap(result)
    state.paper_runtime = result
    state.paper_registry = registry
    state.paper_accounts = result.active_accounts
    state.paper_account_readiness = result.account_states
    state.paper_runtime_error_code = result.error_code
    state.readiness["database"] = result.database_ready
    state.readiness["paper_runtime"] = result.ready
    if not result.ready:
        return None, None
    try:
        context_provider = (
            context_provider_factory(
                sessions=session_factory,
                workspace_id=result.workspace_id,
                active_accounts=result.active_accounts,
            )
            if context_provider_factory is not None
            else None
        )
        paper_application = PaperRuntimeApplicationService(
            workspace_id=result.workspace_id,
            sessions=session_factory,
            active_accounts=result.active_accounts,
            context_provider=context_provider,
            account_invalidation_callback=registry.mark_recovery,
            lease_lost_callback=registry.mark_standby,
        )
        state.paper_application = paper_application
    except Exception:
        state.paper_application = None
        state.paper_accounts = {}
        state.readiness["paper_runtime"] = False
        state.paper_runtime_error_code = "PAPER_APPLICATION_WIRING_FAILED"
        logger.error(
            "PAPER application wiring failed; diagnostic_code=%s", state.paper_runtime_error_code
        )
        return None, None
    if not hasattr(bootstrap, "bootstrap_bot") or getattr(result, "owner_id", None) is None:
        # Bootstrap giả (test cũ) không quản lý lease: không chạy supervisor.
        return None, None
    supervisor_builder = supervisor_factory or _default_supervisor
    supervisor = supervisor_builder(
        registry=registry,
        application=paper_application,
        bootstrap=bootstrap,
        sessions=session_factory,
        workspace_id=result.workspace_id,
        tick_seconds=app_settings.runtime_supervisor_tick_seconds,
        lease_ttl=lease_ttl,
    )
    state.paper_supervisor = supervisor
    # Tick đầu chạy đồng bộ: áp control đã có và xác minh activity trước khi nhận command.
    await supervisor.tick_once()
    return supervisor, asyncio.create_task(supervisor.run())


def _default_supervisor(**kwargs: Any) -> BotRuntimeSupervisor:
    application = kwargs["application"]
    activity_guard = AccountActivityGuard(
        sessions=kwargs["sessions"],
        workspace_id=kwargs["workspace_id"],
        accounts=application.runtime_account,
        adapter=PaperLedgerAccountActivityAdapter(
            workspace_id=kwargs["workspace_id"],
            gateways=application.gateway_view(),
            clock=lambda: datetime.now(UTC),
        ),
    )
    return BotRuntimeSupervisor(activity_guard=activity_guard, **kwargs)


app = create_app()
