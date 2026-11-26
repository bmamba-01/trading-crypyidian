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
from sqlalchemy.orm import load_only

from trading_bot.adapters.database.models import BotInstance
from trading_bot.adapters.database.models.entities import PaperAccountSeed
from trading_bot.adapters.database.models.market_entities import (
    IngestionGap,
    MarketBar,
    MarketInstrument,
    MarketLiquidation,
    MarketLiveQuote,
    MarketMarkPrice,
)
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

_TRADE_LOAD = (
    TradeRecord.id,
    TradeRecord.intent_id,
    TradeRecord.workspace_id,
    TradeRecord.bot_id,
    TradeRecord.account_id,
    TradeRecord.status,
    TradeRecord.symbol,
    TradeRecord.side,
    TradeRecord.quote_asset,
    TradeRecord.opened_at,
    TradeRecord.closed_at,
    TradeRecord.gross_pnl,
    TradeRecord.fee_total,
    TradeRecord.funding_total,
    TradeRecord.slippage_total,
    TradeRecord.total_cost,
    TradeRecord.net_pnl,
    TradeRecord.settings_version_id,
    TradeRecord.strategy_release_id,
    TradeRecord.strategy_release_hash,
    TradeRecord.feature_snapshot_id,
    TradeRecord.candidate_id,
    TradeRecord.risk_decision_id,
    TradeRecord.risk_reservation_id,
    TradeRecord.lineage,
)
_TRADE_COST_LOAD = (
    TradeCostRecord.id,
    TradeCostRecord.workspace_id,
    TradeCostRecord.bot_id,
    TradeCostRecord.account_id,
    TradeCostRecord.trade_id,
    TradeCostRecord.cost_type,
    TradeCostRecord.amount,
    TradeCostRecord.currency,
)
_ORDER_INTENT_LOAD = (
    OrderIntentRecord.id,
    OrderIntentRecord.workspace_id,
    OrderIntentRecord.bot_id,
    OrderIntentRecord.account_id,
    OrderIntentRecord.mode,
    OrderIntentRecord.idempotency_key,
    OrderIntentRecord.plan_hash,
    OrderIntentRecord.settings_version_id,
    OrderIntentRecord.risk_decision_id,
    OrderIntentRecord.risk_reservation_id,
    OrderIntentRecord.symbol,
    OrderIntentRecord.quote_asset,
    OrderIntentRecord.side,
    OrderIntentRecord.quantity,
    OrderIntentRecord.filled_quantity,
    OrderIntentRecord.status,
    OrderIntentRecord.evidence,
)
_BOT_DECISION_LOAD = (
    BotDecisionRecord.id,
    BotDecisionRecord.workspace_id,
    BotDecisionRecord.bot_id,
    BotDecisionRecord.account_id,
    BotDecisionRecord.evidence_id,
    BotDecisionRecord.evidence_hash,
    BotDecisionRecord.bot_config_version_id,
    BotDecisionRecord.bot_config_hash,
    BotDecisionRecord.root_settings_version_id,
    BotDecisionRecord.model_release_id,
    BotDecisionRecord.evaluated_at,
    BotDecisionRecord.status,
    BotDecisionRecord.reason_code,
    BotDecisionRecord.candidate_id,
    BotDecisionRecord.decision_hash,
    BotDecisionRecord.payload,
    BotDecisionRecord.created_at,
)
_FILL_LOAD = (
    FillRecord.id,
    FillRecord.workspace_id,
    FillRecord.bot_id,
    FillRecord.account_id,
    FillRecord.mode,
    FillRecord.intent_id,
    FillRecord.fill_role,
    FillRecord.fill_key,
    FillRecord.bar_key,
    FillRecord.fill_policy_version,
    FillRecord.quantity,
    FillRecord.price,
    FillRecord.fee_amount,
    FillRecord.slippage_amount,
    FillRecord.occurred_at,
    FillRecord.lineage,
    FillRecord.created_at,
)
_POSITION_EVENT_LOAD = (
    PositionEventRecord.id,
    PositionEventRecord.workspace_id,
    PositionEventRecord.bot_id,
    PositionEventRecord.account_id,
    PositionEventRecord.mode,
    PositionEventRecord.intent_id,
    PositionEventRecord.event_key,
    PositionEventRecord.event_type,
    PositionEventRecord.quantity,
    PositionEventRecord.occurred_at,
    PositionEventRecord.payload,
    PositionEventRecord.created_at,
)
_TRADE_PLAN_LOAD = (
    TradePlanRecord.id,
    TradePlanRecord.workspace_id,
    TradePlanRecord.bot_id,
    TradePlanRecord.account_id,
    TradePlanRecord.plan_hash,
    TradePlanRecord.payload,
)
_MARKET_BAR_LOAD = (
    MarketBar.id,
    MarketBar.workspace_id,
    MarketBar.symbol,
    MarketBar.timeframe,
    MarketBar.source,
    MarketBar.open_time,
    MarketBar.close_time,
    MarketBar.available_at,
    MarketBar.is_closed,
    MarketBar.revision,
    MarketBar.checksum,
    MarketBar.open,
    MarketBar.high,
    MarketBar.low,
    MarketBar.close,
    MarketBar.volume,
    MarketBar.quote_volume,
    MarketBar.taker_buy_base_volume,
)
_INGESTION_GAP_LOAD = (
    IngestionGap.id,
    IngestionGap.workspace_id,
    IngestionGap.source,
    IngestionGap.symbol,
    IngestionGap.timeframe,
    IngestionGap.gap_start,
    IngestionGap.gap_end,
    IngestionGap.detected_at,
    IngestionGap.resolved_at,
)
_LIVE_QUOTE_LOAD = (
    MarketLiveQuote.workspace_id,
    MarketLiveQuote.source,
    MarketLiveQuote.instrument_id,
    MarketLiveQuote.symbol,
    MarketLiveQuote.bid_price,
    MarketLiveQuote.bid_quantity,
    MarketLiveQuote.ask_price,
    MarketLiveQuote.ask_quantity,
    MarketLiveQuote.event_at,
)
_MARK_PRICE_LOAD = (
    MarketMarkPrice.id,
    MarketMarkPrice.workspace_id,
    MarketMarkPrice.instrument_id,
    MarketMarkPrice.source,
    MarketMarkPrice.event_at,
    MarketMarkPrice.available_at,
    MarketMarkPrice.mark_price,
    MarketMarkPrice.index_price,
    MarketMarkPrice.funding_rate,
    MarketMarkPrice.next_funding_time,
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
            .options(load_only(*_TRADE_LOAD))
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
            .options(load_only(*_TRADE_LOAD))
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
                .options(load_only(*_TRADE_COST_LOAD))
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
                select(OrderIntentRecord)
                .options(load_only(*_ORDER_INTENT_LOAD))
                .where(
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
                select(BotDecisionRecord)
                .options(load_only(*_BOT_DECISION_LOAD))
                .where(
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
                .options(load_only(*_FILL_LOAD))
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
                .options(load_only(*_POSITION_EVENT_LOAD))
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
            select(TradeRecord)
            .options(load_only(TradeRecord.id, TradeRecord.lineage))
            .where(
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
            select(TradePlanRecord)
            .options(load_only(*_TRADE_PLAN_LOAD))
            .where(
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
            .options(load_only(*_MARKET_BAR_LOAD))
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

    async def latest_bot_decisions(
        self, *, workspace_id: UUID, bot_id: UUID, limit: int = 5, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Các quyết định gần nhất của bot (payload + hash để tầng service xác minh) kèm kế hoạch
        đã được risk gate duyệt của candidate (nếu có) — dùng cho thẻ tín hiệu SIG-3."""
        rows = await self._scalars(
            select(BotDecisionRecord)
            .options(load_only(*_BOT_DECISION_LOAD))
            .where(
                BotDecisionRecord.workspace_id == workspace_id,
                BotDecisionRecord.bot_id == bot_id,
            )
            .order_by(BotDecisionRecord.evaluated_at.desc(), BotDecisionRecord.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        candidate_ids = sorted({row.candidate_id for row in rows if row.candidate_id})
        plans: dict[str, dict[str, Any]] = {}
        if candidate_ids:
            plan_rows = await self._scalars(
                select(TradePlanRecord)
                .options(load_only(*_TRADE_PLAN_LOAD))
                .where(
                    TradePlanRecord.workspace_id == workspace_id,
                    TradePlanRecord.bot_id == bot_id,
                    TradePlanRecord.payload["candidate_id"].astext.in_(candidate_ids),
                )
            )
            for plan in plan_rows:
                plans[str(plan.payload.get("candidate_id"))] = dict(plan.payload)
        return [
            {
                "payload": dict(row.payload),
                "decision_hash": row.decision_hash,
                "plan": plans.get(row.candidate_id or ""),
            }
            for row in rows
        ]

    async def list_market_series(self, *, workspace_id: UUID, as_of: datetime) -> list[str]:
        """Các bộ ``SYMBOL|timeframe|source`` có nến đã đóng trong kho nội bộ của workspace."""
        rows = await self._session.execute(
            select(MarketBar.symbol, MarketBar.timeframe, MarketBar.source)
            .where(
                MarketBar.workspace_id == workspace_id,
                MarketBar.available_at <= as_of,
                MarketBar.is_closed.is_(True),
            )
            .group_by(MarketBar.symbol, MarketBar.timeframe, MarketBar.source)
            .order_by(MarketBar.symbol, MarketBar.timeframe, MarketBar.source)
        )
        return [f"{symbol}|{timeframe}|{source}" for symbol, timeframe, source in rows.all()]

    async def open_positions(
        self, *, workspace_id: UUID, bot_id: UUID, symbol: str
    ) -> list[dict[str, Any]]:
        """Vị thế PAPER đang mở (CK-7): fill ENTRY/EXIT, thang TP kế hoạch đã duyệt, SL đã dời."""
        trades = await self._scalars(
            select(TradeRecord)
            .options(load_only(*_TRADE_LOAD))
            .where(
                TradeRecord.workspace_id == workspace_id,
                TradeRecord.bot_id == bot_id,
                TradeRecord.symbol == symbol,
                TradeRecord.status == "OPEN",
            )
            .order_by(TradeRecord.opened_at)
        )
        if not trades:
            return []
        intent_ids = [trade.intent_id for trade in trades]
        fills = await self._scalars(
            select(FillRecord)
            .options(load_only(*_FILL_LOAD))
            .where(
                FillRecord.workspace_id == workspace_id,
                FillRecord.bot_id == bot_id,
                FillRecord.intent_id.in_(intent_ids),
                FillRecord.fill_role.in_(("ENTRY", "EXIT")),
            )
            .order_by(FillRecord.occurred_at)
        )
        events = await self._scalars(
            select(PositionEventRecord)
            .options(load_only(*_POSITION_EVENT_LOAD))
            .where(
                PositionEventRecord.workspace_id == workspace_id,
                PositionEventRecord.bot_id == bot_id,
                PositionEventRecord.intent_id.in_(intent_ids),
                PositionEventRecord.event_type.in_(("STOP_UPDATED", "REDUCED")),
            )
            .order_by(PositionEventRecord.occurred_at)
        )
        plans = await self._scalars(
            select(TradePlanRecord)
            .options(load_only(*_TRADE_PLAN_LOAD))
            .where(
                TradePlanRecord.workspace_id == workspace_id,
                TradePlanRecord.bot_id == bot_id,
                TradePlanRecord.payload["candidate_id"].astext.in_(
                    [trade.candidate_id for trade in trades]
                ),
            )
        )
        plan_by_candidate = {str(plan.payload.get("candidate_id")): plan.payload for plan in plans}
        latest_stop: dict[UUID, Any] = {}
        exit_reason_by_fill: dict[tuple[UUID, str], str] = {}
        for event in events:
            if event.event_type == "STOP_UPDATED" and event.payload.get("stop_price") is not None:
                latest_stop[event.intent_id] = Decimal(str(event.payload["stop_price"]))
            elif event.event_type == "REDUCED":
                fill_key = event.payload.get("fill_key")
                reason = event.payload.get("exit_reason")
                if isinstance(fill_key, str) and isinstance(reason, str):
                    exit_reason_by_fill[(event.intent_id, fill_key)] = reason
        output: list[dict[str, Any]] = []
        for trade in trades:
            plan = plan_by_candidate.get(trade.candidate_id) or {}
            raw_targets = plan.get("targets")
            output.append(
                {
                    "trade_id": str(trade.id),
                    "symbol": trade.symbol,
                    "side": trade.side,
                    "quote_asset": trade.quote_asset,
                    "entry_fills": [
                        {
                            "price": fill.price,
                            "quantity": fill.quantity,
                            "fee": fill.fee_amount,
                            "occurred_at": fill.occurred_at,
                        }
                        for fill in fills
                        if fill.intent_id == trade.intent_id and fill.fill_role == "ENTRY"
                    ],
                    "exit_fills": [
                        {
                            "price": fill.price,
                            "quantity": fill.quantity,
                            "fee": fill.fee_amount,
                            "occurred_at": fill.occurred_at,
                            "fill_key": fill.fill_key,
                            "exit_reason": exit_reason_by_fill.get(
                                (trade.intent_id, fill.fill_key)
                            ),
                        }
                        for fill in fills
                        if fill.intent_id == trade.intent_id and fill.fill_role == "EXIT"
                    ],
                    "initial_stop": (
                        Decimal(str(plan["stop_price"])) if plan.get("stop_price") else None
                    ),
                    "current_stop": latest_stop.get(trade.intent_id),
                    "target": (
                        Decimal(str(plan["target_price"])) if plan.get("target_price") else None
                    ),
                    "targets": list(raw_targets) if isinstance(raw_targets, list) else [],
                }
            )
        return output

    async def bot_decisions_between(
        self, *, workspace_id: UUID, bot_id: UUID, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Quyết định của bot có ``start < evaluated_at <= end`` (CK-7, phân tích tại nến)."""
        rows = await self._scalars(
            select(BotDecisionRecord)
            .options(load_only(*_BOT_DECISION_LOAD))
            .where(
                BotDecisionRecord.workspace_id == workspace_id,
                BotDecisionRecord.bot_id == bot_id,
                BotDecisionRecord.evaluated_at > start,
                BotDecisionRecord.evaluated_at <= end,
            )
            .order_by(BotDecisionRecord.evaluated_at.desc(), BotDecisionRecord.created_at.desc())
            .limit(5)
        )
        return [{"payload": dict(row.payload), "decision_hash": row.decision_hash} for row in rows]

    async def count_open_trades(self, *, workspace_id: UUID, bot_id: UUID) -> int:
        """Số lệnh PAPER đang mở của bot trên mọi mã (chỗ chạy, CK-12)."""
        count = await self._session.scalar(
            select(func.count())
            .select_from(TradeRecord)
            .where(
                TradeRecord.workspace_id == workspace_id,
                TradeRecord.bot_id == bot_id,
                TradeRecord.status == "OPEN",
            )
        )
        return int(count or 0)

    async def closed_net_since(
        self, *, workspace_id: UUID, bot_id: UUID, since: datetime
    ) -> dict[str, Decimal]:
        """Tổng PnL ròng các lệnh đã đóng từ ``since`` theo đồng định giá (CK-12)."""
        rows = await self._session.execute(
            select(TradeRecord.quote_asset, func.sum(TradeRecord.net_pnl))
            .where(
                TradeRecord.workspace_id == workspace_id,
                TradeRecord.bot_id == bot_id,
                TradeRecord.status == "CLOSED",
                TradeRecord.closed_at >= since,
            )
            .group_by(TradeRecord.quote_asset)
        )
        return {str(asset): Decimal(str(total or 0)) for asset, total in rows.all()}

    async def bar_revision_count(
        self, *, workspace_id: UUID, symbol: str, timeframe: str, source: str, since: datetime
    ) -> int:
        """Số mốc nến (từ ``since``) có hơn một bản ghi — nến đã được sàn/ingest sửa lại (CK-9)."""
        revised = (
            select(MarketBar.open_time)
            .where(
                MarketBar.workspace_id == workspace_id,
                MarketBar.symbol == symbol,
                MarketBar.timeframe == timeframe,
                MarketBar.source == source,
                MarketBar.open_time >= since,
            )
            .group_by(MarketBar.open_time)
            .having(func.count() > 1)
            .subquery()
        )
        count = await self._session.scalar(select(func.count()).select_from(revised))
        return int(count or 0)

    async def open_ingestion_gaps(
        self, *, workspace_id: UUID, symbol: str, timeframe: str
    ) -> list[dict[str, Any]]:
        """Khoảng trống ingest chưa được lấp (``resolved_at`` rỗng) của mã/khung (CK-9)."""
        rows = await self._scalars(
            select(IngestionGap)
            .options(load_only(*_INGESTION_GAP_LOAD))
            .where(
                IngestionGap.workspace_id == workspace_id,
                IngestionGap.symbol == symbol,
                IngestionGap.timeframe == timeframe,
                IngestionGap.resolved_at.is_(None),
            )
            .order_by(IngestionGap.gap_start.desc())
            .limit(20)
        )
        return [
            {
                "source": row.source,
                "start": row.gap_start.isoformat(),
                "end": row.gap_end.isoformat(),
                "detected_at": row.detected_at.isoformat(),
            }
            for row in rows
        ]

    async def latest_live_quote(self, *, workspace_id: UUID, symbol: str) -> dict[str, Any] | None:
        """Giá mua/bán tốt nhất mới nhất (CK-1) của symbol; ``None`` khi stream chưa ghi."""
        row = (
            await self._session.execute(
                select(MarketLiveQuote)
                .options(load_only(*_LIVE_QUOTE_LOAD))
                .where(
                    MarketLiveQuote.workspace_id == workspace_id, MarketLiveQuote.symbol == symbol
                )
                .order_by(MarketLiveQuote.event_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "source": row.source,
            "bid_price": row.bid_price,
            "bid_quantity": row.bid_quantity,
            "ask_price": row.ask_price,
            "ask_quantity": row.ask_quantity,
            "event_at": row.event_at,
        }

    async def latest_mark_price(
        self, *, workspace_id: UUID, symbol: str, as_of: datetime
    ) -> dict[str, Any] | None:
        """Mẫu mark/index/funding mới nhất đã khả dụng tại ``as_of`` của symbol."""
        row = (
            await self._session.execute(
                select(MarketMarkPrice)
                .options(load_only(*_MARK_PRICE_LOAD))
                .join(
                    MarketInstrument,
                    (MarketInstrument.workspace_id == MarketMarkPrice.workspace_id)
                    & (MarketInstrument.id == MarketMarkPrice.instrument_id),
                )
                .where(
                    MarketMarkPrice.workspace_id == workspace_id,
                    MarketInstrument.symbol == symbol,
                    MarketMarkPrice.available_at <= as_of,
                )
                .order_by(MarketMarkPrice.event_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "source": row.source,
            "mark_price": row.mark_price,
            "index_price": row.index_price,
            "funding_rate": row.funding_rate,
            "next_funding_time": row.next_funding_time,
            "event_at": row.event_at,
        }

    async def load_recent_bars(
        self,
        *,
        workspace_id: UUID,
        symbol: str,
        timeframe: str,
        as_of: datetime,
        limit: int,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """Đọc ``limit`` nến đã đóng gần nhất (khả dụng tại as_of), giữ bản sửa mới nhất.

        Đọc dư gấp đôi để bù các bản sửa (revision) trùng ``open_time`` rồi cắt lại; kết quả
        sắp tăng dần theo thời gian. ``taker_buy_volume`` (SIG-0) là ``None`` khi nến chưa có
        trường này — tầng dựng chart tự ẩn lớp áp lực mua/bán thay vì ước lượng.
        """
        rows = await self._scalars(
            select(MarketBar)
            .options(load_only(*_MARKET_BAR_LOAD))
            .where(
                MarketBar.workspace_id == workspace_id,
                MarketBar.symbol == symbol,
                MarketBar.timeframe == timeframe,
                MarketBar.available_at <= as_of,
                MarketBar.is_closed.is_(True),
                *([MarketBar.source == source] if source is not None else []),
            )
            .order_by(MarketBar.open_time.desc(), MarketBar.revision.desc())
            .limit(limit * 2)
        )
        selected: dict[datetime, MarketBar] = {}
        for row in rows:
            selected.setdefault(row.open_time, row)
        latest = sorted(selected.values(), key=lambda row: row.open_time)[-limit:]
        return [
            {
                "time": int(row.open_time.timestamp()),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": float(row.volume),
                "taker_buy_volume": (
                    None if row.taker_buy_base_volume is None else float(row.taker_buy_base_volume)
                ),
                "quote_volume": None if row.quote_volume is None else float(row.quote_volume),
                "source": row.source,
            }
            for row in latest
        ]

    async def load_recent_liquidations(
        self,
        *,
        workspace_id: UUID,
        symbol: str,
        start: datetime,
        as_of: datetime,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Đọc các sự kiện thanh lý ``forceOrder`` khả dụng tại ``as_of`` trong cửa sổ gần nhất."""
        rows = await self._scalars(
            select(MarketLiquidation)
            .join(
                MarketInstrument,
                (MarketInstrument.workspace_id == MarketLiquidation.workspace_id)
                & (MarketInstrument.id == MarketLiquidation.instrument_id),
            )
            .where(
                MarketLiquidation.workspace_id == workspace_id,
                MarketInstrument.symbol == symbol,
                MarketLiquidation.available_at <= as_of,
                MarketLiquidation.event_at >= start,
                MarketLiquidation.event_at <= as_of,
            )
            .order_by(MarketLiquidation.event_at.desc())
            .limit(limit)
        )
        return [
            {
                "symbol": symbol,
                "side": row.side,
                "price": row.price,
                "average_price": row.average_price,
                "quantity": row.quantity,
                "filled_quantity": row.filled_quantity,
                "event_at": row.event_at,
                "available_at": row.available_at,
            }
            for row in rows
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
