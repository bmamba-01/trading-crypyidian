"""Khởi tạo và hydrate PAPER account trước khi runtime trở nên sẵn sàng."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from trading_bot.domain.execution.paper import PaperExecutionGateway
from trading_bot.domain.execution.runtime_control import (
    FencingToken,
    RuntimeControlError,
    RuntimeFence,
)


class PaperAccountRuntimeStatus(StrEnum):
    READY = "READY"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    BLOCKED_BY_WORKSPACE = "BLOCKED_BY_WORKSPACE"


@dataclass(frozen=True, slots=True)
class PaperAccountRuntimeState:
    status: PaperAccountRuntimeStatus
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class PaperRuntimeAccount:
    """Account đã hydrate; `owner_id`/`fencing_token` bất biến, re-acquire thì thay instance.

    Cả hai là None trong chế độ chuyển tiếp trước registry MB05.5 (bot chưa từng có lease).
    """

    account_id: UUID
    bot_id: UUID
    gateway: PaperExecutionGateway
    coordinator: PaperLifecycleCoordinator
    execution_profile: ResolvedPaperExecutionProfile
    owner_id: str | None = None
    fencing_token: FencingToken | None = None

    def __post_init__(self) -> None:
        if (self.owner_id is None) != (self.fencing_token is None):
            raise RuntimeControlError("RUNTIME_FENCE_INCOMPLETE")

    @property
    def fence(self) -> RuntimeFence | None:
        """Bằng chứng lease trình cho mọi command thay đổi state PAPER."""
        if self.owner_id is None or self.fencing_token is None:
            return None
        return RuntimeFence(self.owner_id, self.fencing_token)


@dataclass(frozen=True, slots=True)
class PaperRuntimeBootstrapResult:
    workspace_id: UUID
    ready: bool
    database_ready: bool
    error_code: str | None
    account_states: Mapping[UUID, PaperAccountRuntimeState]
    active_accounts: Mapping[UUID, PaperRuntimeAccount]


class _WorkspaceBootstrapFailure(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class _AccountBootstrapFailure(RuntimeError):
    def __init__(self, account_id: UUID, reason_code: str) -> None:
        super().__init__(reason_code)
        self.account_id = account_id
        self.reason_code = reason_code


class PaperRuntimeBootstrapService:
    """Bootstrap toàn bộ account PAPER của đúng một deployment workspace."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession] | None,
        workspace_id: UUID,
        profile_catalog: PaperExecutionProfileCatalog | None = None,
        clock: Callable[[], datetime] | None = None,
        unit_of_work_factory: Callable[..., Any] = UnitOfWork,
        account_repository_factory: Callable[[Any], Any] = PaperRuntimeAccountRepository,
        settings_repository_factory: Callable[[Any], Any] = WorkspaceSettingsRepository,
        gateway_factory: Callable[..., PaperExecutionGateway] = PaperExecutionGateway,
        coordinator_factory: Callable[..., PaperLifecycleCoordinator] = (PaperLifecycleCoordinator),
    ) -> None:
        self._sessions = sessions
        self._workspace_id = workspace_id
        self._profile_catalog = profile_catalog or default_paper_execution_profile_catalog()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._unit_of_work_factory = unit_of_work_factory
        self._account_repository_factory = account_repository_factory
        self._settings_repository_factory = settings_repository_factory
        self._gateway_factory = gateway_factory
        self._coordinator_factory = coordinator_factory

    async def bootstrap(self) -> PaperRuntimeBootstrapResult:
        """Chỉ công bố gateway sau khi toàn bộ account đã hydrate và commit."""
        if self._sessions is None:
            return _result(
                workspace_id=self._workspace_id,
                ready=False,
                database_ready=False,
                error_code="DATABASE_UNAVAILABLE",
            )

        account_ids: list[UUID] = []
        active_accounts: dict[UUID, PaperRuntimeAccount] = {}
        failing_account_id: UUID | None = None
        database_ready = False
        try:
            async with self._unit_of_work_factory(
                self._sessions, workspace_id=self._workspace_id
            ) as uow:
                await self._settings_repository_factory(uow.session).lock_workspace(
                    self._workspace_id
                )
                account_repository = self._account_repository_factory(uow.session)
                accounts = await account_repository.list_enabled(workspace_id=self._workspace_id)
                accounts = sorted(accounts, key=lambda account: str(account.id))
                account_ids = [account.id for account in accounts]
                database_ready = True
                if not accounts:
                    raise _WorkspaceBootstrapFailure("NO_ENABLED_PAPER_ACCOUNTS")

                settings_repository = self._settings_repository_factory(uow.session)
                published = await settings_repository.load_effective_config(
                    workspace_id=self._workspace_id, as_of=self._clock()
                )
                if published is None:
                    raise _WorkspaceBootstrapFailure("SETTINGS_NOT_EFFECTIVE")
                profile = resolve_paper_execution_profile(published, self._profile_catalog)
                if profile.workspace_id != self._workspace_id:
                    raise _WorkspaceBootstrapFailure("SETTINGS_WORKSPACE_MISMATCH")

                for account in accounts:
                    failing_account_id = account.id
                    self._validate_account_scope(account)
                    bot_id = await uow.bind_account(account.id, allow_switch=True)
                    seed = await account_repository.load_seed(
                        workspace_id=self._workspace_id, account_id=account.id
                    )
                    if seed is None or seed.quote_asset != profile.quote_asset:
                        raise _AccountBootstrapFailure(account.id, "ACCOUNT_SEED_MISMATCH")

                    gateway = self._gateway_factory(profile.fill_policy)
                    coordinator = self._coordinator_factory(
                        gateway, PaperLedgerRepository(), PaperCheckpointRepository()
                    )
                    try:
                        await coordinator.hydrate_account(uow, account_id=account.id)
                    except PaperLifecycleConflict as error:
                        raise _AccountBootstrapFailure(
                            account.id, "PAPER_RECOVERY_CONFLICT"
                        ) from error
                    active_accounts[account.id] = PaperRuntimeAccount(
                        account_id=account.id,
                        bot_id=bot_id,
                        gateway=gateway,
                        coordinator=coordinator,
                        execution_profile=profile,
                    )
                failing_account_id = None

            return _result(
                workspace_id=self._workspace_id,
                ready=True,
                database_ready=database_ready,
                account_ids=account_ids,
                active_accounts=active_accounts,
            )
        except _AccountBootstrapFailure as error:
            return _failed_result(
                workspace_id=self._workspace_id,
                account_ids=account_ids,
                failed_account_id=error.account_id,
                reason_code=error.reason_code,
                database_ready=database_ready,
            )
        except _WorkspaceBootstrapFailure as error:
            return _failed_result(
                workspace_id=self._workspace_id,
                account_ids=account_ids,
                reason_code=error.reason_code,
                database_ready=database_ready,
            )
        except SettingsVersionConflict:
            return _failed_result(
                workspace_id=self._workspace_id,
                account_ids=account_ids,
                reason_code="SETTINGS_INVALID",
                database_ready=database_ready,
            )
        except ExecutionProfileResolutionError:
            return _failed_result(
                workspace_id=self._workspace_id,
                account_ids=account_ids,
                reason_code="EXECUTION_PROFILE_UNAVAILABLE",
                database_ready=database_ready,
            )
        except Exception as error:
            logging.getLogger(__name__).error(
                "Lỗi nội bộ khi khởi động PAPER runtime; exception_type=%s database_sqlstate=%s",
                type(error).__name__,
                getattr(getattr(error, "orig", None), "sqlstate", None),
            )
            return _failed_result(
                workspace_id=self._workspace_id,
                account_ids=account_ids,
                failed_account_id=failing_account_id,
                reason_code="PAPER_BOOTSTRAP_FAILED",
                database_ready=database_ready,
            )

    def _validate_account_scope(self, account: Any) -> None:
        if (
            account.workspace_id != self._workspace_id
            or account.mode != "PAPER"
            or account.status != "ENABLED"
        ):
            raise _AccountBootstrapFailure(account.id, "ACCOUNT_SCOPE_MISMATCH")


def _result(
    *,
    workspace_id: UUID,
    ready: bool,
    database_ready: bool,
    error_code: str | None = None,
    account_ids: list[UUID] | None = None,
    active_accounts: Mapping[UUID, PaperRuntimeAccount] | None = None,
) -> PaperRuntimeBootstrapResult:
    ids = account_ids or []
    runtimes = dict(active_accounts or {})
    states = (
        {
            account_id: PaperAccountRuntimeState(PaperAccountRuntimeStatus.READY)
            for account_id in ids
        }
        if ready
        else {}
    )
    return PaperRuntimeBootstrapResult(
        workspace_id=workspace_id,
        ready=ready,
        database_ready=database_ready,
        error_code=error_code,
        account_states=MappingProxyType(states),
        active_accounts=MappingProxyType(runtimes),
    )


def _failed_result(
    *,
    workspace_id: UUID,
    account_ids: list[UUID],
    reason_code: str,
    database_ready: bool,
    failed_account_id: UUID | None = None,
) -> PaperRuntimeBootstrapResult:
    states: dict[UUID, PaperAccountRuntimeState] = {}
    for account_id in account_ids:
        if failed_account_id is not None and account_id != failed_account_id:
            states[account_id] = PaperAccountRuntimeState(
                PaperAccountRuntimeStatus.BLOCKED_BY_WORKSPACE, reason_code
            )
        else:
            states[account_id] = PaperAccountRuntimeState(
                PaperAccountRuntimeStatus.RECOVERY_REQUIRED, reason_code
            )
    return PaperRuntimeBootstrapResult(
        workspace_id=workspace_id,
        ready=False,
        database_ready=database_ready,
        error_code=reason_code,
        account_states=MappingProxyType(states),
        active_accounts=MappingProxyType({}),
    )
