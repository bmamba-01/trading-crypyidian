"""Bootstrap PAPER runtime theo bot: pha workspace rồi pha bot (MB05.5, ruling 1, 8, 11).

- Pha workspace (một UoW không bot scope): DB, settings hiệu lực, execution profile. Lỗi ở pha
  này chặn cả workspace (`BLOCKED_BY_WORKSPACE`, readiness 503).
- Pha bot (mỗi bot một UoW, bind đúng một account, **không** `allow_switch`): resolve
  `EffectiveBotConfig` (hoặc root-fallback), lấy lease (token mới), hydrate checkpoint, ghi
  `bot_runtime_state` có fence rồi mới công bố gateway sau commit. Lỗi chỉ chặn bot đó:
  hydrate lỗi -> `RECOVERY_REQUIRED` (vẫn giữ lease để ghi state + audit có fence), lease đang do
  process khác giữ -> `STANDBY` (không hydrate, không ghi).

Sau MB05.5 mọi account công bố đều mang `owner_id` + `fencing_token` (đóng chế độ chuyển tiếp
không fence của Part A); `owner_id` duy nhất theo mỗi lần khởi động process.
"""

from __future__ import annotations

import logging
import os
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.errors import (
    database_sqlstate,
    is_transient_database_error,
)
from trading_bot.adapters.database.repositories.bot_runtime_leases import BotRuntimeLease
from trading_bot.adapters.database.repositories.bot_runtime_states import (
    BotRuntimeStateRepository,
)
from trading_bot.adapters.database.repositories.paper_checkpoints import (
    PaperCheckpointRepository,
)
from trading_bot.adapters.database.repositories.paper_ledger import PaperLedgerRepository
from trading_bot.adapters.database.repositories.paper_runtime_accounts import (
    PaperRuntimeAccountRepository,
)
from trading_bot.adapters.database.repositories.workspace_settings import (
    SettingsVersionConflict,
    WorkspaceSettingsRepository,
)
from trading_bot.application.bot_config_resolver import (
    BotConfigResolutionError,
    load_effective_bot_config,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.paper_execution_profile import (
    ExecutionProfileResolutionError,
    PaperExecutionProfileCatalog,
    ResolvedPaperExecutionProfile,
    default_paper_execution_profile_catalog,
    resolve_paper_execution_profile,
)
from trading_bot.application.paper_lifecycle import (
    PaperLifecycleConflict,
    PaperLifecycleCoordinator,
)
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings
from trading_bot.domain.execution.paper import PaperExecutionGateway
from trading_bot.domain.execution.runtime_control import (
    MAX_LEASE_TTL,
    BotReadiness,
    FencingToken,
    LeaseGrant,
    RuntimeControlError,
    RuntimeFence,
)

logger = logging.getLogger(__name__)

DEFAULT_LEASE_TTL = timedelta(seconds=30)
# Lock/statement timeout khi bootstrap (fix round 2): tạm thời, thử lại; không phải lỗi recovery.
TRANSIENT_DATABASE_TIMEOUT = "TRANSIENT_DATABASE_TIMEOUT"


def new_runtime_owner_id() -> str:
    """ID instance process duy nhất mỗi lần khởi động (M6); không bí mật, ≤ 120 ký tự."""
    host = socket.gethostname()[:48] or "runtime"
    return f"{host}:{os.getpid()}:{uuid4().hex}"


class PaperAccountRuntimeStatus(StrEnum):
    READY = "READY"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    STANDBY = "STANDBY"
    BLOCKED_BY_WORKSPACE = "BLOCKED_BY_WORKSPACE"


@dataclass(frozen=True, slots=True)
class PaperAccountRuntimeState:
    status: PaperAccountRuntimeStatus
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class PaperRuntimeAccount:
    """Account đã hydrate với lease hiện hành; re-acquire (token mới) thì thay instance.

    `owner_id`/`fencing_token` bắt buộc (MB05.5 đóng chế độ chuyển tiếp không fence): không có
    runtime writer nào chạy mà không trình token.
    """

    account_id: UUID
    bot_id: UUID
    gateway: PaperExecutionGateway
    coordinator: PaperLifecycleCoordinator
    execution_profile: ResolvedPaperExecutionProfile
    owner_id: str | None = None
    fencing_token: FencingToken | None = None
    bot_config_version_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.owner_id is None or self.fencing_token is None:
            raise RuntimeControlError("FENCING_TOKEN_REQUIRED")

    @property
    def fence(self) -> RuntimeFence:
        """Bằng chứng lease trình cho mọi command thay đổi state PAPER."""
        if self.owner_id is None or self.fencing_token is None:  # pragma: no cover - post_init
            raise RuntimeControlError("FENCING_TOKEN_REQUIRED")
        return RuntimeFence(self.owner_id, self.fencing_token)


@dataclass(frozen=True, slots=True)
class PaperRuntimeBootstrapResult:
    """`ready` = pha workspace đạt (process phục vụ được); trạng thái bot ở `account_states`."""

    workspace_id: UUID
    ready: bool
    database_ready: bool
    error_code: str | None
    account_states: Mapping[UUID, PaperAccountRuntimeState]
    active_accounts: Mapping[UUID, PaperRuntimeAccount]
    owner_id: str | None = None
    bot_ids: Mapping[UUID, UUID] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class BotBootstrapOutcome:
    """Kết quả pha bot; `account` chỉ có khi READY (gateway đã công bố sau commit)."""

    account_id: UUID
    bot_id: UUID | None
    status: PaperAccountRuntimeStatus
    reason_code: str | None = None
    account: PaperRuntimeAccount | None = None


class _WorkspaceBootstrapFailure(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class _BotBootstrapFailure(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class _TransientBotFailure(RuntimeError):
    """Lock/statement timeout trong pha bot; mang `bot_id` để supervisor thử lại."""

    def __init__(self, bot_id: UUID | None) -> None:
        super().__init__(TRANSIENT_DATABASE_TIMEOUT)
        self.bot_id = bot_id


@dataclass(frozen=True, slots=True)
class _WorkspaceContext:
    published: PublishedWorkspaceSettings
    profile: ResolvedPaperExecutionProfile


async def load_bootstrap_bot_config(
    *, uow: Any, account_id: UUID, published: PublishedWorkspaceSettings, as_of: datetime
) -> Any:
    """Resolve config hiệu lực của bot (None = root-fallback khi chưa publish)."""
    return await load_effective_bot_config(
        uow.session,
        workspace_id=uow.workspace_id,
        account_id=account_id,
        as_of=as_of,
        published=published,
    )


class PaperRuntimeBootstrapService:
    """Bootstrap mọi bot ENABLED của đúng một deployment workspace, cô lập lỗi theo bot."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession] | None,
        workspace_id: UUID,
        owner_id: str | None = None,
        lease_ttl: timedelta = DEFAULT_LEASE_TTL,
        profile_catalog: PaperExecutionProfileCatalog | None = None,
        clock: Callable[[], datetime] | None = None,
        unit_of_work_factory: Callable[..., Any] = UnitOfWork,
        account_repository_factory: Callable[[Any], Any] = PaperRuntimeAccountRepository,
        settings_repository_factory: Callable[[Any], Any] = WorkspaceSettingsRepository,
        gateway_factory: Callable[..., PaperExecutionGateway] = PaperExecutionGateway,
        coordinator_factory: Callable[..., PaperLifecycleCoordinator] = (PaperLifecycleCoordinator),
        lease_factory: Callable[[Any], Any] = BotRuntimeLease,
        state_repository_factory: Callable[[Any], Any] = BotRuntimeStateRepository,
        bot_config_loader: Callable[..., Awaitable[Any]] = load_bootstrap_bot_config,
    ) -> None:
        if lease_ttl <= timedelta(0) or lease_ttl > MAX_LEASE_TTL:
            raise RuntimeControlError("LEASE_TTL_INVALID")
        self._sessions = sessions
        self._workspace_id = workspace_id
        self._owner_id = owner_id or new_runtime_owner_id()
        self._lease_ttl = lease_ttl
        self._profile_catalog = profile_catalog or default_paper_execution_profile_catalog()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._unit_of_work_factory = unit_of_work_factory
        self._account_repository_factory = account_repository_factory
        self._settings_repository_factory = settings_repository_factory
        self._gateway_factory = gateway_factory
        self._coordinator_factory = coordinator_factory
        self._lease_factory = lease_factory
        self._state_repository_factory = state_repository_factory
        self._bot_config_loader = bot_config_loader
        self._workspace: _WorkspaceContext | None = None
        self._listed_account_ids: list[UUID] = []
        self._last_bot_id: UUID | None = None

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def lease_ttl(self) -> timedelta:
        return self._lease_ttl

    async def bootstrap(self) -> PaperRuntimeBootstrapResult:
        """Pha workspace rồi pha bot; gateway chỉ công bố sau commit hydrate của chính bot."""
        if self._sessions is None:
            return self._result(
                ready=False, database_ready=False, error_code="DATABASE_UNAVAILABLE"
            )
        database_ready = False
        self._listed_account_ids = []
        try:
            accounts, workspace = await self._workspace_phase()
            database_ready = True
        except _WorkspaceBootstrapFailure as error:
            database_ready = error.reason_code != "DATABASE_UNAVAILABLE"
            return self._workspace_failed(
                list(self._listed_account_ids), error.reason_code, database_ready
            )
        except Exception as error:  # DB/hạ tầng: không lộ chi tiết
            if is_transient_database_error(error):
                # Fix round 2: chờ L1/statement quá hạn -> caller (app) thử lại có backoff.
                logger.warning(
                    "Pha workspace PAPER runtime timeout tạm thời; database_sqlstate=%s",
                    database_sqlstate(error),
                )
                return self._workspace_failed(
                    list(self._listed_account_ids), TRANSIENT_DATABASE_TIMEOUT, True
                )
            logger.error(
                "Lỗi pha workspace PAPER runtime; exception_type=%s database_sqlstate=%s",
                type(error).__name__,
                database_sqlstate(error),
            )
            return self._workspace_failed(
                list(self._listed_account_ids), "PAPER_BOOTSTRAP_FAILED", database_ready
            )

        self._workspace = workspace
        states: dict[UUID, PaperAccountRuntimeState] = {}
        active: dict[UUID, PaperRuntimeAccount] = {}
        bot_ids: dict[UUID, UUID] = {}
        for account in accounts:
            outcome = await self._bot_phase(account, workspace)
            states[account.id] = PaperAccountRuntimeState(outcome.status, outcome.reason_code)
            if outcome.bot_id is not None:
                bot_ids[account.id] = outcome.bot_id
            if outcome.account is not None:
                active[account.id] = outcome.account
        return PaperRuntimeBootstrapResult(
            workspace_id=self._workspace_id,
            ready=True,
            database_ready=True,
            error_code=None,
            account_states=MappingProxyType(states),
            active_accounts=MappingProxyType(active),
            owner_id=self._owner_id,
            bot_ids=MappingProxyType(bot_ids),
        )

    async def bootstrap_bot(self, account_id: UUID) -> BotBootstrapOutcome:
        """Pha bot cho một account (supervisor dùng khi re-acquire lease sau STANDBY)."""
        if self._sessions is None or self._workspace is None:
            return BotBootstrapOutcome(
                account_id, None, PaperAccountRuntimeStatus.STANDBY, "BOOTSTRAP_NOT_RUN"
            )
        async with self._unit_of_work_factory(
            self._sessions, workspace_id=self._workspace_id
        ) as uow:
            accounts = await self._account_repository_factory(uow.session).list_enabled(
                workspace_id=self._workspace_id
            )
        account = next((item for item in accounts if item.id == account_id), None)
        if account is None:
            return BotBootstrapOutcome(
                account_id, None, PaperAccountRuntimeStatus.STANDBY, "BOT_NOT_ENABLED"
            )
        return await self._bot_phase(account, self._workspace)

    # --- pha workspace -------------------------------------------------------------------
    async def _workspace_phase(self) -> tuple[list[Any], _WorkspaceContext]:
        async with self._unit_of_work_factory(
            self._sessions, workspace_id=self._workspace_id
        ) as uow:
            settings_repository = self._settings_repository_factory(uow.session)
            await settings_repository.lock_workspace(self._workspace_id)
            accounts = await self._account_repository_factory(uow.session).list_enabled(
                workspace_id=self._workspace_id
            )
            accounts = sorted(
                (account for account in accounts if account.workspace_id == self._workspace_id),
                key=lambda account: str(account.id),
            )
            self._listed_account_ids = [account.id for account in accounts]
            if not accounts:
                raise _WorkspaceBootstrapFailure("NO_ENABLED_PAPER_ACCOUNTS")
            try:
                published = await settings_repository.load_effective_config(
                    workspace_id=self._workspace_id, as_of=self._clock()
                )
            except SettingsVersionConflict:
                raise _WorkspaceBootstrapFailure("SETTINGS_INVALID") from None
            if published is None:
                raise _WorkspaceBootstrapFailure("SETTINGS_NOT_EFFECTIVE")
            try:
                profile = resolve_paper_execution_profile(published, self._profile_catalog)
            except ExecutionProfileResolutionError:
                raise _WorkspaceBootstrapFailure("EXECUTION_PROFILE_UNAVAILABLE") from None
            if profile.workspace_id != self._workspace_id:
                raise _WorkspaceBootstrapFailure("SETTINGS_WORKSPACE_MISMATCH")
        return accounts, _WorkspaceContext(published, profile)

    # --- pha bot -------------------------------------------------------------------------
    async def _bot_phase(self, account: Any, workspace: _WorkspaceContext) -> BotBootstrapOutcome:
        bot_id: UUID | None = None
        try:
            return await self._hydrate_bot(account, workspace)
        except _TransientBotFailure as error:
            return self._transient_standby(account.id, error.bot_id)
        except _BotBootstrapFailure as error:
            reason = error.reason_code
        except RuntimeControlError as error:
            if error.reason_code == "LEASE_HELD_ELSEWHERE":
                return BotBootstrapOutcome(
                    account.id,
                    error_bot_id(error),
                    PaperAccountRuntimeStatus.STANDBY,
                    error.reason_code,
                )
            reason = error.reason_code
        except Exception as error:
            if is_transient_database_error(error):
                return self._transient_standby(account.id, bot_id)
            logger.error(
                "Lỗi pha bot PAPER runtime; exception_type=%s database_sqlstate=%s",
                type(error).__name__,
                database_sqlstate(error),
            )
            reason = "PAPER_BOOTSTRAP_FAILED"
        return await self._record_recovery_required(account, reason, bot_id)

    def _transient_standby(self, account_id: UUID, bot_id: UUID | None) -> BotBootstrapOutcome:
        """Timeout tạm thời: STANDBY (không lease, không ghi state) để supervisor thử lại.

        Không bao giờ ghi RECOVERY_REQUIRED cho lock/statement timeout (fix round 2): đó không
        phải lỗi dữ liệu, và RECOVERY_REQUIRED không được supervisor thử lại.
        """
        logger.warning("Pha bot PAPER runtime timeout tạm thời; sẽ thử lại ở tick sau.")
        return BotBootstrapOutcome(
            account_id, bot_id, PaperAccountRuntimeStatus.STANDBY, TRANSIENT_DATABASE_TIMEOUT
        )

    async def _hydrate_bot(self, account: Any, workspace: _WorkspaceContext) -> BotBootstrapOutcome:
        # Pha bot chạy tuần tự (bootstrap hoặc supervisor giữ `_tick_lock`), nên ghi bot_id vừa
        # bind vào instance là an toàn; cần để STANDBY tạm thời vẫn mang bot_id cho supervisor.
        self._last_bot_id = None
        try:
            return await self._hydrate_bot_in_uow(account, workspace)
        except Exception as error:
            if is_transient_database_error(error):
                raise _TransientBotFailure(self._last_bot_id) from error
            raise

    async def _hydrate_bot_in_uow(
        self, account: Any, workspace: _WorkspaceContext
    ) -> BotBootstrapOutcome:
        async with self._unit_of_work_factory(
            self._sessions, workspace_id=self._workspace_id
        ) as uow:
            bot_id = await uow.bind_account(account.id)
            self._last_bot_id = bot_id
            if (
                account.workspace_id != self._workspace_id
                or account.mode != "PAPER"
                or account.status != "ENABLED"
            ):
                raise _BotBootstrapFailure("ACCOUNT_SCOPE_MISMATCH")
            seed = await self._account_repository_factory(uow.session).load_seed(
                workspace_id=self._workspace_id, account_id=account.id
            )
            if seed is None or seed.quote_asset != workspace.profile.quote_asset:
                raise _BotBootstrapFailure("ACCOUNT_SEED_MISMATCH")
            try:
                bot_config = await self._bot_config_loader(
                    uow=uow,
                    account_id=account.id,
                    published=workspace.published,
                    as_of=self._clock(),
                )
            except BotConfigResolutionError:
                raise _BotBootstrapFailure("BOT_CONFIG_NOT_EFFECTIVE") from None
            grant = await self._acquire_lease(uow, bot_id)
            gateway = self._gateway_factory(workspace.profile.fill_policy)
            coordinator = self._coordinator_factory(
                gateway, PaperLedgerRepository(), PaperCheckpointRepository()
            )
            try:
                await coordinator.hydrate_account(uow, account_id=account.id)
            except PaperLifecycleConflict:
                raise _BotBootstrapFailure("PAPER_RECOVERY_CONFLICT") from None
            await self._state_repository_factory(uow.session).upsert(
                fence=grant.fence,
                workspace_id=self._workspace_id,
                bot_id=bot_id,
                account_id=account.id,
                readiness=BotReadiness.READY,
                reason_code=None,
                heartbeat_at=grant.granted_at,
            )
            uow.record_audit(
                action="bot.runtime.lease_acquired",
                target_type="bot_instance",
                target_id=bot_id,
                reason="BOOTSTRAP",
                after_state={"fencing_token": int(grant.fencing_token)},
            )
            runtime = PaperRuntimeAccount(
                account_id=account.id,
                bot_id=bot_id,
                gateway=gateway,
                coordinator=coordinator,
                execution_profile=workspace.profile,
                owner_id=grant.owner_id,
                fencing_token=grant.fencing_token,
                bot_config_version_id=getattr(bot_config, "bot_config_version_id", None),
            )
        return BotBootstrapOutcome(
            account.id, bot_id, PaperAccountRuntimeStatus.READY, None, runtime
        )

    async def _acquire_lease(self, uow: Any, bot_id: UUID) -> LeaseGrant:
        lease = self._lease_factory(uow.session)
        now = await lease.database_now()
        try:
            grant: LeaseGrant = await lease.acquire_grant(
                self._workspace_id, bot_id, self._owner_id, now + self._lease_ttl
            )
        except RuntimeControlError as error:
            error.bot_id = bot_id  # type: ignore[attr-defined]
            raise
        return grant

    async def _record_recovery_required(
        self, account: Any, reason: str, bot_id: UUID | None
    ) -> BotBootstrapOutcome:
        """Giữ lease cho bot lỗi để ghi state + audit có fence; không lấy được lease -> STANDBY."""
        try:
            async with self._unit_of_work_factory(
                self._sessions, workspace_id=self._workspace_id
            ) as uow:
                bot_id = await uow.bind_account(account.id)
                grant = await self._acquire_lease(uow, bot_id)
                await self._state_repository_factory(uow.session).upsert(
                    fence=grant.fence,
                    workspace_id=self._workspace_id,
                    bot_id=bot_id,
                    account_id=account.id,
                    readiness=BotReadiness.RECOVERY_REQUIRED,
                    reason_code=reason,
                    heartbeat_at=grant.granted_at,
                )
                uow.record_audit(
                    action="bot.runtime.recovery_required",
                    target_type="bot_instance",
                    target_id=bot_id,
                    reason=reason,
                )
        except RuntimeControlError as error:
            if error.reason_code == "LEASE_HELD_ELSEWHERE":
                return BotBootstrapOutcome(
                    account.id, bot_id, PaperAccountRuntimeStatus.STANDBY, error.reason_code
                )
            logger.error("Không ghi được RECOVERY_REQUIRED; reason_code=%s", error.reason_code)
        except Exception as error:
            if is_transient_database_error(error):
                return self._transient_standby(account.id, bot_id)
            logger.error(
                "Không ghi được RECOVERY_REQUIRED; exception_type=%s", type(error).__name__
            )
        return BotBootstrapOutcome(
            account.id, bot_id, PaperAccountRuntimeStatus.RECOVERY_REQUIRED, reason
        )

    # --- kết quả -------------------------------------------------------------------------
    def _result(
        self, *, ready: bool, database_ready: bool, error_code: str | None
    ) -> PaperRuntimeBootstrapResult:
        return PaperRuntimeBootstrapResult(
            workspace_id=self._workspace_id,
            ready=ready,
            database_ready=database_ready,
            error_code=error_code,
            account_states=MappingProxyType({}),
            active_accounts=MappingProxyType({}),
            owner_id=self._owner_id,
        )

    def _workspace_failed(
        self, account_ids: list[UUID], reason_code: str, database_ready: bool
    ) -> PaperRuntimeBootstrapResult:
        states = {
            account_id: PaperAccountRuntimeState(
                PaperAccountRuntimeStatus.BLOCKED_BY_WORKSPACE, reason_code
            )
            for account_id in account_ids
        }
        return PaperRuntimeBootstrapResult(
            workspace_id=self._workspace_id,
            ready=False,
            database_ready=database_ready,
            error_code=reason_code,
            account_states=MappingProxyType(states),
            active_accounts=MappingProxyType({}),
            owner_id=self._owner_id,
        )


def error_bot_id(error: BaseException) -> UUID | None:
    value = getattr(error, "bot_id", None)
    return value if isinstance(value, UUID) else None
