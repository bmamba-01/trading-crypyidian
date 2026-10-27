"""Truy vấn chỉ đọc ledger PAPER cho báo cáo bot (MB-06).

Mọi câu lệnh là SELECT có bộ lọc ``workspace_id`` + ``bot_id`` (+ ``account_id`` khi bảng có)
tường minh; RLS của ``crypidian_admin_web`` là lớp phạm vi bổ sung, không thay bộ lọc này.
Repository không bao giờ ghi hay tính lại hàng ledger.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import BotInstance
from trading_bot.adapters.database.models.paper_entities import (
    OrderIntentRecord,
    TradeCostRecord,
    TradeRecord,
)
from trading_bot.adapters.database.models.signal_entities import BotDecisionRecord
from trading_bot.domain.portfolio.reporting import (
    INTENT_LINEAGE_KEYS,
    BotLedgerSnapshot,
    DecisionLineage,
    IntentLineage,
    LedgerCost,
    LedgerTrade,
    ReportBot,
)


class BotReportRepository:
    """Đọc ledger trong transaction do caller (UnitOfWork) quản lý."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_bot(self, *, workspace_id: UUID, bot_id: UUID) -> ReportBot | None:
        rows = await self._session.execute(
            select(BotInstance.workspace_id, BotInstance.id, BotInstance.trading_account_id).where(
                BotInstance.workspace_id == workspace_id, BotInstance.id == bot_id
            )
        )
        found = rows.all()
        return _bot(found[0]) if found else None

    async def list_bots(self, *, workspace_id: UUID, limit: int) -> list[ReportBot]:
        """Tối đa ``limit`` bot (caller truyền cap + 1 để phát hiện vượt cap)."""
        rows = await self._session.execute(
            select(BotInstance.workspace_id, BotInstance.id, BotInstance.trading_account_id)
            .where(BotInstance.workspace_id == workspace_id)
            .order_by(BotInstance.id)
            .limit(limit)
        )
        return [_bot(row) for row in rows.all()]

    async def load_bot_ledger(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        account_id: UUID,
        start: datetime,
        end: datetime,
        max_closed: int,
        max_open: int,
    ) -> BotLedgerSnapshot:
        """Trade đóng trong ``[start, end)`` (tối đa ``max_closed + 1`` hàng), trade mở tại end
        (tối đa ``max_open + 1``) và lineage; hàng thừa chỉ để service phát hiện vượt cap."""
        scope = (
            TradeRecord.workspace_id == workspace_id,
            TradeRecord.bot_id == bot_id,
            TradeRecord.account_id == account_id,
        )
        closed = await self._scalars(
            select(TradeRecord)
            .where(
                *scope,
                TradeRecord.status == "CLOSED",
                TradeRecord.closed_at >= start,
                TradeRecord.closed_at < end,
            )
            .order_by(TradeRecord.closed_at, TradeRecord.id)
            .limit(max_closed + 1)
        )
        open_rows = await self._scalars(
            select(TradeRecord)
            .where(
                *scope,
                TradeRecord.opened_at < end,
                or_(TradeRecord.closed_at.is_(None), TradeRecord.closed_at >= end),
            )
            .order_by(TradeRecord.opened_at, TradeRecord.id)
            .limit(max_open + 1)
        )
        trades = [_trade(row) for row in closed]
        if len(trades) > max_closed or len(open_rows) > max_open:
            # Service từ chối báo cáo quá lớn; không cần đọc lineage của hàng thừa.
            return _snapshot(workspace_id, bot_id, account_id, trades, open_rows, (), (), ())
        trade_ids = [trade.trade_id for trade in trades]
        intent_ids = [trade.intent_id for trade in trades]
        candidates = sorted({trade.candidate_id for trade in trades})
        costs = (
            await self._scalars(
                select(TradeCostRecord)
                .where(
                    TradeCostRecord.workspace_id == workspace_id,
                    TradeCostRecord.bot_id == bot_id,
                    TradeCostRecord.account_id == account_id,
                    TradeCostRecord.trade_id.in_(trade_ids),
                )
                .order_by(TradeCostRecord.trade_id, TradeCostRecord.id)
            )
            if trade_ids
            else []
        )
        intents = (
            await self._scalars(
                select(OrderIntentRecord).where(
                    OrderIntentRecord.workspace_id == workspace_id,
                    OrderIntentRecord.bot_id == bot_id,
                    OrderIntentRecord.account_id == account_id,
                    OrderIntentRecord.id.in_(intent_ids),
                )
            )
            if intent_ids
            else []
        )
        decisions = (
            await self._scalars(
                select(BotDecisionRecord).where(
                    BotDecisionRecord.workspace_id == workspace_id,
                    BotDecisionRecord.bot_id == bot_id,
                    BotDecisionRecord.status == "CANDIDATE",
                    BotDecisionRecord.candidate_id.in_(candidates),
                )
            )
            if candidates
            else []
        )
        return _snapshot(
            workspace_id,
            bot_id,
            account_id,
            trades,
            open_rows,
            [_cost(row) for row in costs],
            [_intent(row) for row in intents],
            [_decision(row) for row in decisions],
        )

    async def workspace_closed_totals(
        self, *, workspace_id: UUID, start: datetime, end: datetime
    ) -> dict[str, tuple[int, Decimal]]:
        """Tổng ledger độc lập (không lọc bot) để đối soát báo cáo tổng hợp workspace."""
        rows = await self._session.execute(
            select(TradeRecord.quote_asset, func.count(), func.sum(TradeRecord.net_pnl))
            .where(
                TradeRecord.workspace_id == workspace_id,
                TradeRecord.status == "CLOSED",
                TradeRecord.closed_at >= start,
                TradeRecord.closed_at < end,
            )
            .group_by(TradeRecord.quote_asset)
        )
        return {
            str(asset): (int(count), Decimal(total) if total is not None else Decimal(0))
            for asset, count, total in rows.all()
        }

    async def _scalars(self, statement: Any) -> list[Any]:
        result = await self._session.scalars(statement)
        return list(result.all())


def _bot(row: Sequence[Any]) -> ReportBot:
    return ReportBot(workspace_id=row[0], bot_id=row[1], account_id=row[2])


def _snapshot(
    workspace_id: UUID,
    bot_id: UUID,
    account_id: UUID,
    trades: Sequence[LedgerTrade],
    open_rows: Sequence[TradeRecord],
    costs: Sequence[LedgerCost],
    intents: Sequence[IntentLineage],
    decisions: Sequence[DecisionLineage],
) -> BotLedgerSnapshot:
    return BotLedgerSnapshot(
        workspace_id=workspace_id,
        bot_id=bot_id,
        account_id=account_id,
        closed_trades=tuple(trades),
        open_trades=tuple(_trade(row) for row in open_rows),
        costs=tuple(costs),
        intents=tuple(intents),
        decisions=tuple(decisions),
    )


def _trade(row: TradeRecord) -> LedgerTrade:
    return LedgerTrade(
        trade_id=row.id,
        intent_id=row.intent_id,
        workspace_id=row.workspace_id,
        bot_id=row.bot_id,
        account_id=row.account_id,
        status=row.status,
        symbol=row.symbol,
        side=row.side,
        quote_asset=row.quote_asset,
        opened_at=row.opened_at,
        closed_at=row.closed_at,
        gross_pnl=row.gross_pnl,
        fee_total=row.fee_total,
        funding_total=row.funding_total,
        slippage_total=row.slippage_total,
        total_cost=row.total_cost,
        net_pnl=row.net_pnl,
        settings_version_id=row.settings_version_id,
        strategy_release_id=row.strategy_release_id,
        strategy_release_hash=row.strategy_release_hash,
        feature_snapshot_id=row.feature_snapshot_id,
        candidate_id=row.candidate_id,
        risk_decision_id=row.risk_decision_id,
        risk_reservation_id=row.risk_reservation_id,
        lineage=dict(row.lineage or {}),
    )


def _cost(row: TradeCostRecord) -> LedgerCost:
    return LedgerCost(
        trade_id=row.trade_id,
        workspace_id=row.workspace_id,
        bot_id=row.bot_id,
        cost_type=row.cost_type,
        amount=row.amount,
        currency=row.currency,
    )


def _intent(row: OrderIntentRecord) -> IntentLineage:
    evidence = row.evidence or {}
    return IntentLineage(
        intent_id=row.id,
        bot_id=row.bot_id,
        evidence={key: evidence.get(key) for key in INTENT_LINEAGE_KEYS},
    )


def _decision(row: BotDecisionRecord) -> DecisionLineage:
    return DecisionLineage(
        decision_id=row.id,
        bot_id=row.bot_id,
        candidate_id=row.candidate_id or "",
        decision_hash=row.decision_hash,
        bot_config_version_id=row.bot_config_version_id,
        bot_config_hash=row.bot_config_hash,
        root_settings_version_id=row.root_settings_version_id,
        model_release_id=row.model_release_id,
    )
