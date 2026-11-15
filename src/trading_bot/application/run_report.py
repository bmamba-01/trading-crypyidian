"""Nạp dữ liệu cho Run Report AI-1 từ PostgreSQL (chỉ đọc) rồi dựng báo cáo tất định.

Nguồn: báo cáo bot ``bot-report.v2`` (ledger đã đối soát), ``bot_decision`` trong cửa sổ, order
intent theo ``bot_decision_id``, ``risk_reservation.reserved_risk`` (1R) và nến đã đóng của khung
chiến lược để tính MAE/MFE. Mỗi bot đọc trong UnitOfWork có ``bot_id`` riêng (RLS theo bot).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import String, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import (
    BotConfigVersion,
    BotDecisionRecord,
    BotInstance,
    OrderIntentRecord,
    TradingAccount,
)
from trading_bot.adapters.database.models.paper_entities import TradeRecord
from trading_bot.adapters.database.models.risk_entities import RiskReservationRecord
from trading_bot.adapters.database.repositories.bot_reports import BotReportRepository
from trading_bot.application.market_signal_composition import load_closed_market_bars
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.strategy_market_requirements import entry_timeframe_for_release
from trading_bot.domain.portfolio.reporting import build_bot_report
from trading_bot.domain.portfolio.run_report import (
    BotRunInput,
    DecisionCount,
    RunReport,
    TradeExtremes,
    build_run_report,
)

MAX_CLOSED_TRADES = 2000
MAX_OPEN_TRADES = 200


async def _enabled_bots(session: AsyncSession, workspace_id: UUID) -> list[tuple[Any, ...]]:
    rows = await session.execute(
        select(BotInstance.id, BotInstance.name, BotInstance.trading_account_id)
        .join(
            TradingAccount,
            and_(
                TradingAccount.workspace_id == BotInstance.workspace_id,
                TradingAccount.id == BotInstance.trading_account_id,
            ),
        )
        .where(BotInstance.workspace_id == workspace_id, TradingAccount.status == "ENABLED")
        .order_by(BotInstance.name)
    )
    return list(rows.all())


async def _overlay(session: AsyncSession, workspace_id: UUID, bot_id: UUID) -> dict[str, Any]:
    overlay = await session.scalar(
        select(BotConfigVersion.overlay_json)
        .where(
            BotConfigVersion.workspace_id == workspace_id,
            BotConfigVersion.bot_id == bot_id,
            BotConfigVersion.status == "PUBLISHED",
        )
        .order_by(BotConfigVersion.version.desc())
        .limit(1)
    )
    return dict(overlay or {})


async def _decisions(
    session: AsyncSession, workspace_id: UUID, bot_id: UUID, start: datetime, end: datetime
) -> tuple[tuple[DecisionCount, ...], int]:
    window = (
        BotDecisionRecord.workspace_id == workspace_id,
        BotDecisionRecord.bot_id == bot_id,
        BotDecisionRecord.created_at >= start,
        BotDecisionRecord.created_at < end,
    )
    rows = await session.execute(
        select(BotDecisionRecord.status, BotDecisionRecord.reason_code, func.count())
        .where(*window)
        .group_by(BotDecisionRecord.status, BotDecisionRecord.reason_code)
    )
    counts = tuple(
        DecisionCount(str(status), str(reason), int(count)) for status, reason, count in rows
    )
    converted = await session.scalar(
        select(func.count(func.distinct(BotDecisionRecord.id)))
        .join(
            OrderIntentRecord,
            and_(
                OrderIntentRecord.workspace_id == BotDecisionRecord.workspace_id,
                OrderIntentRecord.evidence["bot_decision_id"].as_string()
                == cast(BotDecisionRecord.id, String),
            ),
        )
        .where(*window, BotDecisionRecord.status == "CANDIDATE")
    )
    return counts, int(converted or 0)


async def _extremes(
    session: AsyncSession,
    workspace_id: UUID,
    trades: Sequence[dict[str, Any]],
    timeframe: str | None,
) -> tuple[TradeExtremes, ...]:
    result: list[TradeExtremes] = []
    for trade in trades:
        trade_id = UUID(str(trade["trade_id"]))
        risk = await session.scalar(
            select(RiskReservationRecord.reserved_risk)
            .join(
                TradeRecord,
                and_(
                    TradeRecord.workspace_id == RiskReservationRecord.workspace_id,
                    TradeRecord.risk_reservation_id == RiskReservationRecord.id,
                ),
            )
            .where(TradeRecord.workspace_id == workspace_id, TradeRecord.id == trade_id)
        )
        low: Decimal | None = None
        high: Decimal | None = None
        opened = datetime.fromisoformat(str(trade["opened_at"]))
        closed = datetime.fromisoformat(str(trade["closed_at"]))
        # Ưu tiên nến 1m (khung execution khi root có 1m), rồi khung chiến lược. Chỉ dùng nến
        # đã đóng tại thời điểm thoát lệnh, không dùng giá sau khi đã thoát lệnh; lệnh ngắn hơn
        # một nến thì để None.
        for frame in ("1m", timeframe):
            if frame is None:
                continue
            bars = await load_closed_market_bars(
                session,
                workspace_id=workspace_id,
                symbol=str(trade["symbol"]),
                timeframe=frame,
                as_of=closed,
            )
            held = [bar for bar in bars if bar.close_time > opened and bar.open_time < closed]
            if held:
                low = min(bar.low for bar in held)
                high = max(bar.high for bar in held)
                break
        result.append(
            TradeExtremes(
                trade_id=str(trade_id),
                risk_amount=None if risk is None else Decimal(str(risk)),
                lowest_price=low,
                highest_price=high,
            )
        )
    return tuple(result)


async def load_run_report(
    sessions: async_sessionmaker[AsyncSession],
    *,
    workspace_id: UUID,
    start: datetime,
    end: datetime,
    run_label: str,
) -> RunReport:
    """Run Report cho mọi bot ENABLED của workspace trong ``[start, end)``."""
    async with UnitOfWork(sessions, workspace_id=workspace_id) as uow:
        bots = await _enabled_bots(uow.session, workspace_id)
    inputs: list[BotRunInput] = []
    for bot_id, name, account_id in bots:
        async with UnitOfWork(
            sessions, workspace_id=workspace_id, bot_id=bot_id, snapshot_read_only=True
        ) as uow:
            session = uow.session
            overlay = await _overlay(session, workspace_id, bot_id)
            strategy = overlay.get("strategy") or {}
            release = str(strategy.get("release_id") or "unknown")
            timeframe = entry_timeframe_for_release(release)
            snapshot = await BotReportRepository(session).load_bot_ledger(
                workspace_id=workspace_id,
                bot_id=bot_id,
                account_id=account_id,
                start=start,
                end=end,
                max_closed=MAX_CLOSED_TRADES,
                max_open=MAX_OPEN_TRADES,
            )
            report = build_bot_report(snapshot=snapshot, start=start, end=end)
            payload = dict(report.payload)
            decisions, converted = await _decisions(session, workspace_id, bot_id, start, end)
            extremes = await _extremes(
                session, workspace_id, list(payload.get("trades") or ()), timeframe
            )
        inputs.append(
            BotRunInput(
                bot_id=str(bot_id),
                bot_name=str(name),
                release_id=release,
                timeframe=timeframe,
                parameters=dict(strategy.get("parameters") or {}),
                risk=dict(overlay.get("risk") or {}),
                universe=tuple((overlay.get("universe") or {}).get("symbols") or ()),
                bot_report=payload,
                decisions=decisions,
                candidates_with_intent=converted,
                extremes=extremes,
            )
        )
    return build_run_report(
        workspace_id=str(workspace_id),
        run_label=run_label,
        window_start=start,
        window_end=end,
        bots=inputs,
    )
