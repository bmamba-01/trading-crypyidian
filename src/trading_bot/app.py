import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from trading_bot.adapters.database.engine import (
    admin_web_session_timeouts,
    create_database_engine,
    create_session_factory,
    database_url_from_secret,
    runtime_session_timeouts,
)
from trading_bot.adapters.database.repositories.flatten_snapshot import (
    PostgresFlattenSnapshotProvider,
)
from trading_bot.adapters.database.source_policy_admin import DatabaseSourcePolicyAdminRepository
from trading_bot.adapters.paper.account_activity import PaperLedgerAccountActivityAdapter
from trading_bot.adapters.secrets.local_mfa import LocalMfaSecretProvider
from trading_bot.adapters.telegram.bot_api_sender import (
    TelegramBotApiSender,
    load_telegram_bot_token,
)
from trading_bot.adapters.telegram.recording_sender import RecordingTelegramSender
from trading_bot.admin.backtest_replay_router import router as backtest_replay_router
from trading_bot.admin.bot_control_router import router as bot_control_router
from trading_bot.admin.bot_reports_router import router as bot_reports_router
from trading_bot.admin.bot_service import BotConfigAdminService
from trading_bot.admin.bots_router import router as bots_router
from trading_bot.admin.learning_incident_entry_hold_router import (
    router as learning_incident_entry_hold_router,
)
from trading_bot.admin.learning_model_registry_page_router import (
    router as learning_model_registry_page_router,
)
from trading_bot.admin.learning_model_registry_router import (
    router as learning_model_registry_router,
)
from trading_bot.admin.learning_observations_page_router import (
    router as learning_observations_page_router,
)
from trading_bot.admin.learning_paper_performance_router import (
    router as learning_paper_performance_router,
)
from trading_bot.admin.learning_scoring_deployment_router import (
    router as learning_scoring_deployment_router,
)
from trading_bot.admin.learning_scoring_page_router import (
    router as learning_scoring_page_router,
)
from trading_bot.admin.learning_split_page_router import router as learning_split_page_router
from trading_bot.admin.learning_split_router import router as learning_split_router
from trading_bot.admin.learning_training_page_router import (
    router as learning_training_page_router,
)
from trading_bot.admin.learning_training_router import router as learning_training_router
from trading_bot.admin.research_source_router import admin_router as research_source_admin_router
from trading_bot.admin.research_source_router import router as research_source_router
from trading_bot.admin.settings_router import router as settings_router
from trading_bot.admin.settings_service import SettingsAdminService
from trading_bot.admin.strategy_card_service import StrategyCardAdminService
from trading_bot.admin.telegram_binding_router import router as telegram_binding_router
from trading_bot.admin.telegram_signal_router import router as telegram_signal_router
from trading_bot.admin.web_router import router as admin_web_router
from trading_bot.application.account_activity_guard import AccountActivityGuard
from trading_bot.application.backtest_replay_jobs import BacktestReplayJobService
from trading_bot.application.bot_operations import BotOperationsService
from trading_bot.application.bot_reporting import BotReportingService
from trading_bot.application.bot_runtime_registry import BotRuntimeRegistry
from trading_bot.application.bot_runtime_supervisor import BotRuntimeSupervisor
from trading_bot.application.flatten_all import FlattenAllService
from trading_bot.application.learning_incident_entry_hold import (
    LearningIncidentEntryHoldService,
)
from trading_bot.application.learning_model_registry import LearningModelRegistryService
from trading_bot.application.learning_observations_admin import (
    LearningObservationsAdminService,
)
from trading_bot.application.learning_paper_performance import LearningPaperPerformanceService
from trading_bot.application.learning_scorer_report_read_model import ScorerReportReadModel
from trading_bot.application.learning_scorer_score_event_read_model import (
    ScorerScoreEventReadModel,
)
from trading_bot.application.learning_scoring_deployment import ScorerDeploymentService
from trading_bot.application.learning_split_admin import LearningSplitAdminService
from trading_bot.application.learning_training_admin import LearningTrainingAdminService
from trading_bot.application.live_kline_ingestion_composition import (
    build_live_market_ingestion,
)
from trading_bot.application.market_signal_composition import build_market_signal_scanner
from trading_bot.application.paper_runtime_application import (
    PaperBotDecisionCommand,
    PaperRuntimeApplicationService,
)
from trading_bot.application.paper_runtime_bootstrap import (
    TRANSIENT_DATABASE_TIMEOUT,
    PaperRuntimeBootstrapService,
    new_runtime_owner_id,
)
from trading_bot.application.scorer_runtime_policy import (
    ConfiguredScorerRuntimePolicyProvider,
    ScorerRuntimePolicyProvider,
)
from trading_bot.application.source_policy_admin import (
    SourcePolicyAdminRepository,
    SourcePolicyAdminService,
)
from trading_bot.application.telegram_binding import TelegramBindingService
from trading_bot.application.telegram_signal_subscription import TelegramSignalSubscriptionService
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
from trading_bot.workers.learning_training_supervisor import LearningTrainingJobSupervisor
from trading_bot.workers.learning_training_worker import LearningTrainingAttemptWorker
from trading_bot.workers.market_signal_producer import MarketSignalProducerWorker
from trading_bot.workers.research_rights_worker import ResearchRightsMaintenanceWorker
from trading_bot.workers.telegram_signal_worker import TelegramSignalDeliveryWorker

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
    rights_worker_factory: Callable[..., Any] | None = None,
    live_market_ingestion_factory: Callable[..., Any] | None = None,
    signal_worker_factory: Callable[..., Any] | None = None,
    market_signal_worker_factory: Callable[..., Any] | None = None,
    training_supervisor_factory: Callable[..., Any] | None = None,
    database_probe: Callable[[], Awaitable[bool]] | None = None,
) -> FastAPI:
    """Tạo ứng dụng mà không kết nối mạng hay tải credential khi import."""
    app_settings = settings or AppSettings()
    admin_web_role = app_settings.process_role == "admin-web"
    scorer_policy_provider = _configured_scorer_policy_provider(app_settings)

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
        application.state.scorer_runtime_policy_provider = scorer_policy_provider
        if database_probe is None and isinstance(active_session_factory, async_sessionmaker):
            probe_sessions = active_session_factory

            async def _default_probe() -> bool:
                return await probe_database(probe_sessions)

            application.state.database_probe = _default_probe
        application.state.scorer_runtime_policy_provider = scorer_policy_provider
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
        application.state.source_policy_admin_service = (
            SourcePolicyAdminService(
                cast(
                    SourcePolicyAdminRepository,
                    DatabaseSourcePolicyAdminRepository(active_session_factory),
                )
            )
            if active_session_factory is not None
            else None
        )
        application.state.bot_reporting_service = (
            BotReportingService(active_session_factory)
            if active_session_factory is not None
            else None
        )
        application.state.backtest_replay_job_service = (
            BacktestReplayJobService(active_session_factory)
            if active_session_factory is not None
            else None
        )
        application.state.bot_operations_service = (
            BotOperationsService(active_session_factory)
            if active_session_factory is not None
            else None
        )
        application.state.strategy_card_service = (
            StrategyCardAdminService(active_session_factory)
            if active_session_factory is not None
            else StrategyCardAdminService()
        )
        application.state.flatten_all_service = (
            FlattenAllService(
                active_session_factory,
                snapshot_provider=PostgresFlattenSnapshotProvider(),
                operations_factory=application.state.bot_operations_service.for_principal,
            )
            if active_session_factory is not None
            else None
        )
        application.state.telegram_binding_service = (
            TelegramBindingService(
                DatabaseTelegramStore(active_session_factory),
                bot_username=app_settings.telegram_bot_username,
            )
            if active_session_factory is not None and app_settings.telegram_bot_username
            else None
        )
        application.state.telegram_signal_subscription_service = (
            TelegramSignalSubscriptionService(active_session_factory)
            if admin_web_role and active_session_factory is not None
            else None
        )
        if admin_web_role:
            application.state.learning_model_registry_service = (
                LearningModelRegistryService(active_session_factory)
                if active_session_factory is not None
                else None
            )
            application.state.learning_scoring_deployment_service = (
                ScorerDeploymentService(
                    active_session_factory,
                    policy_provider=scorer_policy_provider,
                )
                if active_session_factory is not None
                else None
            )
            application.state.learning_scoring_report_read_model = (
                ScorerReportReadModel(active_session_factory)
                if active_session_factory is not None
                else None
            )
            application.state.learning_scoring_score_event_read_model = (
                ScorerScoreEventReadModel(active_session_factory)
                if active_session_factory is not None
                else None
            )
            application.state.learning_observations_admin_service = (
                LearningObservationsAdminService(active_session_factory)
                if active_session_factory is not None
                else None
            )
            application.state.learning_incident_entry_hold_service = (
                LearningIncidentEntryHoldService(active_session_factory)
                if active_session_factory is not None
                else None
            )
            application.state.learning_paper_performance_service = (
                LearningPaperPerformanceService(active_session_factory)
                if active_session_factory is not None
                else None
            )
            application.state.learning_training_admin_service = (
                LearningTrainingAdminService(active_session_factory)
                if active_session_factory is not None
                else None
            )
            application.state.learning_split_admin_service = (
                LearningSplitAdminService(active_session_factory)
                if active_session_factory is not None
                else None
            )
        telegram_sender = _build_telegram_sender(
            app_settings,
            required=app_settings.process_role == "signal-worker",
        )
        application.state.telegram_sender = telegram_sender
        application.state.telegram_webhook = _build_telegram_webhook(
            app_settings, active_session_factory, application.state
        )
        stopping = asyncio.Event()
        supervisor_task: asyncio.Task[None] | None = None
        rights_worker_task: asyncio.Task[None] | None = None
        live_market_runtime: Any | None = None
        live_market_task: asyncio.Task[None] | None = None
        signal_worker_task: asyncio.Task[None] | None = None
        market_signal_worker_task: asyncio.Task[None] | None = None
        training_supervisor_task: asyncio.Task[None] | None = None
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
        elif app_settings.process_role == "ingestion-worker":
            if active_session_factory is None:
                raise RuntimeError("ingestion-worker cần database session factory.")
            workspace_id = app_settings.ingestion_workspace_id
            if workspace_id is None:
                raise RuntimeError("ingestion-worker cần ingestion_workspace_id.")
            worker_factory = rights_worker_factory or ResearchRightsMaintenanceWorker
            rights_worker = worker_factory(
                session_factory=active_session_factory,
                workspace_id=workspace_id,
                worker_id=f"rights-{uuid4().hex}",
                tick_seconds=app_settings.ingestion_worker_tick_seconds,
            )
            application.state.rights_worker = rights_worker
            rights_worker_task = asyncio.create_task(
                rights_worker.run(stopping), name="research-rights-maintenance-worker"
            )
            application.state.rights_worker_task = rights_worker_task
            live_subscriptions = app_settings.ingestion_live_subscriptions
            if live_subscriptions:
                # Live ingest chỉ đọc market data công khai; không nhận API key hay cổng lệnh.
                live_factory = live_market_ingestion_factory or build_live_market_ingestion
                live_market_runtime = live_factory(
                    session_factory=active_session_factory,
                    workspace_id=workspace_id,
                    subscriptions=live_subscriptions,
                    warmup_bars=app_settings.ingestion_live_warmup_bars,
                )
                application.state.live_market_worker = live_market_runtime.worker
                live_market_task = asyncio.create_task(
                    live_market_runtime.worker.run(stopping), name="live-market-ingestion-worker"
                )
                application.state.live_market_task = live_market_task
        elif app_settings.process_role == "trainer-worker":
            if active_session_factory is None:
                raise RuntimeError("trainer-worker cần database session factory.")
            workspace_id = app_settings.learning_training_workspace_id
            if workspace_id is None:
                raise RuntimeError("trainer-worker cần learning_training_workspace_id.")
            supervisor_factory = training_supervisor_factory or LearningTrainingJobSupervisor
            training_supervisor = supervisor_factory(
                worker=LearningTrainingAttemptWorker(active_session_factory),
                workspace_id=workspace_id,
                worker_id=f"learning-trainer-{uuid4().hex}",
                poll_seconds=app_settings.learning_training_worker_poll_seconds,
                max_backoff_seconds=app_settings.learning_training_worker_max_backoff_seconds,
            )
            application.state.learning_training_supervisor = training_supervisor
            training_supervisor_task = asyncio.create_task(
                training_supervisor.run(stopping), name="learning-training-job-supervisor"
            )
            application.state.learning_training_supervisor_task = training_supervisor_task
        elif app_settings.process_role == "signal-worker":
            if active_session_factory is None:
                raise RuntimeError("signal-worker cần database session factory.")
            workspace_id = app_settings.signal_workspace_id
            if workspace_id is None:
                raise RuntimeError("signal-worker cần signal_workspace_id.")
            worker_factory = signal_worker_factory or TelegramSignalDeliveryWorker
            signal_worker = worker_factory(
                session_factory=active_session_factory,
                workspace_id=workspace_id,
                sender=telegram_sender,
                worker_id=f"telegram-signal-{uuid4().hex}",
                tick_seconds=app_settings.signal_worker_tick_seconds,
                max_candidate_age=timedelta(
                    seconds=app_settings.signal_worker_max_candidate_age_seconds
                ),
                max_attempts=app_settings.signal_worker_max_attempts,
            )
            application.state.telegram_signal_worker = signal_worker
            signal_worker_task = asyncio.create_task(
                signal_worker.run(stopping), name="telegram-signal-delivery-worker"
            )
            application.state.telegram_signal_worker_task = signal_worker_task
        elif app_settings.process_role == "market-signal-worker":
            if active_session_factory is None:
                raise RuntimeError("market-signal-worker cần database session factory.")
            workspace_id = app_settings.market_signal_workspace_id
            if workspace_id is None:
                raise RuntimeError("market-signal-worker cần market_signal_workspace_id.")
            producer_factory = market_signal_worker_factory
            if producer_factory is None:
                max_age = timedelta(seconds=app_settings.signal_worker_max_candidate_age_seconds)

                def default_market_signal_worker_factory(**kwargs: Any) -> Any:
                    scanner = build_market_signal_scanner(
                        kwargs["session_factory"],
                        max_observation_age=max_age,
                        signal_validity=max_age,
                        scorer_policy_provider=scorer_policy_provider,
                    )
                    return MarketSignalProducerWorker(
                        scanner,
                        workspace_id=kwargs["workspace_id"],
                        tick_seconds=kwargs["tick_seconds"],
                    )

                producer_factory = default_market_signal_worker_factory
            market_signal_worker = producer_factory(
                session_factory=active_session_factory,
                workspace_id=workspace_id,
                tick_seconds=app_settings.signal_worker_tick_seconds,
            )
            application.state.market_signal_worker = market_signal_worker
            market_signal_worker_task = asyncio.create_task(
                market_signal_worker.run(stopping), name="market-signal-producer-worker"
            )
            application.state.market_signal_worker_task = market_signal_worker_task
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
            if rights_worker_task is not None:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(rights_worker_task, timeout=10)
            if live_market_task is not None:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(live_market_task, timeout=10)
            if live_market_runtime is not None:
                await live_market_runtime.aclose()
            if signal_worker_task is not None:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(signal_worker_task, timeout=10)
            if market_signal_worker_task is not None:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(market_signal_worker_task, timeout=10)
            if training_supervisor_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await training_supervisor_task
            close_sender = getattr(telegram_sender, "aclose", None)
            if callable(close_sender):
                await close_sender()
            if engine is not None:
                await engine.dispose()

    app = FastAPI(
        title="Crypidian Trading Bot",
        openapi_url=None
        if app_settings.process_role in {"signal-worker", "market-signal-worker"}
        else "/openapi.json",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(SecurityHeadersMiddleware, headers=security_headers(app_settings))
    app.state.settings = app_settings
    app.state.templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    worker_role = app_settings.process_role in {"signal-worker", "market-signal-worker"}
    if not worker_role:
        app.mount(
            "/static",
            StaticFiles(directory=str(Path(__file__).parent / "static")),
            name="static",
        )
    app.state.login_limiter = LoginRateLimiter()
    app.state.readiness = {
        "database": False,
        "market_data": False,
        "exchange": False,
    }
    if app_settings.process_role == "trading-runtime":
        app.state.readiness["paper_runtime"] = False
        app.state.paper_application = None
    if not worker_role:
        app.include_router(auth_router)
        app.include_router(settings_router)
        app.include_router(bots_router)
        app.include_router(bot_reports_router)
        app.include_router(backtest_replay_router)
        app.include_router(bot_control_router)
        app.include_router(telegram_binding_router)
        app.include_router(telegram_signal_router)
        app.include_router(research_source_router)
        app.include_router(research_source_admin_router)
        if admin_web_role:
            app.include_router(learning_model_registry_router)
            app.include_router(learning_scoring_deployment_router)
            app.include_router(learning_model_registry_page_router)
            app.include_router(learning_observations_page_router)
            app.include_router(learning_paper_performance_router)
            app.include_router(learning_incident_entry_hold_router)
            app.include_router(learning_scoring_page_router)
            app.include_router(learning_training_router)
            app.include_router(learning_training_page_router)
            app.include_router(learning_split_router)
            app.include_router(learning_split_page_router)
        app.include_router(admin_web_router)
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
        ingestion_role = app_settings.process_role == "ingestion-worker"
        signal_role = app_settings.process_role == "signal-worker"
        market_signal_role = app_settings.process_role == "market-signal-worker"
        trainer_role = app_settings.process_role == "trainer-worker"
        if ingestion_role:
            pending: list[str] = []
            probe = getattr(app.state, "database_probe", None)
            if probe is None or not await probe():
                pending.append("database")
            worker = getattr(app.state, "rights_worker", None)
            worker_task = getattr(app.state, "rights_worker_task", None)
            if (
                worker is None
                or not getattr(worker, "last_tick_succeeded", False)
                or worker_task is None
                or worker_task.done()
            ):
                pending.append("rights_worker")
            if app_settings.ingestion_live_subscriptions:
                live_worker = getattr(app.state, "live_market_worker", None)
                live_task = getattr(app.state, "live_market_task", None)
                if (
                    live_worker is None
                    or not getattr(live_worker, "is_healthy", False)
                    or live_task is None
                    or live_task.done()
                ):
                    pending.append("live_market_ingestion")
            if pending:
                return JSONResponse(
                    status_code=503,
                    content={"status": "not_ready", "pending": sorted(pending)},
                )
            return {"status": "ready"}
        if signal_role:
            pending = []
            probe = getattr(app.state, "database_probe", None)
            if probe is None or not await probe():
                pending.append("database")
            worker = getattr(app.state, "telegram_signal_worker", None)
            worker_task = getattr(app.state, "telegram_signal_worker_task", None)
            if (
                worker is None
                or not getattr(worker, "last_tick_succeeded", False)
                or worker_task is None
                or worker_task.done()
            ):
                pending.append("telegram_signal_worker")
            if pending:
                return JSONResponse(
                    status_code=503,
                    content={"status": "not_ready", "pending": sorted(pending)},
                )
            return {"status": "ready"}
        if market_signal_role:
            market_pending: list[str] = []
            probe = getattr(app.state, "database_probe", None)
            if probe is None or not await probe():
                market_pending.append("database")
            worker = getattr(app.state, "market_signal_worker", None)
            worker_task = getattr(app.state, "market_signal_worker_task", None)
            if (
                worker is None
                or not getattr(worker, "last_tick_succeeded", False)
                or worker_task is None
                or worker_task.done()
            ):
                market_pending.append("market_signal_worker")
            if market_pending:
                return JSONResponse(
                    status_code=503,
                    content={"status": "not_ready", "pending": sorted(market_pending)},
                )
            return {"status": "ready"}
        if trainer_role:
            pending = []
            probe = getattr(app.state, "database_probe", None)
            if probe is None or not await probe():
                pending.append("database")
            supervisor = getattr(app.state, "learning_training_supervisor", None)
            supervisor_task = getattr(app.state, "learning_training_supervisor_task", None)
            if supervisor is None or supervisor_task is None or supervisor_task.done():
                pending.append("learning_training_worker")
            if pending:
                return JSONResponse(
                    status_code=503,
                    content={"status": "not_ready", "pending": sorted(pending)},
                )
            return {"status": "ready"}
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


def _build_telegram_sender(settings: AppSettings, *, required: bool) -> Any:
    """Chỉ dùng sender thật khi token tồn tại; test/local giữ adapter recording."""
    path = settings.telegram_bot_token_file
    if path is None:
        if required:
            raise RuntimeError("signal-worker cần telegram_bot_token_file.")
        return RecordingTelegramSender()
    try:
        token = load_telegram_bot_token(path)
    except OSError, ValueError:
        if required or settings.app_env == "production":
            raise RuntimeError("Telegram Bot API token chưa sẵn sàng.") from None
        return RecordingTelegramSender()
    return TelegramBotApiSender(token)


def _configured_scorer_policy_provider(
    settings: object,
) -> ConfiguredScorerRuntimePolicyProvider | None:
    """Tạo provider từ AppSettings; thiếu policy thì giữ runtime fail-closed."""
    if not callable(getattr(settings, "scorer_test_evaluation_policy", None)):
        return None
    return ConfiguredScorerRuntimePolicyProvider.from_settings(settings)


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
        scorer_policy_provider=_configured_scorer_policy_provider(app_settings),
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
    *,
    context_provider: Any,
    sessions: Any,
    workspace_id: Any,
    application: Any,
    scorer_policy_provider: ScorerRuntimePolicyProvider | None = None,
) -> Callable[..., Any] | None:
    """Nối worker job khi context provider có thể dựng snapshot decision đã lưu."""
    if sessions is None or not callable(getattr(context_provider, "snapshot_for", None)):
        return None
    decision_snapshot_provider = context_provider
    from trading_bot.adapters.database.repositories.bot_decisions import (
        PostgresSignalDecisionStore,
    )
    from trading_bot.adapters.database.repositories.learning_scorer_runtime_committer import (
        PostgresScorerRuntimeCommitter,
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
    store = PostgresSignalDecisionStore(
        sessions,
        score_candidate=PostgresScorerRuntimeCommitter(scorer_policy_provider),
    )
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
