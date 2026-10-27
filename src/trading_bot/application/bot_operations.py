"""Service vận hành bot dùng chung cho Admin API và Telegram (MB-07a, ruling 31–34).

Một control plane: pause/resume bot và kill/release workspace ủy quyền nguyên vẹn cho
`BotRuntimeControlService` của MB-05 (quyền ruling 20/30, MFA gần đây, lý do,
`expected_revision`, L1, audit kể cả noop/từ chối). Service này thêm:

- đọc trạng thái bot/workspace: cần `bot:read`; bot ngoài scope RLS, khác workspace hoặc thiếu
  grant đều là `BOT_NOT_FOUND` (SEC-19);
- disable bot (ruling 29, 34): chỉ owner + MFA gần đây + lý do; trong **một** transaction: guard
  phẳng (L1 rồi đếm) → khóa account → `status = DISABLED` → audit. Bị từ chối thì audit ở
  transaction riêng (transaction chính đã rollback).

Kênh gọi (router HTTP, handler Telegram) không mở session DB; chỉ gọi các phương thức ở đây.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.bot_grants import BotGrantRepository
from trading_bot.adapters.database.repositories.bot_operations import (
    BotIdentityRow,
    BotOperationsRepository,
    BotRuntimeRow,
    WorkspaceControlRow,
)
from trading_bot.application.bot_lifecycle_guard import BotLifecycleGuard, BotNotFlatError
from trading_bot.application.bot_runtime_control import (
    BotRuntimeControlError,
    BotRuntimeControlService,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.service import RECENT_MFA_LIFETIME
from trading_bot.domain.execution.runtime_control import (
    BotControlResult,
    BotEntryState,
    WorkspaceControlResult,
    WorkspaceKillState,
)


@dataclass(frozen=True, slots=True)
class BotRuntimeStatus:
    """Trạng thái vận hành một bot; không chứa account/secret/lease token."""

    bot_id: UUID
    name: str
    status: str
    entry_state: BotEntryState
    control_revision: int
    readiness: str | None
    readiness_reason_code: str | None
    workspace_kill_state: WorkspaceKillState
    workspace_control_revision: int

    @property
    def entry_allowed(self) -> bool:
        """Gợi ý hiển thị; runtime vẫn đọc lại control trong transaction entry (ruling 27)."""
        return (
            self.status == "ENABLED"
            and self.entry_state is BotEntryState.ENABLED
            and self.workspace_kill_state is WorkspaceKillState.ACTIVE
            and self.readiness == "READY"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.bot_id),
            "name": self.name,
            "status": self.status,
            "entry_state": self.entry_state.value,
            "control_revision": self.control_revision,
            "readiness": self.readiness,
            "readiness_reason_code": self.readiness_reason_code,
            "workspace_kill_state": self.workspace_kill_state.value,
            "workspace_control_revision": self.workspace_control_revision,
            "entry_allowed": self.entry_allowed,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceRuntimeStatus:
    workspace_id: UUID
    kill_state: WorkspaceKillState
    revision: int
    bots: tuple[BotRuntimeStatus, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": str(self.workspace_id),
            "kill_state": self.kill_state.value,
            "revision": self.revision,
            "bots": [bot.as_dict() for bot in self.bots],
        }


@dataclass(frozen=True, slots=True)
class BotDisableResult:
    bot_id: UUID
    status: str
    changed: bool

    def as_dict(self) -> dict[str, Any]:
        return {"id": str(self.bot_id), "status": self.status, "changed": self.changed}


class BotOperationsService:
    """Factory theo principal; không giữ state giữa các request."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        control_service: BotRuntimeControlService | None = None,
        clock: Callable[[], datetime] | None = None,
        unit_of_work_factory: Callable[..., Any] = UnitOfWork,
        operations_repository_factory: Callable[[Any], Any] = BotOperationsRepository,
        grant_repository_factory: Callable[[Any], Any] = BotGrantRepository,
        lifecycle_guard_factory: Callable[[Any], Any] = BotLifecycleGuard,
    ) -> None:
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._control = control_service or BotRuntimeControlService(sessions, clock=self._clock)
        self._unit_of_work_factory = unit_of_work_factory
        self._operations_repository_factory = operations_repository_factory
        self._grant_repository_factory = grant_repository_factory
        self._lifecycle_guard_factory = lifecycle_guard_factory

    def for_principal(self, principal: Principal) -> PrincipalBotOperations:
        return PrincipalBotOperations(self, principal)


class PrincipalBotOperations:
    """Thao tác vận hành của một principal đã xác thực (web hoặc Telegram)."""

    def __init__(self, service: BotOperationsService, principal: Principal) -> None:
        self._service = service
        self._principal = principal
        self._control = service._control.for_principal(principal)

    # --- control: ủy quyền MB-05 -----------------------------------------------------------
    async def pause_bot(self, bot_id: UUID, reason: str) -> BotControlResult:
        return await self._control.pause_bot(bot_id, reason)

    async def resume_bot(
        self, bot_id: UUID, reason: str, expected_revision: int | None
    ) -> BotControlResult:
        return await self._control.resume_bot(bot_id, reason, expected_revision)

    async def kill_workspace(self, reason: str) -> WorkspaceControlResult:
        return await self._control.pause_workspace(reason)

    async def release_workspace(
        self, reason: str, expected_revision: int | None
    ) -> WorkspaceControlResult:
        return await self._control.release_workspace(reason, expected_revision)

    # --- đọc trạng thái ------------------------------------------------------------------
    async def list_bots(self) -> list[BotRuntimeStatus]:
        async with self._uow() as uow:
            repository = self._service._operations_repository_factory(uow.session)
            workspace = await repository.workspace_control(self._principal.workspace_id)
            result = []
            for row in await repository.list_bots(self._principal.workspace_id):
                if await self._can_read(uow, row.bot_id):
                    runtime = await repository.bot_runtime(self._principal.workspace_id, row.bot_id)
                    result.append(_status(row, runtime, workspace))
            return result

    async def bot_status(self, bot_id: UUID) -> BotRuntimeStatus:
        async with self._uow(bot_id) as uow:
            repository = self._service._operations_repository_factory(uow.session)
            row = await repository.get_bot(self._principal.workspace_id, bot_id)
            if row is None or not await self._can_read(uow, bot_id):
                raise BotRuntimeControlError("BOT_NOT_FOUND")
            runtime = await repository.bot_runtime(self._principal.workspace_id, bot_id)
            workspace = await repository.workspace_control(self._principal.workspace_id)
            return _status(row, runtime, workspace)

    async def workspace_status(self) -> WorkspaceRuntimeStatus:
        bots = await self.list_bots()
        async with self._uow() as uow:
            workspace = await self._service._operations_repository_factory(
                uow.session
            ).workspace_control(self._principal.workspace_id)
        return WorkspaceRuntimeStatus(
            self._principal.workspace_id, workspace.kill_state, workspace.revision, tuple(bots)
        )

    # --- disable (ruling 29, 34) ---------------------------------------------------------
    async def disable_bot(self, bot_id: UUID, reason: str) -> BotDisableResult:
        correlation_id = uuid4()
        principal = self._principal
        try:
            if Role.OWNER not in principal.roles:
                raise BotRuntimeControlError("CONTROL_PERMISSION_DENIED")
            self._require_recent_mfa()
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
                raise BotRuntimeControlError("CONTROL_REASON_REQUIRED")
            async with self._uow(bot_id, correlation_id) as uow:
                repository = self._service._operations_repository_factory(uow.session)
                row = await repository.get_bot(principal.workspace_id, bot_id)
                if row is None:
                    raise BotRuntimeControlError("BOT_NOT_FOUND")
                changed = False
                if row.status != "DISABLED":
                    # L1 rồi đếm; cùng transaction với lệnh ghi account (ruling 29).
                    await self._service._lifecycle_guard_factory(
                        uow.session
                    ).ensure_flat_for_disable(principal.workspace_id, bot_id)
                    changed = await repository.disable_account(
                        principal.workspace_id, row.account_id
                    )
                uow.record_audit(
                    action="bot.disabled" if changed else "bot.disabled.noop",
                    target_type="bot_instance",
                    target_id=bot_id,
                    reason=reason,
                    before_state={"status": row.status},
                    after_state={"status": "DISABLED"},
                )
                return BotDisableResult(bot_id, "DISABLED", changed)
        except (BotRuntimeControlError, BotNotFlatError) as error:
            await self._audit_disable_denied(bot_id, error.reason_code, correlation_id)
            raise
        except LookupError:
            await self._audit_disable_denied(bot_id, "BOT_NOT_FOUND", correlation_id)
            raise BotRuntimeControlError("BOT_NOT_FOUND") from None
        except DBAPIError:
            # Ví dụ chờ L1 quá `lock_timeout` (55P03, ruling 40): không treo, không lộ chi tiết DB.
            await self._audit_disable_denied(bot_id, "RUNTIME_CONTROL_UNAVAILABLE", correlation_id)
            raise BotRuntimeControlError("RUNTIME_CONTROL_UNAVAILABLE") from None

    # --- nội bộ --------------------------------------------------------------------------
    def _uow(self, bot_id: UUID | None = None, correlation_id: UUID | None = None) -> Any:
        return self._service._unit_of_work_factory(
            self._service._sessions,
            workspace_id=self._principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=self._principal.membership_id,
            request_id=correlation_id,
        )

    async def _can_read(self, uow: Any, bot_id: UUID) -> bool:
        try:
            await self._service._grant_repository_factory(uow.session).require(
                self._principal, bot_id=bot_id, permission=BotPermission.READ
            )
        except PermissionError:
            return False
        return True

    def _require_recent_mfa(self) -> None:
        now = self._service._clock()
        verified = self._principal.mfa_verified_at
        if verified > now or now - verified > RECENT_MFA_LIFETIME:
            raise BotRuntimeControlError("RECENT_MFA_REQUIRED")

    async def _audit_disable_denied(
        self, bot_id: UUID, reason_code: str, correlation_id: UUID
    ) -> None:
        async with self._uow(None, correlation_id) as uow:
            uow.record_audit(
                action="bot.lifecycle.disable_denied",
                target_type="bot_instance",
                target_id=bot_id,
                reason=reason_code,
            )


def _status(
    row: BotIdentityRow, runtime: BotRuntimeRow, workspace: WorkspaceControlRow
) -> BotRuntimeStatus:
    return BotRuntimeStatus(
        bot_id=row.bot_id,
        name=row.name,
        status=row.status,
        entry_state=runtime.entry_state,
        control_revision=runtime.control_revision,
        readiness=runtime.readiness,
        readiness_reason_code=runtime.readiness_reason_code,
        workspace_kill_state=workspace.kill_state,
        workspace_control_revision=workspace.revision,
    )
