import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from trading_bot.adapters.database.engine import (
    admin_web_session_timeouts,
    create_database_engine,
    create_session_factory,
    database_url_from_secret,
    runtime_session_timeouts,
)
from trading_bot.adapters.paper.account_activity import PaperLedgerAccountActivityAdapter
from trading_bot.adapters.secrets.local_mfa import LocalMfaSecretProvider
from trading_bot.adapters.telegram.recording_sender import RecordingTelegramSender
from trading_bot.admin.bot_control_router import router as bot_control_router
from trading_bot.admin.bot_reports_router import router as bot_reports_router
from trading_bot.admin.bot_service import BotConfigAdminService
from trading_bot.admin.bots_router import router as bots_router
from trading_bot.admin.settings_router import router as settings_router
from trading_bot.admin.settings_service import SettingsAdminService
from trading_bot.application.account_activity_guard import AccountActivityGuard
from trading_bot.application.bot_operations import BotOperationsService
from trading_bot.application.bot_reporting import BotReportingService
from trading_bot.application.bot_runtime_registry import BotRuntimeRegistry
from trading_bot.application.bot_runtime_supervisor import BotRuntimeSupervisor
from trading_bot.application.paper_runtime_application import (
    PaperBotDecisionCommand,
    PaperRuntimeApplicationService,
)
from trading_bot.application.paper_runtime_bootstrap import (
    TRANSIENT_DATABASE_TIMEOUT,
    PaperRuntimeBootstrapService,
    new_runtime_owner_id,
)
from trading_bot.auth.router import router as auth_router
from trading_bot.auth.security import LoginRateLimiter
from trading_bot.auth.service import AuthService
from trading_bot.common.http_security import SecurityHeadersMiddleware, security_headers
from trading_bot.config.settings import AppSettings
from trading_bot.ports.secrets import SecretProvider
from trading_bot.telegram.bot_commands import BotCommandHandler
from trading_bot.telegram.webhook import (
    DatabaseTelegramStore,
    TelegramWebhookService,
    load_webhook_secret,
)
from trading_bot.telegram.webhook import router as telegram_router

logger = logging.getLogger(__name__)

DATABASE_PROBE_TIMEOUT_SECONDS = 2.0


async def probe_database(
    sessions: Callable[[], Any], *, timeout: float = DATABASE_PROBE_TIMEOUT_SECONDS
) -> bool:
    """Kiểm tra DB rẻ và có giới hạn (`SELECT 1`) cho readiness; lỗi/quá hạn → False."""

    async def _select_one() -> None:
        async with sessions() as session:
            await session.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(_select_one(), timeout=timeout)
    except Exception:
        return False
    return True


def build_database_engine(settings: AppSettings) -> AsyncEngine | None:
    """Engine production theo role; None khi chưa cấu hình credential DB.

    `trading-runtime` đặt timeout phiên suy từ TTL lease (fix round 1, I2): worker treo giữa
    transaction không giữ L1/`FOR SHARE` lease quá TTL/3, mọi chờ khóa có giới hạn.
    """
    if not (settings.database_user and settings.database_password_file):
        return None
    database_url = database_url_from_secret(
        username=settings.database_user,
        password_file=settings.database_password_file,
        host=settings.database_host,
        database=settings.database_name,
    )
    if settings.process_role == "trading-runtime":
        return create_database_engine(
            database_url,
            session_timeouts=runtime_session_timeouts(
                timedelta(seconds=settings.runtime_lease_ttl_seconds)
            ),
        )
    if settings.process_role == "admin-web":
        # MB-07a ruling 40: admin-web giữ L1 khi disable/control; mọi chờ/treo có giới hạn.
        return create_database_engine(database_url, session_timeouts=admin_web_session_timeouts())
    return create_database_engine(database_url)


def create_app(
    settings: AppSettings | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    secret_provider: SecretProvider | None = None,
    paper_runtime_bootstrap_factory: Callable[..., Any] | None = None,
    paper_decision_context_provider_factory: Callable[..., Any] | None = None,
    paper_runtime_supervisor_factory: Callable[..., Any] | None = None,
    database_probe: Callable[[], Awaitable[bool]] | None = None,
) -> FastAPI:
    """Tạo ứng dụng mà không kết nối mạng hay tải credential khi import."""
    app_settings = settings or AppSettings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        engine: AsyncEngine | None = None
        active_session_factory = session_factory
        active_secret_provider = secret_provider
        if active_session_factory is None:
            engine = build_database_engine(app_settings)
            if engine is not None:
                active_session_factory = create_session_factory(engine)
        application.state.database_probe = database_probe
        if database_probe is None and isinstance(active_session_factory, async_sessionmaker):
            probe_sessions = active_session_factory

            async def _default_probe() -> bool:
                return await probe_database(probe_sessions)

            application.state.database_probe = _default_probe
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
        application.state.bot_reporting_service = (
            BotReportingService(active_session_factory)
            if active_session_factory is not None
            else None
        )
        application.state.bot_operations_service = (
            BotOperationsService(active_session_factory)
            if active_session_factory is not None
            else None
        )
        application.state.telegram_sender = RecordingTelegramSender()
        application.state.telegram_webhook = _build_telegram_webhook(
            app_settings, active_session_factory, application.state
        )
        stopping = asyncio.Event()
        supervisor_task: asyncio.Task[None] | None = None
        if app_settings.process_role == "trading-runtime":
            supervisor_task = await _start_paper_runtime(
                application,
                app_settings=app_settings,
                session_factory=active_session_factory,
                bootstrap_factory=paper_runtime_bootstrap_factory,
                context_provider_factory=paper_decision_context_provider_factory,
                supervisor_factory=paper_runtime_supervisor_factory,
                stopping=stopping,
            )
        try:
            yield
        finally:
            stopping.set()
            supervisor = getattr(application.state, "paper_supervisor", None)
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
    app.include_router(bot_reports_router)
    app.include_router(bot_control_router)
    app.include_router(telegram_router)

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
    async def ready_health() -> JSONResponse | dict[str, object]:
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
        error_code = getattr(app.state, "paper_runtime_error_code", "PAPER_BOOTSTRAP_NOT_RUN")
        probe = getattr(app.state, "database_probe", None)
        if not pending and probe is not None and not await probe():
            # Gap (b): DB mất sau khi khởi động → 503 thay vì giữ cờ bootstrap cũ.
            pending = ["database"]
            error_code = "DATABASE_UNAVAILABLE"
        if pending:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "pending": pending,
                    "paper_accounts": summary,
                    "paper_runtime_error_code": error_code,
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


def _build_telegram_webhook(
    settings: AppSettings,
    sessions: async_sessionmaker[AsyncSession] | None,
    state: Any,
) -> TelegramWebhookService | None:
    """Webhook Telegram chỉ bật khi admin-web cấu hình workspace + secret và có DB (ruling 37)."""
    if (
        settings.telegram_workspace_id is None
        or settings.telegram_webhook_secret_file is None
        or sessions is None
        or state.bot_operations_service is None
    ):
        return None
    return TelegramWebhookService(
        workspace_id=settings.telegram_workspace_id,
        secret=load_webhook_secret(settings.telegram_webhook_secret_file),
        store=DatabaseTelegramStore(sessions),
        handler=BotCommandHandler(state.bot_operations_service),
        sender=state.telegram_sender,
    )


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
    stopping: asyncio.Event,
) -> asyncio.Task[None] | None:
    """Bootstrap theo bot, dựng registry + application, chạy supervisor (MB05.5).

    Không còn cờ readiness toàn process gắn với một account: lỗi/mất lease của một bot chỉ đổi
    trạng thái bot đó trong registry (WBS C4). Timeout tạm thời ở pha workspace (lock/statement,
    fix round 2) không thành 503 dính: task nền thử lại bootstrap với backoff có trần.
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
    except Exception:
        bootstrap = None
    wiring: dict[str, Any] = {
        "app_settings": app_settings,
        "session_factory": session_factory,
        "context_provider_factory": context_provider_factory,
        "supervisor_factory": supervisor_factory,
        "lease_ttl": lease_ttl,
    }
    result = await _run_bootstrap(bootstrap)
    if _bootstrap_is_transient(result):
        _record_bootstrap_result(state, result)
        logger.warning(
            "PAPER runtime bootstrap timeout tạm thời; sẽ thử lại; diagnostic_code=%s",
            state.paper_runtime_error_code,
        )
        return asyncio.create_task(
            _retry_paper_runtime(application, bootstrap, stopping=stopping, **wiring)
        )
    supervisor = await _wire_paper_runtime(application, bootstrap, result, **wiring)
    return asyncio.create_task(supervisor.run()) if supervisor is not None else None


async def _run_bootstrap(bootstrap: Any) -> Any:
    if bootstrap is None:
        return None
    try:
        return await bootstrap.bootstrap()
    except Exception:
        return None


def _bootstrap_is_transient(result: Any) -> bool:
    return (
        result is not None
        and not result.ready
        and getattr(result, "error_code", None) == TRANSIENT_DATABASE_TIMEOUT
    )


def _record_bootstrap_result(state: Any, result: Any) -> None:
    state.paper_runtime = result
    state.paper_accounts = result.active_accounts
    state.paper_account_readiness = result.account_states
    state.paper_runtime_error_code = result.error_code
    state.readiness["database"] = result.database_ready
    state.readiness["paper_runtime"] = result.ready


async def _retry_paper_runtime(
    application: FastAPI,
    bootstrap: Any,
    *,
    stopping: asyncio.Event,
    app_settings: AppSettings,
    **wiring: Any,
) -> None:
    """Thử lại bootstrap (backoff ×2 từ một tick, trần = TTL lease) tới khi không còn timeout."""
    delay = app_settings.runtime_supervisor_tick_seconds
    cap = max(float(app_settings.runtime_lease_ttl_seconds), delay)
    while not stopping.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=delay)
        if stopping.is_set():
            return
        result = await _run_bootstrap(bootstrap)
        if _bootstrap_is_transient(result):
            _record_bootstrap_result(application.state, result)
            delay = min(delay * 2, cap)
            continue
        supervisor = await _wire_paper_runtime(
            application, bootstrap, result, app_settings=app_settings, **wiring
        )
        if supervisor is None:
            return
        if stopping.is_set():
            await supervisor.stop()
            return
        await supervisor.run()
        return


async def _wire_paper_runtime(
    application: FastAPI,
    bootstrap: Any,
    result: Any,
    *,
    app_settings: AppSettings,
    session_factory: async_sessionmaker[AsyncSession] | None,
    context_provider_factory: Callable[..., Any] | None,
    supervisor_factory: Callable[..., Any] | None,
    lease_ttl: timedelta,
) -> Any | None:
    """Từ kết quả bootstrap: registry, application, supervisor + tick đầu đồng bộ."""
    state = application.state
    if result is None:
        state.paper_runtime_error_code = "PAPER_BOOTSTRAP_FAILED"
        logger.error(
            "PAPER runtime bootstrap failed; diagnostic_code=%s", state.paper_runtime_error_code
        )
        return None
    registry = BotRuntimeRegistry.from_bootstrap(result)
    state.paper_registry = registry
    _record_bootstrap_result(state, result)
    if not result.ready:
        return None
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
            lease_lost_callback=registry.mark_lease_lost,
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
        return None
    if not hasattr(bootstrap, "bootstrap_bot") or getattr(result, "owner_id", None) is None:
        # Bootstrap giả (test cũ) không quản lý lease: không chạy supervisor.
        return None
    supervisor_builder = supervisor_factory or _default_supervisor
    job_processor = _runtime_job_processor(
        context_provider=context_provider,
        sessions=session_factory,
        workspace_id=result.workspace_id,
        application=paper_application,
    )
    supervisor = supervisor_builder(
        registry=registry,
        application=paper_application,
        bootstrap=bootstrap,
        sessions=session_factory,
        workspace_id=result.workspace_id,
        tick_seconds=app_settings.runtime_supervisor_tick_seconds,
        lease_ttl=lease_ttl,
        job_processor=job_processor,
    )
    state.paper_supervisor = supervisor
    # Tick đầu chạy đồng bộ: áp control đã có và xác minh activity trước khi nhận command.
    await supervisor.tick_once()
    return supervisor


def _runtime_job_processor(
    *, context_provider: Any, sessions: Any, workspace_id: Any, application: Any
) -> Callable[..., Any] | None:
    """Nối worker job khi context provider có thể dựng snapshot decision đã lưu."""
    if sessions is None or not callable(getattr(context_provider, "snapshot_for", None)):
        return None
    decision_snapshot_provider = context_provider
    from trading_bot.adapters.database.repositories.bot_decisions import (
        PostgresSignalDecisionStore,
    )
    from trading_bot.application.bot_decision_contexts import (
        EffectiveBotDecisionContextProvider,
        FanOutExclusionRecorder,
        RuntimeFanOutGate,
    )
    from trading_bot.application.bot_dispatcher import (
        BotEvidenceDispatcher,
        SignalEvidenceFanOutConsumer,
    )
    from trading_bot.domain.strategy.registry import StrategyRegistry

    decision_contexts = EffectiveBotDecisionContextProvider(
        sessions,
        runtime_gate=RuntimeFanOutGate(),
        exclusion_recorder=FanOutExclusionRecorder(
            sessions=sessions,
            workspace_id=workspace_id,
            accounts=application.runtime_account,
        ),
    )
    store = PostgresSignalDecisionStore(sessions)
    dispatcher = BotEvidenceDispatcher(
        store,
        strategies=StrategyRegistry(),
        snapshots=decision_snapshot_provider,
    )
    consumer = SignalEvidenceFanOutConsumer(
        store,
        dispatcher,
        decision_contexts,
        worker_id=f"paper-runtime-{uuid4().hex}",
    )

    async def process_pending(account_id: Any, now: Any) -> Any:
        await consumer.consume_once(workspace_id=workspace_id, now=now)
        await dispatcher.process_pending(workspace_id=workspace_id, account_id=account_id, now=now)
        pending_candidates = await store.pending_candidate_decisions(
            workspace_id=workspace_id, account_id=account_id, now=now
        )
        return await _submit_bot_candidates(application, pending_candidates)

    return process_pending


async def _submit_bot_candidates(application: Any, decisions: Any) -> tuple[Any, ...]:
    """Chuyển candidate đã lưu thành PAPER command riêng; abstain/veto không đặt lệnh."""
    from trading_bot.application.bot_dispatcher import BotDecisionStatus

    results = []
    for decision in decisions:
        if decision.status is not BotDecisionStatus.CANDIDATE:
            continue
        results.append(
            await application.evaluate_and_submit(
                PaperBotDecisionCommand(
                    account_id=decision.account_id,
                    bot_decision_id=decision.decision_id,
                )
            )
        )
    return tuple(results)


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
