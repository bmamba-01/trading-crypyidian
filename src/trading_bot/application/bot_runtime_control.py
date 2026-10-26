"""Service điều khiển runtime có kiểm quyền: pause/resume bot, kill/release workspace (MB05.6).

Ruling 20/21/25/30: pause bot cần `bot:runtime:pause`, resume cần `bot:runtime:resume` + MFA gần
đây + lý do + `expected_revision`; kill/release workspace cần `workspace:kill` (owner/risk_admin),
release thêm MFA + lý do + `expected_revision`. Bot không thấy được hoặc không có grant trả
`BOT_NOT_FOUND` như nhau (SEC-19). Mọi kết quả — đổi trạng thái, noop, bị từ chối — đều có audit
với actor, lý do và correlation (request_id); audit không chứa token/secret. Mutation đi qua
`BotRuntimeControlRepository` (lấy L1 rồi chỉ INSERT revision mới). Service này chạy trong
admin-web (MB-07 gắn route); runtime chỉ đọc control và áp hệ quả có fencing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models.bot_entities import BotInstance
from trading_bot.adapters.database.repositories.bot_grants import BotGrantRepository
from trading_bot.adapters.database.repositories.bot_runtime_controls import (
    BotRuntimeControlRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Permission, Principal, authorize
from trading_bot.auth.service import RECENT_MFA_LIFETIME
from trading_bot.domain.execution.runtime_control import (
    BotControlResult,
    BotEntryState,
    RuntimeControlError,
    WorkspaceControlResult,
    WorkspaceKillState,
)


class BotRuntimeControlError(RuntimeError):
    """Từ chối thao tác điều khiển với reason code an toàn (không lộ dữ liệu bot khác)."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


async def bot_is_visible(session: AsyncSession, *, workspace_id: UUID, bot_id: UUID) -> bool:
    """Bot thấy được dưới RLS admin-web của principal (workspace + role/grant)."""
    found = await session.scalar(
        select(BotInstance.id).where(
            BotInstance.workspace_id == workspace_id, BotInstance.id == bot_id
        )
    )
    return found is not None


class BotRuntimeControlService:
    """Factory theo principal; không giữ state giữa các request."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession] | None,
        *,
        clock: Callable[[], datetime] | None = None,
        unit_of_work_factory: Callable[..., Any] = UnitOfWork,
        control_repository_factory: Callable[[Any], Any] = BotRuntimeControlRepository,
        grant_repository_factory: Callable[[Any], Any] = BotGrantRepository,
        bot_visibility: Callable[..., Awaitable[bool]] = bot_is_visible,
    ) -> None:
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._unit_of_work_factory = unit_of_work_factory
        self._control_repository_factory = control_repository_factory
        self._grant_repository_factory = grant_repository_factory
        self._bot_visibility = bot_visibility

    def for_principal(self, principal: Principal) -> PrincipalRuntimeControl:
        return PrincipalRuntimeControl(self, principal)


class PrincipalRuntimeControl:
    """Thao tác điều khiển của một principal đã xác thực."""

    def __init__(self, service: BotRuntimeControlService, principal: Principal) -> None:
        self._service = service
        self._principal = principal

    # --- bot -----------------------------------------------------------------------------
    async def pause_bot(self, bot_id: UUID, reason: str) -> BotControlResult:
        """Giảm rủi ro: ma sát thấp (không MFA), idempotent theo trạng thái đích."""
        return await self._bot_change(
            bot_id,
            target=BotEntryState.PAUSED,
            permission=BotPermission.RUNTIME_PAUSE,
            reason=reason,
            expected_revision=None,
            require_mfa=False,
            action="bot.runtime.paused",
            reason_code="OPERATOR_PAUSE",
        )

    async def resume_bot(
        self, bot_id: UUID, reason: str, expected_revision: int | None
    ) -> BotControlResult:
        """Tăng rủi ro: quyền resume riêng, MFA gần đây, lý do, `expected_revision`."""
        return await self._bot_change(
            bot_id,
            target=BotEntryState.ENABLED,
            permission=BotPermission.RUNTIME_RESUME,
            reason=reason,
            expected_revision=expected_revision,
            require_mfa=True,
            action="bot.runtime.resumed",
            reason_code="OPERATOR_RESUME",
        )

    # --- workspace -----------------------------------------------------------------------
    async def pause_workspace(self, reason: str) -> WorkspaceControlResult:
        """Kill switch workspace: chỉ owner/risk_admin, không cần MFA (giảm rủi ro)."""
        return await self._workspace_change(
            target=WorkspaceKillState.KILLED,
            reason=reason,
            expected_revision=None,
            require_mfa=False,
            action="workspace.runtime.killed",
            reason_code="WORKSPACE_KILL",
        )

    async def release_workspace(
        self, reason: str, expected_revision: int | None
    ) -> WorkspaceControlResult:
        """Release kill: owner/risk_admin + MFA gần đây + lý do + `expected_revision`."""
        return await self._workspace_change(
            target=WorkspaceKillState.ACTIVE,
            reason=reason,
            expected_revision=expected_revision,
            require_mfa=True,
            action="workspace.runtime.released",
            reason_code="WORKSPACE_RELEASE",
        )

    # --- nội bộ --------------------------------------------------------------------------
    def _uow(self, correlation_id: UUID) -> Any:
        return self._service._unit_of_work_factory(
            self._service._sessions,
            workspace_id=self._principal.workspace_id,
            actor_membership_id=self._principal.membership_id,
            request_id=correlation_id,
        )

    def _require_recent_mfa(self) -> None:
        now = self._service._clock()
        verified = self._principal.mfa_verified_at
        if verified > now or now - verified > RECENT_MFA_LIFETIME:
            raise BotRuntimeControlError("RECENT_MFA_REQUIRED")

    @staticmethod
    def _require_reason(reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise BotRuntimeControlError("CONTROL_REASON_REQUIRED")

    async def _bot_change(
        self,
        bot_id: UUID,
        *,
        target: BotEntryState,
        permission: BotPermission,
        reason: str,
        expected_revision: int | None,
        require_mfa: bool,
        action: str,
        reason_code: str,
    ) -> BotControlResult:
        correlation_id = uuid4()
        principal = self._principal
        try:
            async with self._uow(correlation_id) as uow:
                # Visibility/grant trước mọi kế hoạch noop (M5, SEC-19).
                visible = await self._service._bot_visibility(
                    uow.session, workspace_id=principal.workspace_id, bot_id=bot_id
                )
                if not visible:
                    raise BotRuntimeControlError("BOT_NOT_FOUND")
                try:
                    await self._service._grant_repository_factory(uow.session).require(
                        principal, bot_id=bot_id, permission=permission
                    )
                except PermissionError:
                    raise BotRuntimeControlError("BOT_NOT_FOUND") from None
                if require_mfa:
                    self._require_recent_mfa()
                self._require_reason(reason)
                result: BotControlResult = await self._service._control_repository_factory(
                    uow.session
                ).append_bot_control(
                    workspace_id=principal.workspace_id,
                    bot_id=bot_id,
                    target=target,
                    expected_revision=expected_revision,
                    changed_by=principal.membership_id,
                    reason_code=reason_code,
                    reason_text=reason,
                    correlation_id=correlation_id,
                )
                uow.record_audit(
                    action=action if result.changed else f"{action}.noop",
                    target_type="bot_instance",
                    target_id=bot_id,
                    reason=reason,
                    after_state={
                        "entry_state": result.entry_state.value,
                        "revision": result.revision,
                    },
                )
                return result
        except (BotRuntimeControlError, RuntimeControlError) as error:
            await self._audit_denied(bot_id, "bot_instance", error.reason_code, correlation_id)
            raise BotRuntimeControlError(error.reason_code) from None

    async def _workspace_change(
        self,
        *,
        target: WorkspaceKillState,
        reason: str,
        expected_revision: int | None,
        require_mfa: bool,
        action: str,
        reason_code: str,
    ) -> WorkspaceControlResult:
        correlation_id = uuid4()
        principal = self._principal
        try:
            if not authorize(principal, Permission.WORKSPACE_KILL):
                raise BotRuntimeControlError("CONTROL_PERMISSION_DENIED")
            if require_mfa:
                self._require_recent_mfa()
            self._require_reason(reason)
            async with self._uow(correlation_id) as uow:
                result: WorkspaceControlResult = await self._service._control_repository_factory(
                    uow.session
                ).append_workspace_control(
                    workspace_id=principal.workspace_id,
                    target=target,
                    expected_revision=expected_revision,
                    changed_by=principal.membership_id,
                    reason_code=reason_code,
                    reason_text=reason,
                    correlation_id=correlation_id,
                )
                uow.record_audit(
                    action=action if result.changed else f"{action}.noop",
                    target_type="workspace",
                    target_id=principal.workspace_id,
                    reason=reason,
                    after_state={
                        "kill_state": result.kill_state.value,
                        "revision": result.revision,
                    },
                )
                return result
        except (BotRuntimeControlError, RuntimeControlError) as error:
            await self._audit_denied(
                principal.workspace_id, "workspace", error.reason_code, correlation_id
            )
            raise BotRuntimeControlError(error.reason_code) from None

    async def _audit_denied(
        self, target_id: UUID, target_type: str, reason_code: str, correlation_id: UUID
    ) -> None:
        """Audit cho thao tác bị từ chối ghi ở transaction riêng (transaction chính đã rollback)."""
        async with self._uow(correlation_id) as uow:
            uow.record_audit(
                action="bot.runtime.control_denied",
                target_type=target_type,
                target_id=target_id,
                reason=reason_code,
            )
