"""Ranh giới application cho PAPER decision và lifecycle commands."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models.entities import StrategyRelease, WorkspaceSettingsVersion
from trading_bot.adapters.database.models.paper_entities import OrderIntentRecord, TradeRecord
from trading_bot.adapters.database.repositories.bot_config_versions import (
    BotConfigVersionRepository,
)
from trading_bot.adapters.database.repositories.risk_reservations import (
    RiskReservationRepository,
)
from trading_bot.adapters.database.repositories.workspace_settings import (
    SettingsVersionConflict,
    WorkspaceSettingsRepository,
    settings_hash,
)
from trading_bot.application.bot_config_resolver import (
    BotConfigResolutionError,
    EffectiveBotConfig,
    load_effective_bot_config,
)
from trading_bot.application.outbox import AfterCommitCallbackError, UnitOfWork
from trading_bot.application.paper_decision_cycle import (
    DecisionCycleOutcome,
    DecisionCycleResult,
    PaperDecisionCycle,
)
from trading_bot.application.paper_execution_profile import (
    ExecutionProfileResolutionError,
    PaperExecutionProfileCatalog,
    ResolvedPaperExecutionProfile,
    default_paper_execution_profile_catalog,
    resolve_paper_execution_profile,
)
from trading_bot.application.paper_lifecycle import (
    EntryLifecycleResult,
    PaperLifecycleConflict,
    PaperSubmissionResult,
    PositionLifecycleResult,
)
from trading_bot.application.paper_runtime_bootstrap import PaperRuntimeAccount
from trading_bot.config.bot_settings import BotConfigOverlay, overlay_hash
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings
from trading_bot.domain.execution.paper import PaperBar
from trading_bot.domain.risk.engine import RiskState
from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    OpenExposure,
    PlannerSettings,
    PlanRejected,
)
from trading_bot.domain.strategy.contracts import DecisionSnapshot, NoTrade, StrategySelection
from trading_bot.domain.strategy.registry import StrategyRegistry


class PaperRuntimeApplicationError(RuntimeError):
    """Lỗi command PAPER có mã an toàn, không chứa dữ liệu nhạy cảm."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class PaperDecisionCommand:
    """Yêu cầu đánh giá từ feature snapshot đã lưu trong workspace."""

    account_id: UUID
    feature_snapshot_id: str
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class PaperDecisionContext:
    """Context point-in-time do application port tin cậy nạp từ fixture/pipeline."""

    snapshot: DecisionSnapshot
    planner_settings: PlannerSettings
    account: AccountSnapshot
    risk_state: RiskState
    account_quote_asset: str
    open_exposures: tuple[OpenExposure, ...]
    evidence: Mapping[str, object]


class BotConfigLoader(Protocol):
    """Nạp cấu hình hiệu lực của bot trong UoW decision; None khi bot chưa có version."""

    async def __call__(
        self,
        *,
        uow: UnitOfWork,
        account: PaperRuntimeAccount,
        published: PublishedWorkspaceSettings,
        as_of: datetime,
    ) -> EffectiveBotConfig | None: ...


class PaperDecisionContextProvider(Protocol):
    """Nạp dữ liệu có provenance; caller không gửi risk-approved result."""

    async def load_context(
        self,
        *,
        uow: UnitOfWork,
        command: PaperDecisionCommand,
        published_settings: PublishedWorkspaceSettings,
        execution_profile: ResolvedPaperExecutionProfile,
        account: PaperRuntimeAccount,
    ) -> PaperDecisionContext: ...

    async def load_monitor_evidence(
        self,
        *,
        uow: UnitOfWork,
        command: PaperMonitorCommand,
        published_settings: PublishedWorkspaceSettings,
        execution_profile: ResolvedPaperExecutionProfile,
        account: PaperRuntimeAccount,
        position: Any,
        pinned_strategy_release_name: str | None,
        pinned_strategy_release_hash: str | None,
        pinned_strategy_settings_version_id: str | None,
        pinned_strategy_parameters: tuple[tuple[str, int], ...],
    ) -> PaperMonitorEvidence: ...


@dataclass(frozen=True, slots=True)
class PaperDecisionApplicationResult:
    decision: DecisionCycleResult | None
    idempotency_key: str | None = None
    submission: PaperSubmissionResult | None = None
    risk_decision_id: UUID | None = None
    risk_reservation_id: UUID | None = None
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class PaperEntryEventCommand:
    account_id: UUID
    ledger_intent_id: UUID
    paper_intent_id: str
    bar: PaperBar


@dataclass(frozen=True, slots=True)
class PaperMonitorCommand:
    account_id: UUID
    ledger_intent_id: UUID
    trade_id: UUID
    paper_position_id: str
    bar: PaperBar
    feature_snapshot_id: str


@dataclass(frozen=True, slots=True)
class PaperMonitorEvidence:
    """Tín hiệu thoát có provenance từ snapshot đã lưu, không do caller tự khai."""

    workspace_id: UUID
    account_id: UUID
    feature_snapshot_id: str
    symbol: str
    available_at: datetime
    strategy_release_name: str
    strategy_release_hash: str
    opposing_signal: bool
    atr: Decimal | None
    strategy_settings_version_id: str | None = None
    strategy_parameters: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class PinnedPositionStrategy:
    """Chính sách strategy bất biến gắn với settings version lúc mở vị thế."""

    release_name: str | None
    release_hash: str | None
    settings_version_id: str | None
    parameters: tuple[tuple[str, int], ...] = ()


class PaperRuntimeApplicationService:
    """Chỉ nhận account đã bootstrap và gom command qua UoW tenant-scoped."""

    def __init__(
        self,
        *,
        workspace_id: UUID,
        sessions: async_sessionmaker[AsyncSession] | None,
        active_accounts: Mapping[UUID, PaperRuntimeAccount],
        context_provider: PaperDecisionContextProvider | None = None,
        profile_catalog: PaperExecutionProfileCatalog | None = None,
        unit_of_work_factory: Any = UnitOfWork,
        settings_repository_factory: Any = WorkspaceSettingsRepository,
        risk_repository_factory: Any = RiskReservationRepository,
        decision_cycle_factory: Any | None = None,
        account_invalidation_callback: Callable[[UUID, str], None] | None = None,
        bot_config_loader: BotConfigLoader | None = None,
    ) -> None:
        self._workspace_id = workspace_id
        self._sessions = sessions
        self._active_accounts = dict(active_accounts)
        self._context_provider = context_provider
        self._profile_catalog = profile_catalog or default_paper_execution_profile_catalog()
        self._unit_of_work_factory = unit_of_work_factory
        self._settings_repository_factory = settings_repository_factory
        self._risk_repository_factory = risk_repository_factory
        self._decision_cycle_factory = decision_cycle_factory or self._make_decision_cycle
        self._account_invalidation_callback = account_invalidation_callback
        self._bot_config_loader = bot_config_loader or load_runtime_bot_config
        self._invalid_accounts: set[UUID] = set()

    def is_available_for(self, account_id: UUID) -> bool:
        account = self._active_accounts.get(account_id)
        return (
            account_id not in self._invalid_accounts
            and account is not None
            and getattr(account, "account_id", None) == account_id
        )

    @property
    def is_ready(self) -> bool:
        return not self._invalid_accounts

    async def evaluate_and_submit(
        self, command: PaperDecisionCommand
    ) -> PaperDecisionApplicationResult:
        """Đánh giá, reserve và tạo intent/checkpoint trong một transaction."""
        account = self._require_account(command.account_id)
        if not command.feature_snapshot_id.strip():
            raise PaperRuntimeApplicationError("FEATURE_SNAPSHOT_REQUIRED")
        if command.evaluated_at.tzinfo is None or command.evaluated_at.utcoffset() is None:
            raise PaperRuntimeApplicationError("DECISION_TIME_INVALID")
        if self._sessions is None:
            raise PaperRuntimeApplicationError("DATABASE_UNAVAILABLE")
        if self._context_provider is None:
            raise PaperRuntimeApplicationError("PAPER_CONTEXT_NOT_CONFIGURED")

        try:
            async with self._unit_of_work_factory(
                self._sessions, workspace_id=self._workspace_id, bot_id=account.bot_id
            ) as uow:
                await self._settings_repository_factory(uow.session).lock_workspace(
                    self._workspace_id
                )
                published, profile = await self._load_published_profile(
                    uow, account=account, as_of=command.evaluated_at
                )
                bot_config = await self._load_bot_config(
                    uow, account=account, published=published, as_of=command.evaluated_at
                )
                bot_lineage = _bot_lineage(bot_config)
                strategy_release_id = await self._resolve_strategy_release_id(
                    uow, published=published
                )
                idempotency_key = _decision_idempotency_key(
                    account_id=command.account_id,
                    settings_version_id=published.settings_version_id,
                    strategy_release=published.settings.strategy.default_release,
                    feature_snapshot_id=command.feature_snapshot_id,
                    bot_config_version_id=(
                        bot_config.bot_config_version_id if bot_config is not None else None
                    ),
                )
                existing = await uow.session.scalar(
                    select(OrderIntentRecord).where(
                        OrderIntentRecord.workspace_id == self._workspace_id,
                        OrderIntentRecord.account_id == command.account_id,
                        OrderIntentRecord.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    if (
                        existing.settings_version_id != published.settings_version_id
                        or existing.feature_snapshot_id != command.feature_snapshot_id
                        or existing.evidence.get("settings_hash") != published.settings_hash
                        or existing.evidence.get("paper_execution_profile_hash")
                        != profile.profile_hash
                        or existing.evidence.get("evaluated_at") != command.evaluated_at.isoformat()
                        or existing.evidence.get("strategy_release_hash")
                        != StrategyRegistry()
                        .get_release(published.settings.strategy.default_release)
                        .content_hash
                        or any(
                            existing.evidence.get(key) != value
                            for key, value in _bot_lineage_expectation(bot_lineage).items()
                        )
                    ):
                        raise PaperRuntimeApplicationError("PAPER_IDEMPOTENCY_CONFLICT")
                    try:
                        order = account.gateway.order(f"paper:{idempotency_key}")
                    except KeyError:
                        raise PaperRuntimeApplicationError(
                            "PAPER_IDEMPOTENT_ORDER_NOT_HYDRATED"
                        ) from None
                    return PaperDecisionApplicationResult(
                        decision=None,
                        idempotency_key=idempotency_key,
                        submission=PaperSubmissionResult(order, existing.id, True),
                        risk_decision_id=existing.risk_decision_id,
                        risk_reservation_id=existing.risk_reservation_id,
                        duplicate=True,
                    )
                context = await self._context_provider.load_context(
                    uow=uow,
                    command=command,
                    published_settings=published,
                    execution_profile=profile,
                    account=account,
                )
                self._validate_context(
                    context,
                    command=command,
                    account=account,
                    published=published,
                    profile=profile,
                )
                cycle = self._decision_cycle_factory(account.gateway)
                decision = await cycle.prepare_published_with_risk_profile(
                    session=uow.session,
                    published_settings=published,
                    snapshot=context.snapshot,
                    evaluated_at=command.evaluated_at,
                    planner_settings=context.planner_settings,
                    account=context.account,
                    risk_state=context.risk_state,
                    open_exposures=context.open_exposures,
                    cost_model=profile.cost_model,
                    bot_config=bot_config,
                )
                if decision.outcome is not DecisionCycleOutcome.RISK_APPROVED:
                    if (
                        decision.outcome is DecisionCycleOutcome.RISK_REJECTED
                        and decision.plan is not None
                        and decision.risk_decision is not None
                    ):
                        await self._risk_repository_factory(uow.session).record_evaluation(
                            plan=decision.plan, decision=decision.risk_decision
                        )
                    return PaperDecisionApplicationResult(decision=decision)

                plan = decision.plan
                risk_decision = decision.risk_decision
                if (
                    plan is None
                    or isinstance(plan, PlanRejected)
                    or risk_decision is None
                    or not risk_decision.approved
                ):
                    raise PaperRuntimeApplicationError("DECISION_RESULT_INVALID")
                self._validate_prepared_decision(
                    decision,
                    command=command,
                    account=account,
                    context=context,
                    published=published,
                    profile=profile,
                )
                risk_repository = self._risk_repository_factory(uow.session)
                evaluation = await risk_repository.record_evaluation(
                    plan=plan, decision=risk_decision
                )
                reservation = await risk_repository.reserve(
                    risk_decision,
                    idempotency_key=idempotency_key,
                    risk_decision_id=evaluation.risk_decision_id,
                )
                evidence = dict(context.evidence)
                evidence.update(
                    {
                        "settings_hash": published.settings_hash,
                        "paper_execution_profile_hash": profile.profile_hash,
                        "strategy_release_hash": StrategyRegistry()
                        .get_release(published.settings.strategy.default_release)
                        .content_hash,
                        "feature_snapshot_id": command.feature_snapshot_id,
                        "evaluated_at": command.evaluated_at.isoformat(),
                        "risk_decision_id": str(evaluation.risk_decision_id),
                        "plan_hash": plan.plan_hash,
                    }
                    | bot_lineage
                )
                submission = await account.coordinator.submit_approved(
                    uow,
                    account_id=command.account_id,
                    result=decision,
                    evaluated_at=command.evaluated_at,
                    idempotency_key=idempotency_key,
                    strategy_release_id=strategy_release_id,
                    feature_snapshot_id=command.feature_snapshot_id,
                    risk_decision_id=evaluation.risk_decision_id,
                    risk_reservation_id=reservation.reservation_id,
                    quote_asset=profile.quote_asset,
                    evidence=evidence,
                )
                return PaperDecisionApplicationResult(
                    decision=decision,
                    idempotency_key=idempotency_key,
                    submission=submission,
                    risk_decision_id=evaluation.risk_decision_id,
                    risk_reservation_id=reservation.reservation_id,
                )
        except PaperRuntimeApplicationError:
            raise
        except AfterCommitCallbackError:
            self._invalidate_account(command.account_id, "PAPER_PUBLISH_FAILED")
            raise PaperRuntimeApplicationError("PAPER_PUBLISH_FAILED") from None
        except PaperLifecycleConflict, SettingsVersionConflict, ExecutionProfileResolutionError:
            raise PaperRuntimeApplicationError("PAPER_COMMAND_REJECTED") from None

    async def apply_entry_event(self, command: PaperEntryEventCommand) -> EntryLifecycleResult:
        """Áp dụng một bar entry qua coordinator và ledger cùng UoW."""
        account = self._require_account(command.account_id)
        self._validate_bar_scope(command.bar, account)
        if self._sessions is None:
            raise PaperRuntimeApplicationError("DATABASE_UNAVAILABLE")
        try:
            async with self._unit_of_work_factory(
                self._sessions, workspace_id=self._workspace_id, bot_id=account.bot_id
            ) as uow:
                await self._settings_repository_factory(uow.session).lock_workspace(
                    self._workspace_id
                )
                published, profile = await self._load_published_profile(
                    uow, account=account, as_of=command.bar.opened_at
                )
                intent = await _load_scoped_intent(
                    uow, account_id=command.account_id, intent_id=command.ledger_intent_id
                )
                if (
                    intent is None
                    or command.paper_intent_id != f"paper:{intent.idempotency_key}"
                    or intent.symbol != command.bar.symbol
                ):
                    raise PaperRuntimeApplicationError("PAPER_INTENT_SCOPE_MISMATCH")
                return await account.coordinator.apply_entry_market_event(
                    uow,
                    account_id=command.account_id,
                    paper_intent_id=command.paper_intent_id,
                    ledger_intent_id=command.ledger_intent_id,
                    bar=command.bar,
                )
        except PaperRuntimeApplicationError:
            raise
        except AfterCommitCallbackError:
            self._invalidate_account(command.account_id, "PAPER_PUBLISH_FAILED")
            raise PaperRuntimeApplicationError("PAPER_PUBLISH_FAILED") from None
        except PaperLifecycleConflict, SettingsVersionConflict, ExecutionProfileResolutionError:
            raise PaperRuntimeApplicationError("PAPER_COMMAND_REJECTED") from None

    async def monitor_position(self, command: PaperMonitorCommand) -> PositionLifecycleResult:
        """Áp dụng một bar monitor cho trade đúng account đã bootstrap."""
        account = self._require_account(command.account_id)
        self._validate_bar_scope(command.bar, account)
        if self._sessions is None:
            raise PaperRuntimeApplicationError("DATABASE_UNAVAILABLE")
        try:
            async with self._unit_of_work_factory(
                self._sessions, workspace_id=self._workspace_id, bot_id=account.bot_id
            ) as uow:
                await self._settings_repository_factory(uow.session).lock_workspace(
                    self._workspace_id
                )
                published, profile = await self._load_published_profile(
                    uow, account=account, as_of=command.bar.opened_at
                )
                await self._resolve_strategy_release_id(uow, published=published)
                intent = await _load_scoped_intent(
                    uow, account_id=command.account_id, intent_id=command.ledger_intent_id
                )
                trade = await uow.session.scalar(
                    select(TradeRecord).where(
                        TradeRecord.workspace_id == self._workspace_id,
                        TradeRecord.account_id == command.account_id,
                        TradeRecord.id == command.trade_id,
                        TradeRecord.intent_id == command.ledger_intent_id,
                    )
                )
                if (
                    intent is None
                    or trade is None
                    or command.paper_position_id != f"paper:{intent.idempotency_key}"
                    or intent.symbol != command.bar.symbol
                    or trade.status != "OPEN"
                ):
                    raise PaperRuntimeApplicationError("PAPER_POSITION_SCOPE_MISMATCH")
                pinned_strategy = await self._load_position_release_pin(
                    uow, intent=intent, trade=trade
                )
                position = account.gateway.position(command.paper_position_id)
                if position is None:
                    raise PaperRuntimeApplicationError("PAPER_POSITION_NOT_READY")
                if self._context_provider is None:
                    raise PaperRuntimeApplicationError("PAPER_MONITOR_EVIDENCE_NOT_CONFIGURED")
                evidence = await self._context_provider.load_monitor_evidence(
                    uow=uow,
                    command=command,
                    published_settings=published,
                    execution_profile=profile,
                    account=account,
                    position=position,
                    pinned_strategy_release_name=pinned_strategy.release_name,
                    pinned_strategy_release_hash=pinned_strategy.release_hash,
                    pinned_strategy_settings_version_id=pinned_strategy.settings_version_id,
                    pinned_strategy_parameters=pinned_strategy.parameters,
                )
                opposing_signal = self._validate_monitor_evidence(
                    evidence,
                    command=command,
                    account=account,
                    published=published,
                    pinned_strategy_release_name=pinned_strategy.release_name,
                    pinned_strategy_release_hash=pinned_strategy.release_hash,
                    pinned_strategy_settings_version_id=pinned_strategy.settings_version_id,
                    pinned_strategy_parameters=pinned_strategy.parameters,
                )
                return await account.coordinator.monitor_market_event(
                    uow,
                    account_id=command.account_id,
                    paper_position_id=command.paper_position_id,
                    ledger_intent_id=command.ledger_intent_id,
                    trade_id=command.trade_id,
                    position=position,
                    bar=command.bar,
                    opposing_signal=opposing_signal,
                    atr=evidence.atr,
                )
        except PaperRuntimeApplicationError:
            raise
        except AfterCommitCallbackError:
            self._invalidate_account(command.account_id, "PAPER_PUBLISH_FAILED")
            raise PaperRuntimeApplicationError("PAPER_PUBLISH_FAILED") from None
        except PaperLifecycleConflict, SettingsVersionConflict, ExecutionProfileResolutionError:
            raise PaperRuntimeApplicationError("PAPER_COMMAND_REJECTED") from None

    def _require_account(self, account_id: UUID) -> PaperRuntimeAccount:
        account = self._active_accounts.get(account_id)
        if (
            account_id in self._invalid_accounts
            or account is None
            or getattr(account, "account_id", None) != account_id
            or getattr(account, "execution_profile", None) is None
            or getattr(account, "coordinator", None) is None
            or getattr(account, "gateway", None) is None
        ):
            raise PaperRuntimeApplicationError("PAPER_ACCOUNT_NOT_READY")
        return account

    async def _load_published_profile(
        self,
        uow: UnitOfWork,
        *,
        account: PaperRuntimeAccount,
        as_of: datetime,
    ) -> tuple[PublishedWorkspaceSettings, ResolvedPaperExecutionProfile]:
        if uow.workspace_id != self._workspace_id:
            raise PaperRuntimeApplicationError("PAPER_WORKSPACE_SCOPE_MISMATCH")
        published = await self._settings_repository_factory(uow.session).load_effective_config(
            workspace_id=self._workspace_id, as_of=as_of
        )
        if published is None or published.workspace_id != self._workspace_id:
            raise PaperRuntimeApplicationError("PAPER_SETTINGS_NOT_EFFECTIVE")
        profile = resolve_paper_execution_profile(published, self._profile_catalog)
        bootstrapped_profile = account.execution_profile
        if profile.profile_hash != bootstrapped_profile.profile_hash:
            self._invalidate_account(account.account_id, "PAPER_PROFILE_DRIFT")
            raise PaperRuntimeApplicationError("PAPER_PROFILE_DRIFT")
        return published, profile

    async def _load_bot_config(
        self,
        uow: UnitOfWork,
        *,
        account: PaperRuntimeAccount,
        published: PublishedWorkspaceSettings,
        as_of: datetime,
    ) -> EffectiveBotConfig | None:
        """Resolve overlay bot cho entry mới; mọi sai lệch đều chặn entry (fail-closed)."""
        try:
            config = await self._bot_config_loader(
                uow=uow, account=account, published=published, as_of=as_of
            )
        except BotConfigResolutionError:
            raise PaperRuntimeApplicationError("PAPER_BOT_CONFIG_NOT_EFFECTIVE") from None
        if config is None:
            return None
        if (
            config.workspace_id != self._workspace_id
            or config.account_id != account.account_id
            or config.bot_id != account.bot_id
            or config.settings_version_id != published.settings_version_id
            or config.settings_hash != published.settings_hash
            or not config.verify_hash()
        ):
            raise PaperRuntimeApplicationError("PAPER_BOT_CONFIG_SCOPE_MISMATCH")
        if config.strategy.release_id != published.settings.strategy.default_release:
            # Chạy release riêng của bot cần fan-out theo bot (MB-03); chưa hỗ trợ ở đây.
            raise PaperRuntimeApplicationError("PAPER_BOT_STRATEGY_FANOUT_REQUIRED")
        return config

    def _invalidate_account(self, account_id: UUID, reason_code: str) -> None:
        self._invalid_accounts.add(account_id)
        if self._account_invalidation_callback is not None:
            self._account_invalidation_callback(account_id, reason_code)

    async def _load_position_release_pin(
        self, uow: UnitOfWork, *, intent: OrderIntentRecord, trade: TradeRecord
    ) -> PinnedPositionStrategy:
        """Nạp policy strategy từ settings bất biến lúc mở; lỗi chỉ chặn tín hiệu đối nghịch."""
        settings_version_id = intent.settings_version_id
        invalid_pin = PinnedPositionStrategy(None, None, None)
        if trade.lineage.get("settings_version_id") != str(settings_version_id):
            return invalid_pin
        settings_version = await uow.session.scalar(
            select(WorkspaceSettingsVersion).where(
                WorkspaceSettingsVersion.workspace_id == self._workspace_id,
                WorkspaceSettingsVersion.id == settings_version_id,
                WorkspaceSettingsVersion.status == "PUBLISHED",
            )
        )
        if (
            settings_version is None
            or settings_version.workspace_id != self._workspace_id
            or settings_hash(settings_version.settings_json) != settings_version.settings_hash
        ):
            return invalid_pin
        try:
            settings = WorkspaceSettings.model_validate(settings_version.settings_json)
            parameters = tuple(sorted(settings.strategy.parameters.items()))
            StrategySelection(
                settings_version_id=str(settings_version_id),
                active_release_ids=(settings.strategy.default_release,),
                published=True,
                parameters=parameters,
            )
        except TypeError, ValueError:
            return invalid_pin
        release_id = intent.strategy_release_id
        release_hash = intent.strategy_release_hash
        lineage = trade.lineage
        if (
            trade.strategy_release_id != release_id
            or lineage.get("strategy_release_id") != str(release_id)
            or lineage.get("strategy_release_hash") != release_hash
        ):
            return invalid_pin
        release = await uow.session.scalar(
            select(StrategyRelease).where(
                StrategyRelease.workspace_id == self._workspace_id,
                StrategyRelease.id == release_id,
            )
        )
        if release is None or release.content_hash != release_hash:
            return invalid_pin
        try:
            registered = StrategyRegistry().get_release(release.name)
        except KeyError:
            return invalid_pin
        if (
            registered.content_hash != release_hash
            or settings.strategy.default_release != release.name
        ):
            return invalid_pin
        pinned_parameters = await self._pinned_bot_parameters(
            uow,
            intent=intent,
            root_release=settings.strategy.default_release,
            root_parameters=parameters,
        )
        if pinned_parameters is None:
            return invalid_pin
        return PinnedPositionStrategy(
            release.name, release_hash, str(settings_version_id), pinned_parameters
        )

    async def _pinned_bot_parameters(
        self,
        uow: UnitOfWork,
        *,
        intent: OrderIntentRecord,
        root_release: str,
        root_parameters: tuple[tuple[str, int], ...],
    ) -> tuple[tuple[str, int], ...] | None:
        """Tham số strategy theo version bot ghi trong intent lúc mở; None nếu lineage hỏng."""
        evidence = intent.evidence
        raw_version_id = evidence.get("bot_config_version_id")
        if raw_version_id is None:
            return root_parameters
        try:
            version_id = UUID(str(raw_version_id))
        except ValueError:
            return None
        record = await BotConfigVersionRepository(uow.session).get(
            workspace_id=self._workspace_id, bot_id=intent.bot_id, version_id=version_id
        )
        if (
            record is None
            or record.status != "PUBLISHED"
            or overlay_hash(record.overlay_json) != record.overlay_hash
            or record.overlay_hash != evidence.get("bot_config_hash")
        ):
            return None
        try:
            overlay = BotConfigOverlay.model_validate(record.overlay_json)
        except ValidationError:
            return None
        if overlay.strategy is None:
            return root_parameters
        if overlay.strategy.release_id != root_release:
            return None
        parameters = tuple(sorted(overlay.strategy.parameters.items()))
        try:
            StrategySelection(
                settings_version_id=str(intent.settings_version_id),
                active_release_ids=(root_release,),
                published=True,
                parameters=parameters,
            )
        except TypeError, ValueError:
            return None
        return parameters

    @staticmethod
    def _validate_monitor_evidence(
        evidence: PaperMonitorEvidence,
        *,
        command: PaperMonitorCommand,
        account: PaperRuntimeAccount,
        published: PublishedWorkspaceSettings,
        pinned_strategy_release_name: str | None,
        pinned_strategy_release_hash: str | None,
        pinned_strategy_settings_version_id: str | None,
        pinned_strategy_parameters: tuple[tuple[str, int], ...],
    ) -> bool:
        available_at = evidence.available_at
        if (
            evidence.workspace_id != published.workspace_id
            or evidence.account_id != account.account_id
            or evidence.feature_snapshot_id != command.feature_snapshot_id
            or evidence.symbol != command.bar.symbol
            or available_at.tzinfo is None
            or available_at.utcoffset() is None
            or available_at > command.bar.opened_at
            or (evidence.atr is not None and (evidence.atr <= 0 or not evidence.atr.is_finite()))
        ):
            raise PaperRuntimeApplicationError("PAPER_MONITOR_EVIDENCE_MISMATCH")
        if (
            pinned_strategy_release_name is None
            or pinned_strategy_release_hash is None
            or pinned_strategy_settings_version_id is None
            or evidence.strategy_settings_version_id != pinned_strategy_settings_version_id
            or evidence.strategy_parameters != pinned_strategy_parameters
        ):
            return False
        try:
            registered = StrategyRegistry().get_release(pinned_strategy_release_name)
        except KeyError:
            return False
        return (
            registered.content_hash == pinned_strategy_release_hash
            and evidence.strategy_release_name == pinned_strategy_release_name
            and evidence.strategy_release_hash == pinned_strategy_release_hash
            and evidence.opposing_signal
        )

    async def _resolve_strategy_release_id(
        self, uow: UnitOfWork, *, published: PublishedWorkspaceSettings
    ) -> UUID:
        """Resolve el UUID từ release đã publish trong workspace, không từ caller."""
        release_name = published.settings.strategy.default_release
        try:
            expected_hash = StrategyRegistry().get_release(release_name).content_hash
        except KeyError:
            raise PaperRuntimeApplicationError("PAPER_STRATEGY_RELEASE_NOT_PUBLISHED") from None
        result = await uow.session.scalars(
            select(StrategyRelease)
            .where(
                StrategyRelease.workspace_id == self._workspace_id,
                StrategyRelease.name == release_name,
                StrategyRelease.status == "PUBLISHED",
                StrategyRelease.content_hash == expected_hash,
            )
            .limit(2)
        )
        releases = result.all()
        if (
            len(releases) != 1
            or releases[0].workspace_id != self._workspace_id
            or releases[0].name != release_name
            or releases[0].status != "PUBLISHED"
            or releases[0].content_hash != expected_hash
        ):
            raise PaperRuntimeApplicationError("PAPER_STRATEGY_RELEASE_NOT_PUBLISHED")
        return releases[0].id

    @staticmethod
    def _validate_context(
        context: PaperDecisionContext,
        *,
        command: PaperDecisionCommand,
        account: PaperRuntimeAccount,
        published: PublishedWorkspaceSettings,
        profile: ResolvedPaperExecutionProfile,
    ) -> None:
        runtime_account_id = str(account.account_id)
        settings_version_id = str(published.settings_version_id)
        if (
            context.snapshot.snapshot_id != command.feature_snapshot_id
            or context.planner_settings.workspace_id != str(published.workspace_id)
            or context.planner_settings.settings_version_id != settings_version_id
            or context.planner_settings.settings_hash != published.settings_hash
            or not context.planner_settings.published
            or context.planner_settings.active_release_ids
            != (published.settings.strategy.default_release,)
            or context.account.workspace_id != str(published.workspace_id)
            or context.account.account_id != runtime_account_id
            or context.account.settings_version_id != settings_version_id
            or context.risk_state.workspace_id != str(published.workspace_id)
            or context.risk_state.account_id != runtime_account_id
            or context.risk_state.settings_version_id != settings_version_id
            or context.risk_state.risk_profile_version_id
            != str(published.settings.risk.profile_version_id)
            or context.risk_state.evaluated_at != command.evaluated_at
            or context.risk_state.account_captured_at != context.account.captured_at
            or context.risk_state.equity != context.account.equity
            or context.risk_state.available_margin != context.account.available_margin
            or context.risk_state.market_data_as_of > context.snapshot.decision_time
            or context.snapshot.decision_time > command.evaluated_at
            or context.risk_state.open_exposures != context.open_exposures
            or context.risk_state.open_position_count != len(context.open_exposures)
            or context.snapshot.symbol not in published.settings.market_data.symbols
            or any(
                bar.available_at > context.snapshot.decision_time
                for series in context.snapshot.series
                for bar in series.bars
            )
            or any(
                feature.available_at > context.snapshot.decision_time
                for feature in context.snapshot.features
            )
            or not context.risk_state.is_known
        ):
            raise PaperRuntimeApplicationError("PAPER_DECISION_CONTEXT_MISMATCH")
        if context.account_quote_asset != profile.quote_asset:
            raise PaperRuntimeApplicationError("PAPER_ACCOUNT_QUOTE_ASSET_MISMATCH")
        profile.validate_account_snapshot(
            context.account, account_quote_asset=context.account_quote_asset
        )

    @staticmethod
    def _validate_prepared_decision(
        decision: DecisionCycleResult,
        *,
        command: PaperDecisionCommand,
        account: PaperRuntimeAccount,
        context: PaperDecisionContext,
        published: PublishedWorkspaceSettings,
        profile: ResolvedPaperExecutionProfile,
    ) -> None:
        plan = decision.plan
        risk_decision = decision.risk_decision
        strategy_result = decision.strategy_result
        release_id = published.settings.strategy.default_release
        if (
            plan is None
            or isinstance(plan, PlanRejected)
            or risk_decision is None
            or isinstance(strategy_result, NoTrade)
            or plan.workspace_id != str(published.workspace_id)
            or plan.account_id != str(account.account_id)
            or plan.settings_version_id != str(published.settings_version_id)
            or plan.settings_hash != published.settings_hash
            or plan.risk_profile_version_id != str(published.settings.risk.profile_version_id)
            or plan.snapshot_id != command.feature_snapshot_id
            or plan.symbol != context.snapshot.symbol
            or plan.strategy_release_id != release_id
            or plan.release_content_hash
            != dict(context.planner_settings.release_content_hashes).get(release_id)
            or plan.candidate_id != getattr(strategy_result, "candidate_id", None)
            or plan.direction != strategy_result.direction
            or plan.cost_model_version != profile.profile_hash
            or risk_decision.plan_hash != plan.plan_hash
            or risk_decision.workspace_id != str(published.workspace_id)
            or risk_decision.account_id != str(account.account_id)
            or risk_decision.settings_version_id != str(published.settings_version_id)
            or risk_decision.risk_profile_version_id
            != str(published.settings.risk.profile_version_id)
            or risk_decision.snapshot_id != command.feature_snapshot_id
            or risk_decision.release_content_hash != plan.release_content_hash
        ):
            raise PaperRuntimeApplicationError("PAPER_DECISION_RESULT_MISMATCH")

    @staticmethod
    def _validate_bar_scope(bar: PaperBar, account: PaperRuntimeAccount) -> None:
        if (
            not bar.symbol.strip()
            or bar.opened_at.tzinfo is None
            or bar.opened_at.utcoffset() is None
            or bar.closed_at.tzinfo is None
            or bar.closed_at.utcoffset() is None
        ):
            raise PaperRuntimeApplicationError("PAPER_BAR_INVALID")
        if account.execution_profile.workspace_id is None:
            raise PaperRuntimeApplicationError("PAPER_PROFILE_INVALID")

    @staticmethod
    def _make_decision_cycle(gateway: Any) -> PaperDecisionCycle:
        from trading_bot.domain.risk.engine import RiskEngine
        from trading_bot.domain.risk.planner import TradePlanner
        from trading_bot.domain.strategy.registry import StrategyRegistry

        return PaperDecisionCycle(StrategyRegistry(), TradePlanner(), RiskEngine(), gateway)


async def _load_scoped_intent(
    uow: UnitOfWork, *, account_id: UUID, intent_id: UUID
) -> OrderIntentRecord | None:
    return await uow.session.scalar(
        select(OrderIntentRecord).where(
            OrderIntentRecord.workspace_id == uow.workspace_id,
            OrderIntentRecord.account_id == account_id,
            OrderIntentRecord.id == intent_id,
        )
    )


async def load_runtime_bot_config(
    *,
    uow: UnitOfWork,
    account: PaperRuntimeAccount,
    published: PublishedWorkspaceSettings,
    as_of: datetime,
) -> EffectiveBotConfig | None:
    """Loader mặc định: resolve cùng root đã nạp trong transaction decision hiện tại."""
    return await load_effective_bot_config(
        uow.session,
        workspace_id=uow.workspace_id,
        account_id=account.account_id,
        as_of=as_of,
        published=published,
    )


_BOT_LINEAGE_KEYS = ("bot_config_version_id", "bot_config_hash", "effective_config_hash")


def _bot_lineage(config: EffectiveBotConfig | None) -> dict[str, str]:
    """Lineage bot ghi vào evidence intent; rỗng khi bot dùng root như MB-01."""
    if config is None:
        return {}
    return {
        "bot_config_version_id": str(config.bot_config_version_id),
        "bot_config_hash": config.bot_config_hash,
        "effective_config_hash": config.effective_hash,
    }


def _bot_lineage_expectation(lineage: dict[str, str]) -> dict[str, str | None]:
    """Intent trùng key phải mang đúng lineage bot hiện tại (hoặc không có nếu dùng root)."""
    return {key: lineage.get(key) for key in _BOT_LINEAGE_KEYS}


def _decision_idempotency_key(
    *,
    account_id: UUID,
    settings_version_id: UUID,
    strategy_release: str,
    feature_snapshot_id: str,
    bot_config_version_id: UUID | None = None,
) -> str:
    material = {
        "account_id": str(account_id),
        "settings_version_id": str(settings_version_id),
        "strategy_release": strategy_release,
        "feature_snapshot_id": feature_snapshot_id,
    }
    # Chỉ thêm khi bot có version publish để key lịch sử (MB-01) giữ nguyên.
    if bot_config_version_id is not None:
        material["bot_config_version_id"] = str(bot_config_version_id)
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"paper-decision-v1:{digest}"
