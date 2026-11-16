"""Nguồn nến đã lưu nội bộ cho phân tích đồng thuận giả của Strategy Card (SIG-6 lát 1).

Chỉ đọc kho ``market_bar`` của workspace (RLS, phiên chỉ đọc) — không gọi mạng sàn. Chọn một
nguồn cho mỗi mã/khung như chart và bộ quét để không trộn nến thật với dữ liệu mẫu.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.bot_reports import BotReportRepository
from trading_bot.application.bot_reporting import preferred_market_source
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.strategy_pipeline.consensus import CONSENSUS_BAR_LIMIT, PriceBar
from trading_bot.auth.policy import Permission, Principal, authorize


class PostgresCardBarsProvider:
    """``(principal, symbol, timeframe)`` → nến đã đóng gần nhất, tăng dần theo thời gian."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        limit: int = CONSENSUS_BAR_LIMIT,
        unit_of_work: Callable[..., Any] = UnitOfWork,
        repository: Callable[[Any], Any] = BotReportRepository,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._limit = limit
        self._unit_of_work = unit_of_work
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))

    async def __call__(self, principal: Principal, symbol: str, timeframe: str) -> list[PriceBar]:
        if not authorize(principal, Permission.WORKSPACE_READ):
            return []
        instant = self._clock()
        async with self._unit_of_work(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            repository = self._repository(unit.session)
            sources = [
                pair.split("|", 2)[2]
                for pair in await repository.list_market_series(
                    workspace_id=principal.workspace_id, as_of=instant
                )
                if pair.split("|", 2)[:2] == [symbol, timeframe]
            ]
            if not sources:
                return []
            rows = await repository.load_recent_bars(
                workspace_id=principal.workspace_id,
                symbol=symbol,
                timeframe=timeframe,
                as_of=instant,
                limit=self._limit,
                source=preferred_market_source(sources),
            )
        return [
            PriceBar(
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
            )
            for row in rows
        ]
