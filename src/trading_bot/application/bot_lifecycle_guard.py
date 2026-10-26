"""Guard "phải phẳng mới được disable" cho bot/account (MB05.6, ruling 29).

Lấy L1 `workspace-settings` trước khi đếm, cùng thứ tự khóa với đường entry/fill.

MB-07 gọi `ensure_flat_for_disable` trong cùng transaction với đường disable (và thêm grant cột
`status` cùng code đó, SEC R10). Không có chế độ disable chỉ-thoát: người vận hành pause bot
(exits bảo vệ vẫn chạy) cho tới khi phẳng. Chỉ đọc trong scope bot của transaction (RLS).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.bot_entities import BotInstance
from trading_bot.adapters.database.models.paper_entities import OrderIntentRecord, TradeRecord
from trading_bot.adapters.database.models.risk_entities import RiskReservationRecord
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

    async def ensure_flat_for_disable(self, workspace_id: UUID, bot_id: UUID) -> None:
        # L1 trước khi đếm (fix round 1, M7): entry/fill đang chạy giữ L1 nên không thể mở vị
        # thế/reservation mới giữa lúc đếm và lúc đường disable commit.
        await WorkspaceSettingsRepository(self._session).lock_workspace(workspace_id)
        bot = await self._session.scalar(
            select(BotInstance.id).where(
                BotInstance.workspace_id == workspace_id, BotInstance.id == bot_id
            )
        )
        if bot is None:
            raise BotNotFlatError("BOT_NOT_FOUND")
        positions = await self._session.scalar(
            select(func.count())
            .select_from(TradeRecord)
            .where(
                TradeRecord.workspace_id == workspace_id,
                TradeRecord.bot_id == bot_id,
                TradeRecord.status == "OPEN",
            )
        )
        orders = await self._session.scalar(
            select(func.count())
            .select_from(OrderIntentRecord)
            .where(
                OrderIntentRecord.workspace_id == workspace_id,
                OrderIntentRecord.bot_id == bot_id,
                OrderIntentRecord.status.in_(sorted(OPEN_ORDER_STATUSES)),
            )
        )
        reservations = await self._session.scalar(
            select(func.count())
            .select_from(RiskReservationRecord)
            .where(
                RiskReservationRecord.workspace_id == workspace_id,
                RiskReservationRecord.bot_id == bot_id,
                RiskReservationRecord.status.in_(sorted(HELD_RESERVATION_STATUSES)),
            )
        )
        counts = (int(positions or 0), int(orders or 0), int(reservations or 0))
        if any(counts):
            raise BotNotFlatError(
                "BOT_NOT_FLAT",
                open_positions=counts[0],
                open_orders=counts[1],
                held_reservations=counts[2],
            )
