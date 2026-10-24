"""Cấp/thu hồi quyền bot, giữ lịch sử và trần quyền theo role."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import BotGrant, BotInstance, UserMembership
from trading_bot.auth.bot_policy import (
    WORKSPACE_BOT_ROLES,
    BotPermission,
    role_allows_bot_permission,
)
from trading_bot.auth.policy import Principal, Role


class BotGrantRepository:
    """Application authorization bổ sung cho row-scope RLS."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def require(
        self, principal: Principal, *, bot_id: UUID, permission: BotPermission
    ) -> None:
        permission = BotPermission(permission)
        if not role_allows_bot_permission(principal.roles, permission):
            raise PermissionError("Role không cho phép thao tác bot này.")
        # Chỉ bỏ qua grant khi chính role workspace-wide cho phép thao tác này;
        # quyền của role theo bot (operator) không được mượn phạm vi workspace.
        if role_allows_bot_permission(principal.roles & WORKSPACE_BOT_ROLES, permission):
            return
        grant = await self._active(
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            membership_id=principal.membership_id,
            permission=permission,
        )
        if grant is None:
            raise PermissionError("Thiếu grant đang hiệu lực cho bot và thao tác này.")

    async def grant(
        self, principal: Principal, *, bot_id: UUID, membership_id: UUID, permission: BotPermission
    ) -> BotGrant:
        """Chỉ owner cấp quyền; thành viên và bot phải cùng workspace."""
        self._require_owner(principal)
        permission = BotPermission(permission)
        bot = await self._session.scalar(
            select(BotInstance).where(
                BotInstance.workspace_id == principal.workspace_id, BotInstance.id == bot_id
            )
        )
        member = await self._session.scalar(
            select(UserMembership).where(
                UserMembership.workspace_id == principal.workspace_id,
                UserMembership.id == membership_id,
                UserMembership.status == "ACTIVE",
            )
        )
        if bot is None or member is None:
            raise PermissionError("Bot hoặc thành viên không thuộc workspace đang hoạt động.")
        # Khóa membership để tuần tự hóa grant/revoke cho cùng người nhận.
        await self._session.scalar(
            select(UserMembership)
            .where(
                UserMembership.workspace_id == principal.workspace_id,
                UserMembership.id == membership_id,
            )
            .with_for_update()
        )
        existing = await self._active(
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            membership_id=membership_id,
            permission=permission,
        )
        if existing is not None:
            return existing
        row = BotGrant(
            id=uuid4(),
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            membership_id=membership_id,
            permission=permission.value,
            granted_by=principal.membership_id,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def revoke(
        self, principal: Principal, *, bot_id: UUID, membership_id: UUID, permission: BotPermission
    ) -> bool:
        self._require_owner(principal)
        row = await self._active(
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            membership_id=membership_id,
            permission=BotPermission(permission),
            lock=True,
        )
        if row is None:
            return False
        row.revoked_at = datetime.now(UTC)
        await self._session.flush()
        return True

    async def _active(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        membership_id: UUID,
        permission: BotPermission,
        lock: bool = False,
    ) -> BotGrant | None:
        query = select(BotGrant).where(
            BotGrant.workspace_id == workspace_id,
            BotGrant.bot_id == bot_id,
            BotGrant.membership_id == membership_id,
            BotGrant.permission == permission.value,
            BotGrant.revoked_at.is_(None),
        )
        return await self._session.scalar(query.with_for_update() if lock else query)

    @staticmethod
    def _require_owner(principal: Principal) -> None:
        if Role.OWNER not in principal.roles:
            raise PermissionError("Chỉ owner được cấp hoặc thu hồi grant bot.")
