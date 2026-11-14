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
from trading_bot.adapters.database.models.entities import PaperAccountSeed
from trading_bot.adapters.database.models.market_entities import MarketBar
from trading_bot.adapters.database.models.paper_entities import (
    FillRecord,
    OrderIntentRecord,
    PositionEventRecord,
    TradeCostRecord,
    TradeRecord,
)
from trading_bot.adapters.database.models.risk_entities import TradePlanRecord
from trading_bot.adapters.database.models.signal_entities import BotDecisionRecord
from trading_bot.domain.portfolio.reporting import (
    INTENT_LINEAGE_KEYS,
    BotLedgerSnapshot,
    DecisionLineage,
    IntentLineage,
    LedgerCost,
    LedgerFill,
    LedgerTrade,
    PositionTransition,
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
        fills = (
            await self._scalars(
                select(FillRecord)
                .where(
                    FillRecord.workspace_id == workspace_id,
                    FillRecord.bot_id == bot_id,
                    FillRecord.account_id == account_id,
                    FillRecord.intent_id.in_(intent_ids),
                )
                .order_by(FillRecord.occurred_at, FillRecord.fill_key)
            )
            if intent_ids
            else []
        )
        position_events = (
            await self._scalars(
                select(PositionEventRecord)
                .where(
                    PositionEventRecord.workspace_id == workspace_id,
                    PositionEventRecord.bot_id == bot_id,
                    PositionEventRecord.account_id == account_id,
                    PositionEventRecord.intent_id.in_(intent_ids),
                )
                .order_by(PositionEventRecord.occurred_at, PositionEventRecord.event_key)
            )
            if intent_ids
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
            [_fill(row) for row in fills],
            [_position_event(row) for row in position_events],
        )

    async def load_account_seed(self, *, workspace_id: UUID, bot_id: UUID) -> dict[str, Any] | None:
        """Vốn ban đầu của tài khoản PAPER của bot (bất biến, một hàng cho mỗi tài khoản).

        Chỉ chọn các cột admin-web được cấp SELECT (migration `d2e4f6a8b0c1`); dòng được lọc bởi
        RLS `bot_member_isolation`. Không có hàng thì trả None để nơi gọi ghi "chưa có dữ liệu".
        """
        result = await self._session.execute(
            select(
                PaperAccountSeed.quote_asset,
                PaperAccountSeed.initial_equity,
                PaperAccountSeed.created_at,
            )
            .where(
                PaperAccountSeed.workspace_id == workspace_id,
                PaperAccountSeed.bot_id == bot_id,
            )
            .order_by(PaperAccountSeed.created_at.desc())
            .limit(1)
        )
        row = result.first()
        if row is None:
            return None
        return {
            "quote_asset": row.quote_asset,
            "initial_equity": str(row.initial_equity),
            "created_at": row.created_at.isoformat(),
        }

    async def load_trade_plans(
        self, *, workspace_id: UUID, bot_id: UUID, account_id: UUID, trade_ids: Sequence[UUID]
    ) -> dict[UUID, dict[str, Any]]:
        """Đọc kế hoạch đã được risk gate duyệt của các trade (giá, đòn bẩy, chi phí dự trù).

        Kế hoạch được ghim qua `trade.lineage.plan_hash`. Trade thiếu hash hoặc thiếu hàng kế
        hoạch không có mặt trong kết quả, để nơi gọi ghi "chưa có dữ liệu" thay vì suy diễn.
        """
        if not trade_ids:
            return {}
        trades = await self._scalars(
            select(TradeRecord).where(
                TradeRecord.workspace_id == workspace_id,
                TradeRecord.bot_id == bot_id,
                TradeRecord.account_id == account_id,
                TradeRecord.id.in_(list(trade_ids)),
            )
        )
        hashes = {
            trade.id: trade.lineage["plan_hash"]
            for trade in trades
            if isinstance((trade.lineage or {}).get("plan_hash"), str)
        }
        if not hashes:
            return {}
        plans = await self._scalars(
            select(TradePlanRecord).where(
                TradePlanRecord.workspace_id == workspace_id,
                TradePlanRecord.bot_id == bot_id,
                TradePlanRecord.account_id == account_id,
                TradePlanRecord.plan_hash.in_(sorted(set(hashes.values()))),
            )
        )
        fields = (
            "entry_reference_price",
            "entry_price",
            "stop_price",
            "target_price",
            "quantity",
            "notional",
            "leverage",
            "initial_margin",
            "risk_budget",
            "estimated_loss",
            "entry_slippage_cost",
        )
        by_hash = {
            plan.plan_hash: {key: plan.payload.get(key) for key in fields}
            | {"plan_hash": plan.plan_hash}
            for plan in plans
            if isinstance(plan.payload, dict)
        }
        return {
            trade_id: by_hash[plan_hash]
            for trade_id, plan_hash in hashes.items()
            if plan_hash in by_hash
        }

    async def load_trade_bars(
        self,
        *,
        workspace_id: UUID,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        as_of: datetime,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Đọc nến đã đóng và khả dụng tại as_of cho biểu đồ, giữ bản sửa mới nhất."""
        rows = await self._scalars(
            select(MarketBar)
            .where(
                MarketBar.workspace_id == workspace_id,
                MarketBar.symbol == symbol,
                MarketBar.timeframe == timeframe,
                MarketBar.open_time >= start,
                MarketBar.open_time < end,
                MarketBar.available_at <= as_of,
                MarketBar.is_closed.is_(True),
            )
            .order_by(MarketBar.open_time, MarketBar.revision.desc())
            .limit(limit)
        )
        selected: dict[datetime, MarketBar] = {}
        for row in rows:
            selected.setdefault(row.open_time, row)
        return [
            {
                "time": int(row.open_time.timestamp()),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": float(row.volume),
            }
            for row in selected.values()
        ]

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
    fills: Sequence[LedgerFill] = (),
    position_events: Sequence[PositionTransition] = (),
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
        fills=tuple(fills),
        position_events=tuple(position_events),
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
        candidate=(row.payload or {}).get("candidate") or {},
    )


def _fill(row: FillRecord) -> LedgerFill:
    return LedgerFill(
        fill_key=row.fill_key,
        intent_id=row.intent_id,
        workspace_id=row.workspace_id,
        bot_id=row.bot_id,
        account_id=row.account_id,
        role=row.fill_role,
        quantity=row.quantity,
        price=row.price,
        occurred_at=row.occurred_at,
        fee_amount=row.fee_amount,
        slippage_amount=row.slippage_amount,
        bar_key=row.bar_key,
    )


def _position_event(row: PositionEventRecord) -> PositionTransition:
    return PositionTransition(
        event_key=row.event_key,
        intent_id=row.intent_id,
        workspace_id=row.workspace_id,
        bot_id=row.bot_id,
        account_id=row.account_id,
        event_type=row.event_type,
        occurred_at=row.occurred_at,
        payload=dict(row.payload or {}),
    )
