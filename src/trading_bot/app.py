import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

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
from trading_bot.adapters.secrets.telegram_token import (
    DEFAULT_TELEGRAM_BOT_TOKEN_REFERENCE,
    CompositeSecretProvider,
    FileTelegramBotSecretProvider,
)
from trading_bot.adapters.telegram.bot_api_sender import TelegramBotApiSender
from trading_bot.adapters.telegram.outbound_queue import ThrottledTelegramSender
from trading_bot.adapters.telegram.recording_sender import RecordingTelegramSender
from trading_bot.admin.audit_router import router as audit_router
from trading_bot.admin.backtest_replay_router import router as backtest_replay_router
from trading_bot.admin.bot_control_router import router as bot_control_router
from trading_bot.admin.bot_grant_service import BotGrantAdminService
from trading_bot.admin.bot_grants_router import router as bot_grants_router
from trading_bot.admin.bot_reports_router import router as bot_reports_router
from trading_bot.admin.bot_service import BotConfigAdminService
from trading_bot.admin.bots_router import router as bots_router
from trading_bot.admin.incidents_router import router as incidents_router
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
from trading_bot.admin.learning_paper_entry_block_router import (
    router as learning_paper_entry_block_router,
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
from trading_bot.admin.live_monitor_router import router as live_monitor_router
from trading_bot.admin.members_router import router as members_router
from trading_bot.admin.research_source_router import admin_router as research_source_admin_router
from trading_bot.admin.research_source_router import router as research_source_router
from trading_bot.admin.risk_appetite_page_router import router as risk_appetite_page_router
from trading_bot.admin.risk_appetite_service import RiskAppetiteService
from trading_bot.admin.session_redirect import login_redirect_for
from trading_bot.admin.settings_router import router as settings_router
from trading_bot.admin.settings_service import SettingsAdminService
from trading_bot.admin.strategy_card_service import StrategyCardAdminService
from trading_bot.admin.strategy_release_service import StrategyReleaseAdminService
from trading_bot.admin.telegram_binding_router import router as telegram_binding_router
from trading_bot.admin.telegram_signal_router import router as telegram_signal_router
from trading_bot.admin.trade_journal_router import (
    html_router as trade_journal_html_router,
)
from trading_bot.admin.trade_journal_router import (
    router as trade_journal_router,
)
from trading_bot.admin.watchlist_router import router as watchlist_router
from trading_bot.admin.web_router import router as admin_web_router
from trading_bot.application.account_activity_guard import AccountActivityGuard
from trading_bot.application.analyst_review import AnalystReviewService
from trading_bot.application.backtest_replay_jobs import BacktestReplayJobService
from trading_bot.application.bot_operations import BotOperationsService
from trading_bot.application.bot_reporting import BotReportingService
from trading_bot.application.bot_runtime_registry import BotRuntimeRegistry
from trading_bot.application.bot_runtime_supervisor import BotRuntimeSupervisor
from trading_bot.application.economic_calendar import EconomicCalendarService
from trading_bot.application.flatten_all import FlattenAllService
from trading_bot.application.learning_incident_entry_hold import (
    LearningIncidentEntryHoldService,
)
from trading_bot.application.learning_model_registry import LearningModelRegistryService
from trading_bot.application.learning_observations_admin import (
    LearningObservationsAdminService,
)
from trading_bot.application.learning_paper_entry_block import LearningPaperEntryBlockService
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
from trading_bot.application.live_monitor import LiveMonitorService
from trading_bot.application.market_scanner import MarketScannerService
from trading_bot.application.market_signal_composition import build_market_signal_scanner
from trading_bot.application.operational_alert_outbox import OperationalAlertOutboxConsumer
from trading_bot.application.operational_telemetry import OperationalHealthService
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
from trading_bot.application.signal_outcome_review import SignalOutcomeReviewService
from trading_bot.application.source_policy_admin import (
    SourcePolicyAdminRepository,
    SourcePolicyAdminService,
)
from trading_bot.application.strategy_card_bars import PostgresCardBarsProvider
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.application.telegram_binding import TelegramBindingService
from trading_bot.application.telegram_paper_report import TelegramPaperReportService
from trading_bot.application.telegram_signal_subscription import TelegramSignalSubscriptionService
from trading_bot.application.telegram_signals import SignalThrottlePolicy
from trading_bot.application.trade_journal import TradeJournalService
from trading_bot.auth.account_security_router import router as account_security_router
from trading_bot.auth.action_challenge import ActionChallengeService
from trading_bot.auth.cookies import session_cookie_name
from trading_bot.auth.mfa_recovery import AccountSecurityService
from trading_bot.auth.route_policy import assert_all_routes_have_policy, public_route
from trading_bot.auth.router import router as auth_router
from trading_bot.auth.security import LoginRateLimiter, TestModeTotpManager
from trading_bot.auth.service import AuthService
from trading_bot.common.http_limits import (
    BodySizeLimitMiddleware,
    ClientIPMiddleware,
    RequestRateLimitMiddleware,
)
from trading_bot.common.http_security import SecurityHeadersMiddleware, security_headers
from trading_bot.config.settings import AppSettings
from trading_bot.observability.readiness import (
    WorkerReadinessReport,
    WorkerReadinessState,
    evaluate_worker_readiness,
)
from trading_bot.ports.secrets import (
    SecretAccessError,
    SecretProvider,
    TelegramBotSecretProvider,
)
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
from trading_bot.workers.signal_outcome_worker import SignalOutcomeWorker
from trading_bot.workers.telegram_signal_worker import TelegramSignalDeliveryWorker
from trading_bot.workers.telegram_trade_result_worker import (
    ChatPacer,
    TelegramTradeResultWorker,
)

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


def build_scorer_history_database_engine(settings: AppSettings) -> AsyncEngine | None:
    """Engine riêng chỉ đọc lịch sử scorer bằng role ``crypidian_scorer_history`` (CRY-439)."""
    if not (
        settings.scorer_history_database_user and settings.scorer_history_database_password_file
    ):
        return None
    database_url = database_url_from_secret(
        username=settings.scorer_history_database_user,
        password_file=settings.scorer_history_database_password_file,
        host=settings.database_host,
        database=settings.database_name,
    )
    return create_database_engine(database_url, session_timeouts=admin_web_session_timeouts())


def create_app(
    settings: AppSettings | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    scorer_history_session_factory: async_sessionmaker[AsyncSession] | None = None,
    secret_provider: SecretProvider | None = None,
    paper_runtime_bootstrap_factory: Callable[..., Any] | None = None,
    paper_decision_context_provider_factory: Callable[..., Any] | None = None,
    paper_runtime_supervisor_factory: Callable[..., Any] | None = None,
    rights_worker_factory: Callable[..., Any] | None = None,
    live_market_ingestion_factory: Callable[..., Any] | None = None,
    signal_worker_factory: Callable[..., Any] | None = None,
    market_signal_worker_factory: Callable[..., Any] | None = None,
    signal_outcome_worker_factory: Callable[..., Any] | None = None,
    training_supervisor_factory: Callable[..., Any] | None = None,
    database_probe: Callable[[], Awaitable[bool]] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Tạo ứng dụng mà không kết nối mạng hay tải credential khi import."""
    app_settings = settings or AppSettings()
    admin_web_role = app_settings.process_role == "admin-web"
    scorer_policy_provider = _configured_scorer_policy_provider(app_settings)

    initial_limiter_sessions = (
        session_factory
        if admin_web_role
        and isinstance(session_factory, async_sessionmaker)
        and session_factory.kw.get("bind") is not None
        else None
    )
    default_login_limiter = LoginRateLimiter(session_factory=initial_limiter_sessions)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        engine: AsyncEngine | None = None
        scorer_history_engine: AsyncEngine | None = None
        active_session_factory = session_factory
        active_scorer_history_sessions = scorer_history_session_factory
        active_secret_provider = secret_provider
        if active_session_factory is None:
            engine = build_database_engine(app_settings)
            if engine is not None:
                active_session_factory = create_session_factory(engine)
        if active_scorer_history_sessions is None and admin_web_role:
            scorer_history_engine = build_scorer_history_database_engine(app_settings)
            if scorer_history_engine is not None:
                active_scorer_history_sessions = create_session_factory(scorer_history_engine)
        application.state.session_factory = active_session_factory
        if (
            admin_web_role
            and isinstance(active_session_factory, async_sessionmaker)
            and active_session_factory.kw.get("bind") is not None
            and application.state.login_limiter is default_login_limiter
            and default_login_limiter.session_factory is None
        ):
            default_login_limiter.bind_session_factory(active_session_factory)
        application.state.database_probe = database_probe
        application.state.worker_heartbeat_reader = None
        if admin_web_role and active_session_factory is not None:
            from trading_bot.application.ingestion_worker_heartbeat import (
                WorkerHeartbeatReadService,
            )

            application.state.worker_heartbeat_reader = WorkerHeartbeatReadService(
                active_session_factory
            )
        application.state.scorer_runtime_policy_provider = scorer_policy_provider
        if database_probe is None and isinstance(active_session_factory, async_sessionmaker):
            probe_sessions = active_session_factory

            async def _default_probe() -> bool:
                return await probe_database(probe_sessions)

            application.state.database_probe = _default_probe
        application.state.scorer_runtime_policy_provider = scorer_policy_provider
        if active_secret_provider is None:
            mfa_provider = (
                LocalMfaSecretProvider(
                    key_file=app_settings.mfa_encryption_key_file,
                    storage_directory=app_settings.mfa_secret_directory,
                )
                if app_settings.mfa_encryption_key_file and app_settings.mfa_secret_directory
                else None
            )
            telegram_token_provider = (
                FileTelegramBotSecretProvider(token_file=app_settings.telegram_bot_token_file)
                if app_settings.telegram_bot_token_file is not None
                else None
            )
            if mfa_provider is not None and telegram_token_provider is not None:
                active_secret_provider = CompositeSecretProvider(
                    mfa_provider=mfa_provider,
                    telegram_provider=telegram_token_provider,
                )
            elif mfa_provider is not None:
                active_secret_provider = mfa_provider
            elif telegram_token_provider is not None:
                active_secret_provider = telegram_token_provider
        if app_settings.auth_test_totp_enabled:
            logger.warning(
                "AUTH_TEST_TOTP_ENABLED: chấp nhận mã xác thực cố định; chỉ cho kiểm thử local."
            )
        totp_manager = TestModeTotpManager() if app_settings.auth_test_totp_enabled else None
        application.state.auth_service = (
            AuthService(
                active_session_factory,
                active_secret_provider,
                totp=totp_manager,
                idle_timeout=timedelta(minutes=app_settings.session_idle_timeout_minutes),
            )
            if active_session_factory is not None and active_secret_provider is not None
            else None
        )
        application.state.account_security_service = (
            AccountSecurityService(
                active_session_factory,
                active_secret_provider,
                totp=totp_manager,
            )
            if active_session_factory is not None and active_secret_provider is not None
            else None
        )
        action_challenge_service = (
            ActionChallengeService(
                active_session_factory,
                active_secret_provider,
                totp=totp_manager,
            )
            if active_session_factory is not None and active_secret_provider is not None
            else None
        )
        require_action_step_up = app_settings.is_action_step_up_required or (
            app_settings.app_env == "test"
            and app_settings.action_step_up_required is None
            and action_challenge_service is not None
        )
        application.state.action_challenge_service = action_challenge_service
        application.state.settings_admin_service = (
            SettingsAdminService(
                active_session_factory,
                action_challenge_service=action_challenge_service,
                require_action_step_up=require_action_step_up,
            )
            if active_session_factory is not None
            else None
        )
        # CRY-428: trang "Khẩu vị rủi ro" dùng chung SettingsAdminService để publish có audit.
        application.state.risk_appetite_service = (
            RiskAppetiteService(
                active_session_factory,
                application.state.settings_admin_service,
                action_challenge_service=action_challenge_service,
                require_action_step_up=require_action_step_up,
            )
            if active_session_factory is not None
            else None
        )
        application.state.bot_admin_service = (
            BotConfigAdminService(
                active_session_factory,
                action_challenge_service=action_challenge_service,
                require_action_step_up=require_action_step_up,
            )
            if active_session_factory is not None
            else None
        )
        application.state.bot_grant_service = (
            BotGrantAdminService(active_session_factory)
            if admin_web_role and active_session_factory is not None
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
        application.state.trade_journal_service = (
            TradeJournalService(active_session_factory)
            if active_session_factory is not None
            else None
        )
        application.state.live_monitor_service = (
            LiveMonitorService(active_session_factory)
            if active_session_factory is not None
            else None
        )
        application.state.market_scanner_service = (
            MarketScannerService(active_session_factory)
            if active_session_factory is not None
            else None
        )
        application.state.economic_calendar_service = (
            EconomicCalendarService(active_session_factory)
            if active_session_factory is not None
            else None
        )
        application.state.analyst_review_service = (
            AnalystReviewService(active_session_factory)
            if active_session_factory is not None
            else None
        )
        application.state.signal_outcome_review_service = (
            SignalOutcomeReviewService(active_session_factory)
            if active_session_factory is not None
            else None
        )
        application.state.backtest_replay_job_service = (
            BacktestReplayJobService(active_session_factory)
            if active_session_factory is not None
            else None
        )
        application.state.bot_operations_service = (
            BotOperationsService(
                active_session_factory,
                action_challenge_service=action_challenge_service,
                require_action_step_up=require_action_step_up,
            )
            if active_session_factory is not None
            else None
        )
        application.state.operational_health_service = (
            OperationalHealthService(active_session_factory)
            if active_session_factory is not None
            else None
        )
        application.state.strategy_card_service = (
            StrategyCardAdminService(
                active_session_factory,
                bars_provider=PostgresCardBarsProvider(active_session_factory),
            )
            if active_session_factory is not None
            else StrategyCardAdminService()
        )
        application.state.strategy_release_service = (
            StrategyReleaseAdminService(
                active_session_factory,
                action_challenge_service=action_challenge_service,
                require_action_step_up=require_action_step_up,
            )
            if active_session_factory is not None
            else StrategyReleaseAdminService()
        )
        application.state.flatten_all_service = (
            FlattenAllService(
                active_session_factory,
                snapshot_provider=PostgresFlattenSnapshotProvider(),
                operations_factory=application.state.bot_operations_service.for_principal,
                action_challenge_service=action_challenge_service,
                require_action_step_up=require_action_step_up,
            )
            if active_session_factory is not None
            else None
        )
        application.state.telegram_binding_service = (
            TelegramBindingService(
                DatabaseTelegramStore(active_session_factory),
                bot_username=app_settings.telegram_bot_username,
                # TG-4: sender dựng sau trong lifespan; chỉ sender có create_forum_topic dùng được.
                topic_creator=lambda: _topic_creator(
                    getattr(application.state, "telegram_sender", None)
                ),
                action_challenge_service=action_challenge_service,
                require_action_step_up=require_action_step_up,
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
                    active_modes_enabled=app_settings.scorer_active_modes_enabled,
                )
                if active_session_factory is not None
                else None
            )
            application.state.learning_scoring_report_read_model = (
                ScorerReportReadModel(active_session_factory)
                if active_session_factory is not None
                else None
            )
            # Fail-closed (CRY-439): không dùng fallback sang active_session_factory của admin_web.
            application.state.learning_scoring_score_event_read_model = (
                ScorerScoreEventReadModel(active_scorer_history_sessions)
                if active_scorer_history_sessions is not None
                else None
            )
            application.state.learning_paper_entry_block_service = (
                LearningPaperEntryBlockService(active_session_factory)
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
            secret_provider=active_secret_provider,
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
        live_event_task: asyncio.Task[None] | None = None
        live_quote_task: asyncio.Task[None] | None = None
        ingestion_heartbeat_task: asyncio.Task[None] | None = None
        market_collector_task: asyncio.Task[None] | None = None
        macro_calendar_task: asyncio.Task[None] | None = None
        derivatives_task: asyncio.Task[None] | None = None
        listings_task: asyncio.Task[None] | None = None
        signal_worker_task: asyncio.Task[None] | None = None
        trade_result_worker_task: asyncio.Task[None] | None = None
        operational_alert_worker_task: asyncio.Task[None] | None = None
        market_signal_worker_task: asyncio.Task[None] | None = None
        signal_outcome_task: asyncio.Task[None] | None = None
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
                clock=clock,
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
                    events=app_settings.ingestion_live_event_kinds,
                    mark_price_interval_seconds=(
                        app_settings.ingestion_live_mark_price_interval_seconds
                    ),
                    quote_interval_seconds=app_settings.ingestion_live_quote_interval_seconds,
                )
                application.state.live_market_worker = live_market_runtime.worker
                live_market_task = asyncio.create_task(
                    live_market_runtime.worker.run(stopping), name="live-market-ingestion-worker"
                )
                application.state.live_market_task = live_market_task
                live_event_worker = getattr(live_market_runtime, "event_worker", None)
                if live_event_worker is not None:
                    application.state.live_event_worker = live_event_worker
                    live_event_task = asyncio.create_task(
                        live_event_worker.run(stopping), name="live-market-event-worker"
                    )
                    application.state.live_event_task = live_event_task
                live_quote_worker = getattr(live_market_runtime, "quote_worker", None)
                if live_quote_worker is not None:
                    # CK-1: giá mua/bán tốt nhất (route /public), ghi đè bảng giá trực tiếp.
                    application.state.live_quote_worker = live_quote_worker
                    live_quote_task = asyncio.create_task(
                        live_quote_worker.run(stopping), name="live-market-quote-worker"
                    )
                    application.state.live_quote_task = live_quote_task
            if app_settings.market_collector_enabled:
                market_collector_task = _start_market_collector(
                    application,
                    app_settings=app_settings,
                    session_factory=active_session_factory,
                    workspace_id=workspace_id,
                    stopping=stopping,
                )
            if app_settings.macro_calendar_enabled:
                macro_calendar_task = _start_macro_calendar_collector(
                    application,
                    session_factory=active_session_factory,
                    workspace_id=workspace_id,
                    stopping=stopping,
                )
            if app_settings.derivatives_collector_enabled:
                derivatives_task = _start_derivatives_collector(
                    application,
                    app_settings=app_settings,
                    session_factory=active_session_factory,
                    workspace_id=workspace_id,
                    stopping=stopping,
                )
            if app_settings.listings_collector_enabled:
                listings_task = _start_listings_collector(
                    application,
                    session_factory=active_session_factory,
                    workspace_id=workspace_id,
                    stopping=stopping,
                )
            if live_market_runtime is not None:
                from trading_bot.application.ingestion_worker_heartbeat import (
                    IngestionWorkerHeartbeatPublisher,
                    WorkerHeartbeatSource,
                )

                sources = [
                    WorkerHeartbeatSource(
                        component="live_market_ingestion",
                        worker=getattr(live_market_runtime, "worker", None),
                        task=live_market_task,
                    )
                ]
                event_worker = getattr(live_market_runtime, "event_worker", None)
                quote_worker = getattr(live_market_runtime, "quote_worker", None)
                if event_worker is not None:
                    sources.append(
                        WorkerHeartbeatSource(
                            component="live_market_events",
                            worker=event_worker,
                            task=live_event_task,
                        )
                    )
                if quote_worker is not None:
                    sources.append(
                        WorkerHeartbeatSource(
                            component="live_market_quotes",
                            worker=quote_worker,
                            task=live_quote_task,
                        )
                    )
                publisher = IngestionWorkerHeartbeatPublisher(
                    sessions=active_session_factory,
                    workspace_id=workspace_id,
                    sources=sources,
                )
                application.state.ingestion_worker_heartbeat_publisher = publisher
                ingestion_heartbeat_task = asyncio.create_task(
                    publisher.run(stopping), name="ingestion-worker-heartbeat"
                )
                application.state.ingestion_worker_heartbeat_task = ingestion_heartbeat_task
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
            # TG-3: một pacer cho cả tin tín hiệu và kết quả PAPER để mỗi chat cách ≥ 1,1 giây.
            chat_pacer = ChatPacer()
            pacer_kwargs: dict[str, Any] = (
                {"pacer": chat_pacer} if signal_worker_factory is None else {}
            )
            # TG-3: ảnh thẻ (PNG + caption) cho tín hiệu, kết quả và tổng kết; dự phòng văn bản.
            card_senders: dict[str, dict[str, Any]] = {"signal": {}, "trade": {}}
            if app_settings.telegram_card_images_enabled:
                from trading_bot.adapters.telegram.card_delivery import (
                    send_paper_report_card,
                    send_signal_card,
                    send_trade_result_card,
                )

                if signal_worker_factory is None:
                    card_senders["signal"] = {"signal_send": send_signal_card}
                card_senders["trade"] = {
                    "trade_result_send": send_trade_result_card,
                    "paper_report_send": send_paper_report_card,
                }
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
                throttle=SignalThrottlePolicy(
                    cooldown=timedelta(minutes=app_settings.signal_worker_cooldown_minutes),
                    max_per_hour=app_settings.signal_worker_max_messages_per_hour,
                ),
                **pacer_kwargs,
                **card_senders["signal"],
            )
            application.state.telegram_signal_worker = signal_worker
            signal_worker_task = asyncio.create_task(
                signal_worker.run(stopping), name="telegram-signal-delivery-worker"
            )
            application.state.telegram_signal_worker_task = signal_worker_task
            report_minutes = app_settings.telegram_report_interval_minutes
            if app_settings.telegram_trade_results_enabled or report_minutes > 0:
                trade_result_worker = TelegramTradeResultWorker(
                    session_factory=active_session_factory,
                    workspace_id=workspace_id,
                    sender=telegram_sender,
                    worker_id=f"telegram-trade-result-{uuid4().hex}",
                    tick_seconds=app_settings.signal_worker_tick_seconds,
                    trade_results_enabled=app_settings.telegram_trade_results_enabled,
                    max_event_age=timedelta(
                        minutes=app_settings.telegram_trade_result_max_age_minutes
                    ),
                    report_interval=(
                        timedelta(minutes=report_minutes) if report_minutes > 0 else None
                    ),
                    max_attempts=app_settings.signal_worker_max_attempts,
                    pacer=chat_pacer,
                    **card_senders["trade"],
                )
                application.state.telegram_trade_result_worker = trade_result_worker
                trade_result_worker_task = asyncio.create_task(
                    trade_result_worker.run(stopping), name="telegram-trade-result-worker"
                )
                application.state.telegram_trade_result_worker_task = trade_result_worker_task
            if app_settings.operational_alerts_enabled:
                # CRY-274: consumer outbox cảnh báo vận hành — cùng sender/pacer nhịp với
                # tin tín hiệu; idempotent qua ConsumerReceipt, dead-letter vẫn thấy trên Admin.
                alert_consumer = OperationalAlertOutboxConsumer(
                    active_session_factory,
                    sender=telegram_sender,
                    workspace_id=workspace_id,
                    worker_id=f"telegram-operational-alert-{uuid4().hex}",
                    admin_base_url=app_settings.admin_public_base_url,
                    max_attempts=app_settings.operational_alert_max_attempts,
                    tick_seconds=app_settings.signal_worker_tick_seconds,
                )
                application.state.operational_alert_worker = alert_consumer
                operational_alert_worker_task = asyncio.create_task(
                    alert_consumer.run(stopping), name="telegram-operational-alert-worker"
                )
                application.state.operational_alert_worker_task = operational_alert_worker_task
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
                        now=clock,
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
            if app_settings.signal_outcome_tick_seconds > 0:
                outcome_factory = signal_outcome_worker_factory
                if outcome_factory is None:

                    def default_signal_outcome_worker_factory(**kwargs: Any) -> Any:
                        from trading_bot.application.signal_outcomes import SignalOutcomeScorer

                        return SignalOutcomeWorker(
                            SignalOutcomeScorer(kwargs["session_factory"]),
                            workspace_id=kwargs["workspace_id"],
                            tick_seconds=kwargs["tick_seconds"],
                        )

                    outcome_factory = default_signal_outcome_worker_factory
                signal_outcome_worker = outcome_factory(
                    session_factory=active_session_factory,
                    workspace_id=workspace_id,
                    tick_seconds=app_settings.signal_outcome_tick_seconds,
                )
                application.state.signal_outcome_worker = signal_outcome_worker
                signal_outcome_task = asyncio.create_task(
                    signal_outcome_worker.run(stopping), name="signal-outcome-worker"
                )
                application.state.signal_outcome_task = signal_outcome_task
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
            if live_event_task is not None:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(live_event_task, timeout=10)
            if live_quote_task is not None:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(live_quote_task, timeout=10)
            if ingestion_heartbeat_task is not None:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(ingestion_heartbeat_task, timeout=10)
            if live_market_runtime is not None:
                await live_market_runtime.aclose()
            if market_collector_task is not None:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(market_collector_task, timeout=10)
                funding_task = getattr(application.state, "funding_collector_task", None)
                if funding_task is not None:
                    with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                        await asyncio.wait_for(funding_task, timeout=10)
                adapter = getattr(application.state, "market_collector_adapter", None)
                if adapter is not None:
                    await adapter.aclose()
            if macro_calendar_task is not None:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(macro_calendar_task, timeout=10)
                fetcher = getattr(application.state, "macro_calendar_fetcher", None)
                if fetcher is not None:
                    await fetcher.aclose()
            if derivatives_task is not None:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(derivatives_task, timeout=10)
                derivatives_fetcher = getattr(application.state, "derivatives_fetcher", None)
                if derivatives_fetcher is not None:
                    await derivatives_fetcher.aclose()
            if listings_task is not None:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(listings_task, timeout=10)
                listings_fetcher = getattr(application.state, "listings_fetcher", None)
                if listings_fetcher is not None:
                    await listings_fetcher.aclose()
            if signal_worker_task is not None:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(signal_worker_task, timeout=10)
            if trade_result_worker_task is not None:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(trade_result_worker_task, timeout=10)
            if operational_alert_worker_task is not None:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(operational_alert_worker_task, timeout=10)
            if market_signal_worker_task is not None:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(market_signal_worker_task, timeout=10)
            if signal_outcome_task is not None:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(signal_outcome_task, timeout=10)
            if training_supervisor_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await training_supervisor_task
            close_sender = getattr(telegram_sender, "aclose", None)
            if callable(close_sender):
                await close_sender()
            if scorer_history_engine is not None:
                await scorer_history_engine.dispose()
            if engine is not None:
                await engine.dispose()

    app = FastAPI(
        title="Crypidian Trading Bot",
        # SEC-26: production không công khai schema; worker không phục vụ API quản trị.
        openapi_url=None
        if app_settings.app_env == "production" or not admin_web_role
        else "/openapi.json",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    trusted_proxies = app_settings.trusted_proxy_networks
    allowed_hosts = app_settings.allowed_host_list
    # Thứ tự bọc từ trong ra ngoài (lớp thêm sau chạy trước):
    # SecurityHeaders -> ClientIP -> TrustedHost -> BodySizeLimit -> RequestRateLimit -> router.
    app.add_middleware(
        RequestRateLimitMiddleware,
        mutation_per_window=app_settings.rate_limit_mutation_per_minute,
        heavy_read_per_window=app_settings.rate_limit_heavy_read_per_minute,
        heavy_read_paths=app_settings.heavy_read_path_prefixes,
        session_cookie_names=(session_cookie_name(app_settings),),
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=app_settings.request_body_max_bytes)
    if allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts, www_redirect=False)
    app.add_middleware(ClientIPMiddleware, trusted_proxies=trusted_proxies)
    app.add_middleware(SecurityHeadersMiddleware, headers=security_headers(app_settings))
    app.state.settings = app_settings
    app.state.trusted_proxies = trusted_proxies
    app.state.templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    if admin_web_role:
        app.mount(
            "/static",
            StaticFiles(directory=str(Path(__file__).parent / "static")),
            name="static",
        )
    app.state.login_limiter = default_login_limiter
    app.state.readiness = {
        "database": False,
        "market_data": False,
        "exchange": False,
    }
    if app_settings.process_role == "trading-runtime":
        app.state.readiness["paper_runtime"] = False
        app.state.paper_application = None
    # SEC-26: router admin/UI/API quản trị chỉ mount ở process admin-web.
    if admin_web_role:
        app.include_router(auth_router)
        app.include_router(account_security_router)
        app.include_router(settings_router)
        app.include_router(members_router)
        app.include_router(watchlist_router)
        app.include_router(incidents_router)
        app.include_router(audit_router)
        app.include_router(bots_router)
        app.include_router(bot_grants_router)
        app.include_router(bot_reports_router)
        app.include_router(trade_journal_router)
        app.include_router(trade_journal_html_router)
        app.include_router(backtest_replay_router)
        app.include_router(bot_control_router)
        app.include_router(telegram_binding_router)
        app.include_router(telegram_signal_router)
        app.include_router(research_source_router)
        app.include_router(research_source_admin_router)
        app.include_router(learning_model_registry_router)
        app.include_router(learning_scoring_deployment_router)
        app.include_router(learning_model_registry_page_router)
        app.include_router(learning_observations_page_router)
        app.include_router(learning_paper_performance_router)
        app.include_router(learning_incident_entry_hold_router)
        app.include_router(learning_paper_entry_block_router)
        app.include_router(learning_scoring_page_router)
        app.include_router(learning_training_router)
        app.include_router(learning_training_page_router)
        app.include_router(learning_split_router)
        app.include_router(learning_split_page_router)
        app.include_router(risk_appetite_page_router)
        app.include_router(live_monitor_router)
        app.include_router(admin_web_router)
        app.include_router(telegram_router)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_response(request: Request, error: StarletteHTTPException) -> Response:
        """Phiên hết hạn khi mở trang HTML Admin thì về login; còn lại giữ hành vi mặc định.

        Cần ở mức app vì một số router trang (`/admin/learning/*`, `/admin/research-sources`)
        không dùng `AdminHTMLRoute`; API JSON vẫn qua handler mặc định của FastAPI.
        """
        redirect = login_redirect_for(request, error.status_code)
        if redirect is not None:
            return redirect
        return await http_exception_handler(request, error)

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

    @app.get(
        "/health/live",
        include_in_schema=False,
        dependencies=[
            public_route(reason="Probe liveness cho Docker/k8s; chỉ trả status, mode và role.")
        ],
    )
    def live_health() -> dict[str, str]:
        """Báo process còn hoạt động; không khẳng định các dependency đã sẵn sàng."""
        return {
            "status": "ok",
            "mode": app_settings.trading_mode,
            "role": app_settings.process_role,
        }

    @app.get(
        "/health/ready",
        include_in_schema=False,
        response_model=None,
        dependencies=[
            public_route(reason="Probe readiness cho Docker/k8s; chỉ trả bộ đếm trạng thái.")
        ],
    )
    async def ready_health() -> JSONResponse | dict[str, object]:
        """Readiness theo vai trò; runtime: 200 `ready`/`degraded`, 503 khi không phục vụ được.

        Ruling 2/26: một bot lỗi/pause/standby chỉ làm `degraded`; 503 chỉ khi DB không truy cập
        được, bootstrap/wiring thất bại hoặc điều kiện workspace lỗi. Body chỉ có bộ đếm.
        """
        runtime_role = app_settings.process_role == "trading-runtime"
        ingestion_role = app_settings.process_role == "ingestion-worker"
        signal_role = app_settings.process_role == "signal-worker"
        market_signal_role = app_settings.process_role == "market-signal-worker"
        backtest_role = app_settings.process_role == "backtest-worker"
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
            live_kinds = app_settings.ingestion_live_event_kinds
            if "book_ticker" in live_kinds:
                quote_worker = getattr(app.state, "live_quote_worker", None)
                quote_task = getattr(app.state, "live_quote_task", None)
                if (
                    quote_worker is None
                    or not getattr(quote_worker, "is_healthy", False)
                    or quote_task is None
                    or quote_task.done()
                ):
                    pending.append("live_market_quotes")
            if any(kind != "book_ticker" for kind in live_kinds):
                event_worker = getattr(app.state, "live_event_worker", None)
                event_task = getattr(app.state, "live_event_task", None)
                if (
                    event_worker is None
                    or not getattr(event_worker, "is_healthy", False)
                    or event_task is None
                    or event_task.done()
                ):
                    pending.append("live_market_events")
            collector_status = _market_collector_status(app)
            if collector_status is not None and collector_status["state"] == "stopped":
                pending.append("market_collector")
            if pending:
                return JSONResponse(
                    status_code=503,
                    content={"status": "not_ready", "pending": sorted(pending)},
                )
            if collector_status is not None:
                # Thiếu quyền nguồn hoặc lỗi luồng chỉ là degraded: rights worker vẫn phục vụ.
                return {"status": "ready", "market_collector": collector_status}
            return {"status": "ready"}
        if signal_role:
            pending = []
            reasons: dict[str, str] = {}
            degraded_reasons: dict[str, str] = {}
            probe = getattr(app.state, "database_probe", None)
            if probe is None or not await probe():
                pending.append("database")
                reasons["database"] = "DATABASE_UNAVAILABLE"
            missing_token_reason = (
                "TELEGRAM_TOKEN_MISSING"
                if (
                    app_settings.telegram_bot_token_file is not None
                    and not app_settings.telegram_bot_token_file.exists()
                )
                else None
            )
            worker = getattr(app.state, "telegram_signal_worker", None)
            worker_task = getattr(app.state, "telegram_signal_worker_task", None)
            signal_report = _worker_readiness_report(
                component="telegram_signal_worker",
                worker=worker,
                task=worker_task,
                missing_secret_reason=missing_token_reason,
            )
            if signal_report.state is WorkerReadinessState.NOT_READY:
                pending.append("telegram_signal_worker")
                reasons["telegram_signal_worker"] = (
                    signal_report.reason_code or "NO_SUCCESSFUL_TICK"
                )
            elif signal_report.state is WorkerReadinessState.DEGRADED and signal_report.reason_code:
                degraded_reasons["telegram_signal_worker"] = signal_report.reason_code
            trade_worker = getattr(app.state, "telegram_trade_result_worker", None)
            if trade_worker is not None:
                trade_task = getattr(app.state, "telegram_trade_result_worker_task", None)
                trade_report = _worker_readiness_report(
                    component="telegram_trade_result_worker",
                    worker=trade_worker,
                    task=trade_task,
                    missing_secret_reason=missing_token_reason,
                )
                if trade_report.state is WorkerReadinessState.NOT_READY:
                    pending.append("telegram_trade_result_worker")
                    reasons["telegram_trade_result_worker"] = (
                        trade_report.reason_code or "NO_SUCCESSFUL_TICK"
                    )
                elif (
                    trade_report.state is WorkerReadinessState.DEGRADED and trade_report.reason_code
                ):
                    degraded_reasons["telegram_trade_result_worker"] = trade_report.reason_code
            if pending:
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "not_ready",
                        "pending": sorted(pending),
                        "reasons": reasons,
                    },
                )
            if degraded_reasons:
                return {"status": "degraded", "reasons": degraded_reasons}
            return {"status": "ready"}
        if market_signal_role:
            market_pending: list[str] = []
            market_reasons: dict[str, str] = {}
            market_degraded: dict[str, str] = {}
            probe = getattr(app.state, "database_probe", None)
            if probe is None or not await probe():
                market_pending.append("database")
                market_reasons["database"] = "DATABASE_UNAVAILABLE"
            worker = getattr(app.state, "market_signal_worker", None)
            worker_task = getattr(app.state, "market_signal_worker_task", None)
            market_report = _worker_readiness_report(
                component="market_signal_worker",
                worker=worker,
                task=worker_task,
            )
            if market_report.state is WorkerReadinessState.NOT_READY:
                market_pending.append("market_signal_worker")
                market_reasons["market_signal_worker"] = (
                    market_report.reason_code or "NO_SUCCESSFUL_TICK"
                )
            elif market_report.state is WorkerReadinessState.DEGRADED and market_report.reason_code:
                market_degraded["market_signal_worker"] = market_report.reason_code
            if market_pending:
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "not_ready",
                        "pending": sorted(market_pending),
                        "reasons": market_reasons,
                    },
                )
            if market_degraded:
                return {"status": "degraded", "reasons": market_degraded}
            return {"status": "ready"}
        if backtest_role:
            backtest_pending: list[str] = []
            backtest_reasons: dict[str, str] = {}
            backtest_degraded: dict[str, str] = {}
            probe = getattr(app.state, "database_probe", None)
            if probe is None or not await probe():
                backtest_pending.append("database")
                backtest_reasons["database"] = "DATABASE_UNAVAILABLE"
            bt_worker = getattr(app.state, "backtest_worker", None)
            bt_task = getattr(app.state, "backtest_worker_task", None)
            bt_report = _worker_readiness_report(
                component="backtest_worker",
                worker=bt_worker,
                task=bt_task,
            )
            if bt_report.state is WorkerReadinessState.NOT_READY:
                backtest_pending.append("backtest_worker")
                backtest_reasons["backtest_worker"] = bt_report.reason_code or "NO_SUCCESSFUL_TICK"
            elif bt_report.state is WorkerReadinessState.DEGRADED and bt_report.reason_code:
                backtest_degraded["backtest_worker"] = bt_report.reason_code
            if backtest_pending:
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "not_ready",
                        "pending": sorted(backtest_pending),
                        "reasons": backtest_reasons,
                    },
                )
            if backtest_degraded:
                return {"status": "degraded", "reasons": backtest_degraded}
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
        dispatch_state, dispatch_detail = _market_dispatch_status(app)
        body: dict[str, Any] = {
            "status": registry.status() if registry is not None else "degraded",
            "workspace_control": (
                registry.workspace_control() if registry is not None else "active"
            ),
            "paper_accounts": summary,
            "execution_capabilities": {
                "paper_recovery": "ready",
                "market_dispatch": dispatch_state,
                "exchange_execution": "disabled",
            },
        }
        if dispatch_detail is not None:
            body["market_dispatch"] = dispatch_detail
        return body

    assert_all_routes_have_policy(app)
    return app


def _topic_creator(sender: Any) -> Any:
    """Sender hỗ trợ ``create_forum_topic`` (Bot API thật hoặc recording); ngược lại None."""
    return sender if callable(getattr(sender, "create_forum_topic", None)) else None


def _build_telegram_sender(
    settings: AppSettings,
    *,
    secret_provider: SecretProvider | TelegramBotSecretProvider | None = None,
    required: bool,
) -> Any:
    """Nạp token Telegram qua SecretProvider (fallback secret file ở local; fail-closed prod)."""
    if settings.process_role not in {"admin-web", "signal-worker"}:
        return RecordingTelegramSender()

    workspace_id = settings.signal_workspace_id or settings.telegram_workspace_id
    reference = settings.telegram_bot_token_reference or DEFAULT_TELEGRAM_BOT_TOKEN_REFERENCE
    telegram_configured = required or any(
        value is not None
        for value in (
            settings.telegram_workspace_id,
            settings.telegram_webhook_secret_file,
            settings.telegram_bot_username,
            settings.telegram_bot_token_reference,
            settings.telegram_bot_token_file,
        )
    )

    tg_provider: TelegramBotSecretProvider | None = None
    if (
        secret_provider is not None
        and not isinstance(secret_provider, LocalMfaSecretProvider)
        and callable(getattr(secret_provider, "resolve_telegram_bot_token", None))
    ):
        tg_provider = secret_provider
    elif settings.telegram_bot_token_file is not None and (
        settings.telegram_bot_token_reference is None or settings.app_env != "production"
    ):
        tg_provider = FileTelegramBotSecretProvider(token_file=settings.telegram_bot_token_file)

    if tg_provider is None:
        if required or (settings.app_env == "production" and telegram_configured):
            raise RuntimeError(
                "Telegram Bot API token yêu cầu SecretProvider hoặc secret file hợp lệ."
            )
        if settings.app_env == "production":
            return None
        return RecordingTelegramSender()

    try:
        api_sender = TelegramBotApiSender.from_secret_provider(
            tg_provider,
            reference=reference,
            workspace_id=workspace_id,
        )
    except SecretAccessError, ValueError:
        if (
            required
            or settings.app_env == "production"
            or settings.telegram_bot_token_reference is not None
        ):
            raise RuntimeError("Telegram Bot API token chưa sẵn sàng qua SecretProvider.") from None
        return RecordingTelegramSender()

    return ThrottledTelegramSender(api_sender, retry_on_error=not required)


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
        handler=BotCommandHandler(
            state.bot_operations_service,
            paper_report=_build_paper_report(settings, state),
        ),
        sender=state.telegram_sender,
    )


def _build_paper_report(settings: AppSettings, state: Any) -> TelegramPaperReportService | None:
    """TG-3: `/report` dùng BotReportingService (kiểm `bot:report:read`) cùng chu kỳ tổng hợp."""
    reporting = getattr(state, "bot_reporting_service", None)
    if reporting is None:
        return None
    minutes = settings.telegram_report_interval_minutes or 240
    return TelegramPaperReportService(
        reporting=reporting,
        operations=state.bot_operations_service,
        interval=timedelta(minutes=minutes),
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
    clock: Callable[[], datetime] | None = None,
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
    state.paper_market_loop = None
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
        "clock": clock,
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
    clock: Callable[[], datetime] | None = None,
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
    market_dispatch = app_settings.runtime_market_dispatch_enabled
    if context_provider_factory is None and market_dispatch:
        # RT-1 lát 2: provider thật chỉ đọc dữ liệu đã persist (không gọi API sàn).
        context_provider_factory = _postgres_context_provider
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
        active_modes_enabled=app_settings.scorer_active_modes_enabled,
    )
    if market_dispatch:
        from trading_bot.application.paper_market_event_loop import PaperMarketEventLoop

        market_loop = PaperMarketEventLoop(
            application=paper_application,
            sessions=session_factory,
            workspace_id=result.workspace_id,
            clock=clock,
        )
        state.paper_market_loop = market_loop
        job_processor = _with_market_loop(job_processor, market_loop)
    supervisor_kwargs: dict[str, Any] = {
        "registry": registry,
        "application": paper_application,
        "bootstrap": bootstrap,
        "sessions": session_factory,
        "workspace_id": result.workspace_id,
        "tick_seconds": app_settings.runtime_supervisor_tick_seconds,
        "lease_ttl": lease_ttl,
        "job_processor": job_processor,
    }
    if clock is not None:
        supervisor_kwargs["clock"] = clock
    supervisor = supervisor_builder(**supervisor_kwargs)
    state.paper_supervisor = supervisor
    # Tick đầu chạy đồng bộ: áp control đã có và xác minh activity trước khi nhận command.
    await supervisor.tick_once()
    return supervisor


def _postgres_context_provider(**kwargs: Any) -> Any:
    from trading_bot.application.paper_market_context import (
        PostgresPaperDecisionContextProvider,
    )

    return PostgresPaperDecisionContextProvider(
        kwargs["sessions"], workspace_id=kwargs["workspace_id"]
    )


def _with_market_loop(
    job_processor: Callable[..., Any] | None, market_loop: Any
) -> Callable[..., Any]:
    """Submit candidate rồi luôn chạy vòng nến; lỗi submit không chặn theo dõi vị thế."""

    async def process(account_id: Any, now: Any) -> Any:
        try:
            return await job_processor(account_id, now) if job_processor is not None else None
        finally:
            await market_loop.run_for_account(account_id)

    return process


def _market_dispatch_status(app: FastAPI) -> tuple[str, dict[str, Any] | None]:
    """Trạng thái RT-1 lát 2 cho readiness; chỉ bộ đếm, không có dữ liệu nhạy cảm."""
    loop = getattr(app.state, "paper_market_loop", None)
    if loop is None:
        return "not_configured", None
    detail: dict[str, Any] = loop.status()
    return str(detail["state"]), detail


def _runtime_job_processor(
    *,
    context_provider: Any,
    sessions: Any,
    workspace_id: Any,
    application: Any,
    scorer_policy_provider: ScorerRuntimePolicyProvider | None = None,
    active_modes_enabled: bool = False,
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
        DEFAULT_MAX_BARS_PER_STREAM,
        BotEvidenceDispatcher,
        SignalEvidenceFanOutConsumer,
    )

    max_bars_per_stream = getattr(
        decision_snapshot_provider,
        "max_bars_per_stream",
        DEFAULT_MAX_BARS_PER_STREAM,
    )
    decision_contexts = EffectiveBotDecisionContextProvider(
        sessions,
        runtime_gate=RuntimeFanOutGate(),
        exclusion_recorder=FanOutExclusionRecorder(
            sessions=sessions,
            workspace_id=workspace_id,
            accounts=application.runtime_account,
        ),
        max_bars_per_stream=max_bars_per_stream,
    )
    store = PostgresSignalDecisionStore(
        sessions,
        score_candidate=PostgresScorerRuntimeCommitter(
            scorer_policy_provider if active_modes_enabled else None
        ),
    )
    dispatcher = BotEvidenceDispatcher(
        store,
        strategies=strategy_registry(),
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
        return await _submit_bot_candidates(application, pending_candidates, record_outcome)

    async def record_outcome(decision: Any, stage: str, reason_code: str) -> None:
        """Lưu (decision, giai đoạn, lý do) vào paper_candidate_outcome; trùng thì bỏ qua."""
        from sqlalchemy.dialects.postgresql import insert

        from trading_bot.adapters.database.models import PaperCandidateOutcomeRecord
        from trading_bot.application.outbox import UnitOfWork

        async with UnitOfWork(sessions, workspace_id=workspace_id, bot_id=decision.bot_id) as uow:
            await uow.session.execute(
                insert(PaperCandidateOutcomeRecord)
                .values(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    bot_id=decision.bot_id,
                    account_id=decision.account_id,
                    decision_id=decision.decision_id,
                    stage=stage,
                    reason_code=reason_code[:120],
                )
                .on_conflict_do_nothing(constraint="uq_paper_candidate_outcome")
            )

    return process_pending


# Khóa (decision, lý do) đã log "không thành lệnh"; dict giữ thứ tự để bỏ khóa cũ nhất.
_LOGGED_CANDIDATE_REJECTIONS: dict[tuple[Any, ...], None] = {}


CandidateRecorder = Callable[[Any, str, str], Awaitable[None]]


def candidate_rejections(result: Any) -> list[tuple[str, str]]:
    """(giai đoạn, mã lý do) khiến candidate không thành lệnh; rỗng nếu đã submit/trùng."""
    if getattr(result, "submission", None) is not None or getattr(result, "duplicate", False):
        return []
    reasons: list[tuple[str, str]] = []
    block = getattr(result, "entry_block_reason", None)
    if block:
        reasons.append(("ENTRY_BLOCK", str(block)))
    cycle = getattr(result, "decision", None)
    risk = getattr(cycle, "risk_decision", None)
    if risk is not None and not getattr(risk, "approved", True):
        reasons.append(("RISK", str(getattr(risk, "reason_code", "RISK_REJECTED"))))
    plan_code = getattr(getattr(cycle, "plan", None), "reason_code", None)
    if plan_code:
        reasons.append(("PLAN", str(plan_code)))
    strategy_code = getattr(getattr(cycle, "strategy_result", None), "code", None)
    outcome = getattr(getattr(cycle, "outcome", None), "value", None)
    if outcome == "NO_TRADE" and strategy_code:
        reasons.append(("STRATEGY", str(strategy_code)))
    if not reasons:
        reasons.append(("ERROR", str(outcome or "NOT_SUBMITTED")))
    return reasons


async def _submit_bot_candidates(
    application: Any, decisions: Any, record: CandidateRecorder | None = None
) -> tuple[Any, ...]:
    """Chuyển candidate đã lưu thành PAPER command riêng; abstain/veto không đặt lệnh.

    Candidate không thành lệnh: log một lần cho mỗi (decision, lý do) và gọi ``record`` để lưu
    bền vào ``paper_candidate_outcome`` (16/11/2026) cho Run Report/AI Analyst phân tích.
    """
    from trading_bot.application.bot_dispatcher import BotDecisionStatus

    results = []
    for decision in decisions:
        if decision.status is not BotDecisionStatus.CANDIDATE:
            continue
        try:
            result = await application.evaluate_and_submit(
                PaperBotDecisionCommand(
                    account_id=decision.account_id,
                    bot_decision_id=decision.decision_id,
                )
            )
        except Exception as error:
            # Một candidate lỗi không được chặn candidate khác của bot; ghi mã để vận hành thấy.
            code = str(getattr(error, "reason_code", type(error).__name__))
            if await _first_rejection(decision, (("ERROR", code),), record):
                logger.warning(
                    "PAPER candidate không submit được; decision_id=%s reason_code=%s",
                    decision.decision_id,
                    code,
                )
            continue
        results.append(result)
        reasons = candidate_rejections(result)
        if reasons and await _first_rejection(decision, tuple(reasons), record):
            logger.warning(
                "PAPER candidate không thành lệnh; decision_id=%s reasons=%s",
                decision.decision_id,
                ",".join(f"{stage}:{code}" for stage, code in reasons),
            )
    return tuple(results)


async def _first_rejection(
    decision: Any, reasons: tuple[tuple[str, str], ...], record: CandidateRecorder | None
) -> bool:
    """True khi (decision, lý do) mới gặp lần đầu trong process: log + lưu bền một lần.

    Candidate chưa submit được đánh giá lại mỗi tick tới khi hết hạn (15/11: 1.380 dòng log/15
    phút) nên phải chặn trùng; DB còn ràng buộc unique để restart không ghi lặp.
    """
    key = (decision.decision_id, reasons)
    if key in _LOGGED_CANDIDATE_REJECTIONS:
        return False
    _LOGGED_CANDIDATE_REJECTIONS[key] = None
    while len(_LOGGED_CANDIDATE_REJECTIONS) > 4096:
        _LOGGED_CANDIDATE_REJECTIONS.pop(next(iter(_LOGGED_CANDIDATE_REJECTIONS)))
    if record is not None:
        for stage, code in reasons:
            try:
                await record(decision, stage, code)
            except Exception as error:  # noqa: BLE001 - lưu lý do không được chặn runtime
                logger.warning("Không lưu được lý do candidate; error=%s", type(error).__name__)
    return True


def _default_supervisor(**kwargs: Any) -> BotRuntimeSupervisor:
    application = kwargs["application"]
    clock = kwargs.get("clock") or (lambda: datetime.now(UTC))
    activity_guard = AccountActivityGuard(
        sessions=kwargs["sessions"],
        workspace_id=kwargs["workspace_id"],
        accounts=application.runtime_account,
        adapter=PaperLedgerAccountActivityAdapter(
            workspace_id=kwargs["workspace_id"],
            gateways=application.gateway_view(),
            clock=clock,
        ),
    )
    return BotRuntimeSupervisor(activity_guard=activity_guard, **kwargs)


app = create_app()


def _start_market_collector(
    application: FastAPI,
    *,
    app_settings: AppSettings,
    session_factory: Any,
    workspace_id: UUID,
    stopping: asyncio.Event,
) -> asyncio.Task[None]:
    """SIG-0: thu nến công khai Binance cho PAPER nội bộ; chỉ dữ liệu thị trường, không API key."""
    from trading_bot.adapters.binance_usdm.market_data import BinanceUsdMMarketDataAdapter
    from trading_bot.application.market_bar_collector import (
        MarketBarCollector,
        parse_collector_streams,
    )
    from trading_bot.workers.market_bar_collector_worker import (
        MarketBarCollectorWorker,
        database_collector_scope,
    )

    adapter = BinanceUsdMMarketDataAdapter()
    application.state.market_collector_adapter = adapter
    collector = MarketBarCollector(
        workspace_id=workspace_id,
        streams=parse_collector_streams(app_settings.market_collector_streams),
        market_data=adapter,
        session_scope=database_collector_scope(session_factory, workspace_id),
        initial_bars=app_settings.market_collector_initial_bars,
    )
    worker = MarketBarCollectorWorker(
        collector=collector, tick_seconds=app_settings.market_collector_tick_seconds
    )
    application.state.market_collector = worker
    task = asyncio.create_task(worker.run(stopping), name="market-bar-collector")
    application.state.market_collector_task = task
    # FX-3 lát 1: funding đã chốt cho cùng danh sách symbol, cùng adapter công khai và quyền nguồn.
    from trading_bot.application.funding_rate_collector import (
        FundingRateCollector,
        run_funding_collector,
    )

    funding = FundingRateCollector(
        workspace_id=workspace_id,
        symbols=[stream.symbol for stream in collector.streams],
        market_data=adapter,
        session_scope=database_collector_scope(session_factory, workspace_id),
    )

    def remember_funding(tick: Any) -> None:
        application.state.funding_collector_last = tick

    application.state.funding_collector_task = asyncio.create_task(
        run_funding_collector(funding, stopping, on_tick=remember_funding),
        name="funding-rate-collector",
    )
    return task


def _start_macro_calendar_collector(
    application: FastAPI,
    *,
    session_factory: Any,
    workspace_id: UUID,
    stopping: asyncio.Event,
) -> asyncio.Task[None]:
    """AI-4: lịch công bố vĩ mô BEA/FOMC cho H06; cần policy PAPER_DEMO của từng nguồn."""
    from trading_bot.adapters.sources.macro_calendar_http import MacroCalendarHttpFetcher
    from trading_bot.application.macro_calendar_collector import (
        MacroCalendarCollector,
        run_macro_collector,
    )
    from trading_bot.workers.macro_calendar_worker import database_macro_store_scope

    fetcher = MacroCalendarHttpFetcher()
    application.state.macro_calendar_fetcher = fetcher
    collector = MacroCalendarCollector(
        workspace_id=workspace_id,
        fetcher=fetcher,
        store_scope=database_macro_store_scope(session_factory, workspace_id),
    )

    def remember(results: Any) -> None:
        application.state.macro_calendar_last = {r.source_id: r.status for r in results}

    task = asyncio.create_task(
        run_macro_collector(collector, stopping, on_tick=remember), name="macro-calendar"
    )
    application.state.macro_calendar_task = task
    return task


def _start_derivatives_collector(
    application: FastAPI,
    *,
    app_settings: AppSettings,
    session_factory: Any,
    workspace_id: UUID,
    stopping: asyncio.Event,
) -> asyncio.Task[None]:
    """AI-4 lát 2: ảnh chụp phái sinh Binance theo giờ; cần policy PAPER_DEMO của nguồn."""
    from trading_bot.adapters.sources.binance_derivatives_http import (
        BinanceDerivativesHttpFetcher,
    )
    from trading_bot.application.derivatives_collector import (
        DerivativesCollector,
        run_derivatives_collector,
    )
    from trading_bot.application.market_bar_collector import parse_collector_streams
    from trading_bot.workers.derivatives_worker import database_derivatives_store_scope

    symbols = tuple(
        dict.fromkeys(
            s.symbol for s in parse_collector_streams(app_settings.market_collector_streams)
        )
    )
    fetcher = BinanceDerivativesHttpFetcher()
    application.state.derivatives_fetcher = fetcher
    collector = DerivativesCollector(
        workspace_id=workspace_id,
        symbols=symbols,
        fetcher=fetcher,
        store_scope=database_derivatives_store_scope(session_factory, workspace_id),
    )

    def remember(result: Any) -> None:
        application.state.derivatives_last = result.status

    task = asyncio.create_task(
        run_derivatives_collector(collector, stopping, on_tick=remember), name="derivatives"
    )
    application.state.derivatives_task = task
    return task


def _start_listings_collector(
    application: FastAPI,
    *,
    session_factory: Any,
    workspace_id: UUID,
    stopping: asyncio.Event,
) -> asyncio.Task[None]:
    """AI-4 lát 3: kiểm exchangeInfo Binance để phát hiện niêm yết/gỡ; cần policy PAPER_DEMO."""
    from trading_bot.adapters.sources.binance_derivatives_http import (
        BinanceDerivativesHttpFetcher,
    )
    from trading_bot.application.listings_collector import (
        ListingsCollector,
        run_listings_collector,
    )
    from trading_bot.workers.listings_worker import database_listings_store_scope

    fetcher = BinanceDerivativesHttpFetcher()
    application.state.listings_fetcher = fetcher
    collector = ListingsCollector(
        workspace_id=workspace_id,
        fetcher=fetcher,
        store_scope=database_listings_store_scope(session_factory, workspace_id),
    )

    def remember(result: Any) -> None:
        application.state.listings_last = result.status

    task = asyncio.create_task(
        run_listings_collector(collector, stopping, on_tick=remember), name="listings"
    )
    application.state.listings_task = task
    return task


def _market_collector_status(app: FastAPI) -> dict[str, Any] | None:
    """Bộ đếm trạng thái collector cho readiness; không chứa chi tiết policy hay payload."""
    worker = getattr(app.state, "market_collector", None)
    if worker is None:
        return None
    task = getattr(app.state, "market_collector_task", None)
    tick = worker.last_tick
    if task is None or task.done():
        return {"state": "stopped"}
    if tick is None:
        return {"state": "starting"}
    statuses = [result.status for result in tick.results]
    return {
        "state": "ok" if tick.succeeded else "degraded",
        "streams": len(statuses),
        "rights_denied": statuses.count("rights_denied"),
        "errors": statuses.count("error"),
    }


def _worker_readiness_report(
    *,
    component: str,
    worker: Any,
    task: Any,
    missing_secret_reason: str | None = None,
) -> WorkerReadinessReport:
    """Ưu tiên snapshot riêng của worker nếu có; ngược lại đánh giá chuẩn hóa chung."""
    snapshot_fn = getattr(worker, "readiness_snapshot", None)
    if callable(snapshot_fn) and missing_secret_reason is None:
        report = snapshot_fn(task=task)
        if isinstance(report, WorkerReadinessReport):
            return report
    return evaluate_worker_readiness(
        component=component,
        worker=worker,
        task=task,
        require_task=True,
        missing_secret_reason=missing_secret_reason,
    )
