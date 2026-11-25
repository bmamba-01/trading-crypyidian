"""Guard "phải phẳng mới được disable" cho bot/account (MB05.6, ruling 29).

Lấy L1 `workspace-settings` trước khi đếm, cùng thứ tự khóa với đường entry/fill.

MB-07 gọi `ensure_flat_for_disable` trong cùng transaction với đường disable (và thêm grant cột
`status` cùng code đó, SEC R10). Không có chế độ disable chỉ-thoát: người vận hành pause bot
(exits bảo vệ vẫn chạy) cho tới khi phẳng. Chỉ đọc trong scope bot của transaction (RLS).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.repositories.admin_lifecycle_guard import (
    AdminLifecycleGuardRepository,
)
from trading_bot.adapters.database.repositories.workspace_settings import (
    WorkspaceSettingsRepository,
)

OPEN_ORDER_STATUSES = frozenset({"ACCEPTED", "PARTIALLY_FILLED", "UNKNOWN"})
HELD_RESERVATION_STATUSES = frozenset({"RESERVED", "UNKNOWN"})


class BotNotFlatError(RuntimeError):
    """`BOT_NOT_FLAT` kèm số đếm của chính bot đó; `BOT_NOT_FOUND` khi không thấy bot."""

    def __init__(
        self,
        reason_code: str,
        *,
        open_positions: int = 0,
        open_orders: int = 0,
        held_reservations: int = 0,
    ) -> None:
        super().__init__(
            f"{reason_code}: positions={open_positions} orders={open_orders} "
            f"reservations={held_reservations}"
        )
        self.reason_code = reason_code
        self.open_positions = open_positions
        self.open_orders = open_orders
        self.held_reservations = held_reservations


class BotLifecycleGuard:
    """Kiểm bot phẳng (không vị thế mở, lệnh chưa khớp, reservation giữ budget)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_workspace_for_enable(self, workspace_id: UUID) -> None:
        """Giữ L1 theo cùng thứ tự khóa entry/disable trước khi xét enable."""
        await WorkspaceSettingsRepository(self._session).lock_workspace(workspace_id)

    async def ensure_flat_for_disable(self, workspace_id: UUID, bot_id: UUID) -> None:
        # L1 trước khi đếm (fix round 1, M7): entry/fill đang chạy giữ L1 nên không thể mở vị
        # thế/reservation mới giữa lúc đếm và lúc đường disable commit.
        await WorkspaceSettingsRepository(self._session).lock_workspace(workspace_id)
        snapshot = await AdminLifecycleGuardRepository(self._session).load_snapshot(
            workspace_id=workspace_id,
            bot_id=bot_id,
        )
        if not snapshot.bot_exists:
            raise BotNotFlatError("BOT_NOT_FOUND")
        counts = (
            snapshot.open_positions,
            snapshot.open_orders,
            snapshot.held_reservations,
        )
        if any(counts):
            raise BotNotFlatError(
                "BOT_NOT_FLAT",
                open_positions=counts[0],
                open_orders=counts[1],
                held_reservations=counts[2],
            )
