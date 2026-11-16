"""TG-3: repository đọc ledger PAPER và ghi delivery kết quả/tổng hợp cho signal-worker.

Chạy dưới role ``crypidian_signal_worker``: chỉ ``SELECT`` theo cột trên ``trade``, ``fill``,
``position_event``, ``risk_reservation`` và ``bot_instance`` của đúng bot trong ``app.bot_id``
(migration d5f7b9c1e3a6). Mọi truy vấn chỉ liệt kê cột đã được cấp; không bao giờ ghi ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    BotInstance,
    FillRecord,
    PositionEventRecord,
    RiskReservationRecord,
    TelegramBinding,
    TelegramPaperReportDelivery,
    TelegramSignalSubscription,
    TelegramTradeNotification,
    TradeRecord,
    UserMembership,
)
from trading_bot.application.telegram_trade_results import (
    SUMMARY_TOTAL_WINDOW,
    BotSummaryRow,
    ClosedTradeFact,
    TradeFillFact,
    TradeNotificationKind,
    TradeResultSource,
    build_bot_summary_row,
)


@dataclass(frozen=True, slots=True)
class TradeEvent:
    """Một sự kiện mở/đóng trade PAPER mới phát hiện trong ledger."""

    trade_id: UUID
    kind: TradeNotificationKind
    symbol: str
    side: str


@dataclass(frozen=True, slots=True)
class TradeNotificationClaim:
    """Delivery đã nhận lease; ``source`` là dữ kiện ledger đọc trong cùng transaction."""

    notification_id: UUID
    bot_id: UUID
    subscription_id: UUID
    trade_id: UUID
    kind: TradeNotificationKind
    attempts: int
    chat_id: int
    source: TradeResultSource


@dataclass(frozen=True, slots=True)
class PaperReportClaim:
    """Delivery tin tổng hợp đã nhận lease cho một chat trong một cửa sổ."""

    report_id: UUID
    chat_id: int
    window_start: datetime
    window_end: datetime
    attempts: int


def _active_subscription_join(query: Any) -> Any:
    """Chỉ subscription bật, binding đã xác minh chưa thu hồi và membership ACTIVE."""
    return (
        query.join(
            TelegramBinding,
            and_(
                TelegramBinding.workspace_id == TelegramSignalSubscription.workspace_id,
                TelegramBinding.id == TelegramSignalSubscription.telegram_binding_id,
                TelegramBinding.membership_id == TelegramSignalSubscription.membership_id,
            ),
        )
        .join(
            UserMembership,
            and_(
                UserMembership.workspace_id == TelegramSignalSubscription.workspace_id,
                UserMembership.id == TelegramSignalSubscription.membership_id,
            ),
        )
        .where(
            TelegramSignalSubscription.enabled.is_(True),
            TelegramBinding.verified_at.is_not(None),
            TelegramBinding.revoked_at.is_(None),
            UserMembership.status == "ACTIVE",
        )
    )


class TelegramTradeResultRepository:
    """Truy vấn/ghi trong UoW có workspace scope (và bot scope cho dữ liệu theo bot)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- phát hiện sự kiện ---------------------------------------------------------------

    async def recent_trade_events(
        self, *, workspace_id: UUID, bot_id: UUID, since: datetime, limit: int = 100
    ) -> tuple[TradeEvent, ...]:
        """Trade mới mở (``trade.created_at``) và mới đóng (``position_event`` CLOSED).

        Dùng thời điểm ghi hàng thay vì ``opened_at``/``closed_at`` (mốc nến) để nến lớn hoặc
        replay không làm lỡ cửa sổ phát hiện.
        """
        opened = await self._session.execute(
            select(TradeRecord.id, TradeRecord.symbol, TradeRecord.side)
            .where(
                TradeRecord.workspace_id == workspace_id,
                TradeRecord.bot_id == bot_id,
                TradeRecord.mode == "PAPER",
                TradeRecord.created_at >= since,
            )
            .order_by(TradeRecord.created_at, TradeRecord.id)
            .limit(limit)
        )
        closed = await self._session.execute(
            select(TradeRecord.id, TradeRecord.symbol, TradeRecord.side)
            .join(
                PositionEventRecord,
                and_(
                    PositionEventRecord.workspace_id == TradeRecord.workspace_id,
                    PositionEventRecord.bot_id == TradeRecord.bot_id,
                    PositionEventRecord.account_id == TradeRecord.account_id,
                    PositionEventRecord.intent_id == TradeRecord.intent_id,
                    PositionEventRecord.mode == "PAPER",
                    PositionEventRecord.event_type == "CLOSED",
                ),
            )
            .where(
                TradeRecord.workspace_id == workspace_id,
                TradeRecord.bot_id == bot_id,
                TradeRecord.mode == "PAPER",
                TradeRecord.status == "CLOSED",
                PositionEventRecord.created_at >= since,
            )
            .order_by(PositionEventRecord.created_at, TradeRecord.id)
            .limit(limit)
        )
        events = [
            TradeEvent(trade_id, TradeNotificationKind.OPENED, str(symbol), str(side))
            for trade_id, symbol, side in opened
        ]
        events += [
            TradeEvent(trade_id, TradeNotificationKind.CLOSED, str(symbol), str(side))
            for trade_id, symbol, side in closed
        ]
        return tuple(events)

    async def enqueue_trade_notification(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        subscription_id: UUID,
        trade_id: UUID,
        kind: TradeNotificationKind,
        available_at: datetime,
    ) -> bool:
        inserted = await self._session.scalar(
            insert(TelegramTradeNotification)
            .values(
                id=uuid4(),
                workspace_id=workspace_id,
                bot_id=bot_id,
                subscription_id=subscription_id,
                trade_id=trade_id,
                kind=kind.value,
                status="PENDING",
                attempts=0,
                available_at=available_at,
            )
            .on_conflict_do_nothing(
                index_elements=["workspace_id", "subscription_id", "trade_id", "kind"]
            )
            .returning(TelegramTradeNotification.id)
        )
        return inserted is not None

    # --- nhận lease và dựng dữ kiện ------------------------------------------------------

    async def claim_trade_notification(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
    ) -> TradeNotificationClaim | None:
        query = _active_subscription_join(
            select(TelegramTradeNotification, TelegramSignalSubscription.telegram_chat_id).join(
                TelegramSignalSubscription,
                and_(
                    TelegramSignalSubscription.workspace_id
                    == TelegramTradeNotification.workspace_id,
                    TelegramSignalSubscription.bot_id == TelegramTradeNotification.bot_id,
                    TelegramSignalSubscription.id == TelegramTradeNotification.subscription_id,
                ),
            )
        )
        query = (
            query.where(
                TelegramTradeNotification.workspace_id == workspace_id,
                TelegramTradeNotification.bot_id == bot_id,
                TelegramTradeNotification.attempts < max_attempts,
                TelegramTradeNotification.available_at <= now,
                or_(
                    TelegramTradeNotification.status == "PENDING",
                    and_(
                        TelegramTradeNotification.status == "PROCESSING",
                        TelegramTradeNotification.lease_expires_at <= now,
                    ),
                ),
            )
            .order_by(TelegramTradeNotification.available_at, TelegramTradeNotification.created_at)
            .limit(1)
            .with_for_update(of=TelegramTradeNotification, skip_locked=True)
        )
        row = (await self._session.execute(query)).first()
        if row is None:
            return None
        notification: TelegramTradeNotification = row[0]
        kind = TradeNotificationKind(notification.kind)
        try:
            source = await self.load_trade_source(
                workspace_id=workspace_id,
                bot_id=bot_id,
                trade_id=notification.trade_id,
                kind=kind,
            )
        except ValueError:
            source = None
        if source is None:
            # Trade không còn/không đủ dữ kiện: ghi DEAD có mã lỗi, không gửi tin thiếu.
            notification.status = "DEAD"
            notification.last_error_code = "TRADE_RESULT_SOURCE_UNAVAILABLE"
            notification.lease_owner = None
            notification.lease_expires_at = None
            return None
        notification.status = "PROCESSING"
        notification.attempts += 1
        notification.lease_owner = worker_id
        notification.lease_expires_at = now + lease_duration
        return TradeNotificationClaim(
            notification_id=notification.id,
            bot_id=notification.bot_id,
            subscription_id=notification.subscription_id,
            trade_id=notification.trade_id,
            kind=kind,
            attempts=notification.attempts,
            chat_id=int(row.telegram_chat_id),
            source=source,
        )

    async def bot_name(self, *, workspace_id: UUID, bot_id: UUID) -> str | None:
        name = await self._session.scalar(
            select(BotInstance.name).where(
                BotInstance.workspace_id == workspace_id, BotInstance.id == bot_id
            )
        )
        return None if name is None else str(name)

    async def load_trade_source(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        trade_id: UUID,
        kind: TradeNotificationKind,
    ) -> TradeResultSource | None:
        """Đọc đúng các cột được cấp cho một trade PAPER; thiếu trade/fill trả ``None``."""
        trade = (
            await self._session.execute(
                select(
                    TradeRecord.id,
                    TradeRecord.account_id,
                    TradeRecord.intent_id,
                    TradeRecord.status,
                    TradeRecord.symbol,
                    TradeRecord.side,
                    TradeRecord.quote_asset,
                    TradeRecord.net_pnl,
                    TradeRecord.opened_at,
                    TradeRecord.closed_at,
                    TradeRecord.risk_reservation_id,
                ).where(
                    TradeRecord.workspace_id == workspace_id,
                    TradeRecord.bot_id == bot_id,
                    TradeRecord.mode == "PAPER",
                    TradeRecord.id == trade_id,
                )
            )
        ).first()
        if trade is None:
            return None
        fills = await self._session.execute(
            select(
                FillRecord.fill_role,
                FillRecord.quantity,
                FillRecord.price,
                FillRecord.occurred_at,
            )
            .where(
                FillRecord.workspace_id == workspace_id,
                FillRecord.bot_id == bot_id,
                FillRecord.account_id == trade.account_id,
                FillRecord.mode == "PAPER",
                FillRecord.intent_id == trade.intent_id,
            )
            .order_by(FillRecord.occurred_at)
        )
        facts = tuple(
            TradeFillFact(str(role), Decimal(quantity), Decimal(price), occurred_at)
            for role, quantity, price, occurred_at in fills
        )
        if not any(fact.role == "ENTRY" for fact in facts):
            return None
        exit_reason: str | None = None
        if kind is TradeNotificationKind.CLOSED:
            payloads = await self._session.execute(
                select(PositionEventRecord.event_type, PositionEventRecord.payload)
                .where(
                    PositionEventRecord.workspace_id == workspace_id,
                    PositionEventRecord.bot_id == bot_id,
                    PositionEventRecord.account_id == trade.account_id,
                    PositionEventRecord.mode == "PAPER",
                    PositionEventRecord.intent_id == trade.intent_id,
                    PositionEventRecord.event_type.in_(("CLOSED", "REDUCED")),
                )
                .order_by(PositionEventRecord.created_at.desc())
            )
            for event_type, payload in payloads:
                reason = payload.get("exit_reason") if isinstance(payload, dict) else None
                if isinstance(reason, str) and reason:
                    exit_reason = reason
                    if event_type == "CLOSED":
                        break
        reserved = await self._session.scalar(
            select(RiskReservationRecord.reserved_risk).where(
                RiskReservationRecord.workspace_id == workspace_id,
                RiskReservationRecord.bot_id == bot_id,
                RiskReservationRecord.id == trade.risk_reservation_id,
            )
        )
        name = await self.bot_name(workspace_id=workspace_id, bot_id=bot_id)
        return TradeResultSource(
            kind=kind,
            bot_id=bot_id,
            bot_name=name or str(bot_id),
            trade_id=trade.id,
            symbol=str(trade.symbol),
            side=str(trade.side),
            quote_asset=str(trade.quote_asset),
            status=str(trade.status),
            opened_at=trade.opened_at,
            closed_at=trade.closed_at,
            net_pnl=Decimal(trade.net_pnl),
            fills=facts,
            reserved_risk=None if reserved is None else Decimal(reserved),
            exit_reason=exit_reason,
        )

    # --- tổng hợp -----------------------------------------------------------------------

    async def bot_summary(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        window_start: datetime,
        window_end: datetime,
    ) -> BotSummaryRow:
        """Số liệu một bot: trade đóng trong ``[window_end - 366 ngày, window_end)``."""
        total_start = window_end - SUMMARY_TOTAL_WINDOW
        closed = await self._session.execute(
            select(
                TradeRecord.id,
                TradeRecord.net_pnl,
                TradeRecord.closed_at,
                TradeRecord.quote_asset,
            ).where(
                TradeRecord.workspace_id == workspace_id,
                TradeRecord.bot_id == bot_id,
                TradeRecord.mode == "PAPER",
                TradeRecord.status == "CLOSED",
                TradeRecord.closed_at >= total_start,
                TradeRecord.closed_at < window_end,
            )
        )
        facts: list[ClosedTradeFact] = []
        quotes: set[str] = set()
        for trade_id, net_pnl, closed_at, quote in closed:
            if closed_at is None:
                continue
            facts.append(ClosedTradeFact(trade_id, Decimal(net_pnl), closed_at))
            quotes.add(str(quote))
        open_count = await self._session.scalar(
            select(func.count(TradeRecord.id)).where(
                TradeRecord.workspace_id == workspace_id,
                TradeRecord.bot_id == bot_id,
                TradeRecord.mode == "PAPER",
                TradeRecord.status == "OPEN",
            )
        )
        name = await self.bot_name(workspace_id=workspace_id, bot_id=bot_id)
        return build_bot_summary_row(
            bot_id=bot_id,
            bot_name=name or str(bot_id),
            closed_trades=facts,
            open_positions=int(open_count or 0),
            window_start=window_start,
            window_end=window_end,
            quote_asset=quotes.pop() if len(quotes) == 1 else "USDT",
        )

    async def enqueue_report(
        self,
        *,
        workspace_id: UUID,
        chat_id: int,
        window_start: datetime,
        window_end: datetime,
        available_at: datetime,
    ) -> bool:
        inserted = await self._session.scalar(
            insert(TelegramPaperReportDelivery)
            .values(
                id=uuid4(),
                workspace_id=workspace_id,
                telegram_chat_id=chat_id,
                window_start=window_start,
                window_end=window_end,
                status="PENDING",
                attempts=0,
                available_at=available_at,
            )
            .on_conflict_do_nothing(
                index_elements=["workspace_id", "telegram_chat_id", "window_start"]
            )
            .returning(TelegramPaperReportDelivery.id)
        )
        return inserted is not None

    async def claim_report(
        self,
        *,
        workspace_id: UUID,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
    ) -> PaperReportClaim | None:
        row = await self._session.scalar(
            select(TelegramPaperReportDelivery)
            .where(
                TelegramPaperReportDelivery.workspace_id == workspace_id,
                TelegramPaperReportDelivery.attempts < max_attempts,
                TelegramPaperReportDelivery.available_at <= now,
                or_(
                    TelegramPaperReportDelivery.status == "PENDING",
                    and_(
                        TelegramPaperReportDelivery.status == "PROCESSING",
                        TelegramPaperReportDelivery.lease_expires_at <= now,
                    ),
                ),
            )
            .order_by(
                TelegramPaperReportDelivery.available_at, TelegramPaperReportDelivery.created_at
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        row.status = "PROCESSING"
        row.attempts += 1
        row.lease_owner = worker_id
        row.lease_expires_at = now + lease_duration
        return PaperReportClaim(
            report_id=row.id,
            chat_id=int(row.telegram_chat_id),
            window_start=row.window_start,
            window_end=row.window_end,
            attempts=row.attempts,
        )

    # --- chuyển trạng thái delivery (dùng chung cho hai bảng) ------------------------------

    async def renew(
        self,
        *,
        workspace_id: UUID,
        delivery_id: UUID,
        worker_id: str,
        lease_expires_at: datetime,
        report: bool = False,
    ) -> bool:
        """Gia hạn lease ngay trước HTTP để replica khác không reclaim trong lúc gửi."""
        return await self._transition(
            workspace_id, delivery_id, worker_id, report, lease_expires_at=lease_expires_at
        )

    async def mark_sent(
        self,
        *,
        workspace_id: UUID,
        delivery_id: UUID,
        worker_id: str,
        sent_at: datetime,
        report: bool = False,
    ) -> bool:
        return await self._transition(
            workspace_id,
            delivery_id,
            worker_id,
            report,
            status="SENT",
            sent_at=sent_at,
            lease_owner=None,
            lease_expires_at=None,
            last_error_code=None,
        )

    async def mark_failed(
        self,
        *,
        workspace_id: UUID,
        delivery_id: UUID,
        worker_id: str,
        available_at: datetime,
        error_code: str,
        dead: bool,
        report: bool = False,
    ) -> bool:
        return await self._transition(
            workspace_id,
            delivery_id,
            worker_id,
            report,
            status="DEAD" if dead else "PENDING",
            available_at=available_at,
            lease_owner=None,
            lease_expires_at=None,
            last_error_code=error_code[:120],
        )

    async def mark_filtered(
        self,
        *,
        workspace_id: UUID,
        delivery_id: UUID,
        worker_id: str,
        error_code: str,
        report: bool = False,
    ) -> bool:
        return await self._transition(
            workspace_id,
            delivery_id,
            worker_id,
            report,
            status="FILTERED",
            lease_owner=None,
            lease_expires_at=None,
            last_error_code=error_code[:120],
        )

    async def _transition(
        self,
        workspace_id: UUID,
        delivery_id: UUID,
        worker_id: str,
        report: bool,
        **values: Any,
    ) -> bool:
        model: Any = TelegramPaperReportDelivery if report else TelegramTradeNotification
        result = await self._session.execute(
            update(model)
            .where(
                model.workspace_id == workspace_id,
                model.id == delivery_id,
                model.status == "PROCESSING",
                model.lease_owner == worker_id,
            )
            .values(**values)
        )
        return bool(getattr(result, "rowcount", 0))
