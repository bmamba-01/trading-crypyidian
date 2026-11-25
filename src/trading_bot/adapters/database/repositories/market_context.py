"""Repository đọc bối cảnh thị trường theo workspace/RLS và policy nguồn (CRY-374)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text
from sqlalchemy.sql.elements import ColumnElement

from trading_bot.adapters.database.models.market_entities import (
    MarketFunding,
    MarketInstrument,
    MarketLiquidation,
    MarketMarkPrice,
    MarketOpenInterest,
    MarketTradeFlow,
)
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.domain.portfolio.market_context import (
    MarketContextKind,
    MarketContextObservation,
)
from trading_bot.domain.research.source_policy import SourceUsePurpose

_MAX_ROWS_PER_KIND = 500


class SqlAlchemyMarketContextRepository:
    """Mọi truy vấn nằm trong session/workspace của caller; không mở session riêng."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._registry = SourceRegistryRepository(session)

    async def database_clock(self, *, workspace_id: UUID) -> datetime:
        return await self._registry.database_clock(workspace_id=workspace_id)

    async def display_permitted(self, *, workspace_id: UUID, source_id: str, at: datetime) -> bool:
        """Chỉ cho hiển thị khi policy VERIFIED có receipt observer và đúng purpose DISPLAY."""
        observed = await self._registry.get_observed_entry(
            workspace_id=workspace_id, source_id=source_id
        )
        if observed is None:
            return False
        policy = observed.history.current.policy
        if policy.source_id != source_id:
            return False
        known_at = observed.known_at.get(policy.policy_id)
        if known_at is None or known_at >= at:
            return False
        return bool(
            observed.history.permits(SourceUsePurpose.DISPLAY, at=at)
            and policy.permits(SourceUsePurpose.DISPLAY, at=at)
        )

    async def list_in_window(
        self, *, workspace_id: UUID, symbol: str, start: datetime, end: datetime
    ) -> tuple[MarketContextObservation, ...]:
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        if not symbol.strip():
            raise ValueError("symbol không được rỗng.")
        if start >= end:
            raise ValueError("start phải trước end.")
        # RLS theo workspace: set_config(..., true) có hiệu lực trong transaction hiện tại.
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        results: list[MarketContextObservation] = []
        results.extend(await self._list_funding(workspace_id, symbol, start, end))
        results.extend(await self._list_oi(workspace_id, symbol, start, end))
        results.extend(await self._list_mark_price(workspace_id, symbol, start, end))
        results.extend(await self._list_liquidation(workspace_id, symbol, start, end))
        results.extend(await self._list_trade_flow(workspace_id, symbol, start, end))
        return tuple(results)

    def _instrument_filter(
        self,
        model: object,
        workspace_id: UUID,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> ColumnElement[bool]:
        return and_(
            model.workspace_id == workspace_id,  # type: ignore[attr-defined]
            MarketInstrument.workspace_id == workspace_id,
            MarketInstrument.id == model.instrument_id,  # type: ignore[attr-defined]
            MarketInstrument.symbol == symbol,
            model.event_at >= start,  # type: ignore[attr-defined]
            model.event_at <= end,  # type: ignore[attr-defined]
            model.available_at <= end,  # type: ignore[attr-defined]
        )

    async def _list_funding(
        self, workspace_id: UUID, symbol: str, start: datetime, end: datetime
    ) -> list[MarketContextObservation]:
        rows = (
            await self._session.scalars(
                select(MarketFunding)
                .join(
                    MarketInstrument,
                    and_(
                        MarketInstrument.workspace_id == MarketFunding.workspace_id,
                        MarketInstrument.id == MarketFunding.instrument_id,
                    ),
                )
                .where(self._instrument_filter(MarketFunding, workspace_id, symbol, start, end))
                .order_by(MarketFunding.event_at.desc())
                .limit(_MAX_ROWS_PER_KIND)
            )
        ).all()
        return [
            MarketContextObservation(
                kind=MarketContextKind.FUNDING,
                symbol=symbol,
                source=row.source,
                checksum=row.checksum,
                event_at=row.event_at,
                available_at=row.available_at,
                value=row.funding_rate,
                unit="ratio",
                revision=row.revision,
                raw_reference=row.idempotency_key,
            )
            for row in rows
        ]

    async def _list_oi(
        self, workspace_id: UUID, symbol: str, start: datetime, end: datetime
    ) -> list[MarketContextObservation]:
        rows = (
            await self._session.scalars(
                select(MarketOpenInterest)
                .join(
                    MarketInstrument,
                    and_(
                        MarketInstrument.workspace_id == MarketOpenInterest.workspace_id,
                        MarketInstrument.id == MarketOpenInterest.instrument_id,
                    ),
                )
                .where(
                    and_(
                        MarketOpenInterest.workspace_id == workspace_id,
                        MarketInstrument.workspace_id == workspace_id,
                        MarketInstrument.id == MarketOpenInterest.instrument_id,
                        MarketInstrument.symbol == symbol,
                        MarketOpenInterest.observed_at >= start,
                        MarketOpenInterest.observed_at <= end,
                        MarketOpenInterest.available_at <= end,
                    )
                )
                .order_by(MarketOpenInterest.observed_at.desc())
                .limit(_MAX_ROWS_PER_KIND)
            )
        ).all()
        return [
            MarketContextObservation(
                kind=MarketContextKind.OPEN_INTEREST,
                symbol=symbol,
                source=row.source,
                checksum=row.checksum,
                event_at=row.observed_at,
                available_at=row.available_at,
                value=row.quantity,
                unit="contracts",
                revision=row.revision,
                raw_reference=row.idempotency_key,
            )
            for row in rows
        ]

    async def _list_mark_price(
        self, workspace_id: UUID, symbol: str, start: datetime, end: datetime
    ) -> list[MarketContextObservation]:
        rows = (
            await self._session.scalars(
                select(MarketMarkPrice)
                .join(
                    MarketInstrument,
                    and_(
                        MarketInstrument.workspace_id == MarketMarkPrice.workspace_id,
                        MarketInstrument.id == MarketMarkPrice.instrument_id,
                    ),
                )
                .where(self._instrument_filter(MarketMarkPrice, workspace_id, symbol, start, end))
                .order_by(MarketMarkPrice.event_at.desc())
                .limit(_MAX_ROWS_PER_KIND)
            )
        ).all()
        return [
            MarketContextObservation(
                kind=MarketContextKind.MARK_PRICE,
                symbol=symbol,
                source=row.source,
                checksum=row.checksum,
                event_at=row.event_at,
                available_at=row.available_at,
                value=row.mark_price,
                unit="USDT",
                raw_reference=row.idempotency_key,
            )
            for row in rows
        ]

    async def _list_liquidation(
        self, workspace_id: UUID, symbol: str, start: datetime, end: datetime
    ) -> list[MarketContextObservation]:
        rows = (
            await self._session.scalars(
                select(MarketLiquidation)
                .join(
                    MarketInstrument,
                    and_(
                        MarketInstrument.workspace_id == MarketLiquidation.workspace_id,
                        MarketInstrument.id == MarketLiquidation.instrument_id,
                    ),
                )
                .where(self._instrument_filter(MarketLiquidation, workspace_id, symbol, start, end))
                .order_by(MarketLiquidation.event_at.desc())
                .limit(_MAX_ROWS_PER_KIND)
            )
        ).all()
        return [
            MarketContextObservation(
                kind=MarketContextKind.LIQUIDATION,
                symbol=symbol,
                source=row.source,
                checksum=row.checksum,
                event_at=row.event_at,
                available_at=row.available_at,
                value=row.quantity,
                unit="contracts",
                raw_reference=row.idempotency_key,
            )
            for row in rows
        ]

    async def _list_trade_flow(
        self, workspace_id: UUID, symbol: str, start: datetime, end: datetime
    ) -> list[MarketContextObservation]:
        rows = (
            await self._session.scalars(
                select(MarketTradeFlow)
                .join(
                    MarketInstrument,
                    and_(
                        MarketInstrument.workspace_id == MarketTradeFlow.workspace_id,
                        MarketInstrument.id == MarketTradeFlow.instrument_id,
                    ),
                )
                .where(self._instrument_filter(MarketTradeFlow, workspace_id, symbol, start, end))
                .order_by(MarketTradeFlow.event_at.desc())
                .limit(_MAX_ROWS_PER_KIND)
            )
        ).all()
        return [
            MarketContextObservation(
                kind=MarketContextKind.TRADE_FLOW,
                symbol=symbol,
                source=row.source,
                checksum=row.checksum,
                event_at=row.event_at,
                available_at=row.available_at,
                value=row.buy_quantity + row.sell_quantity,
                unit="contracts",
                raw_reference=row.idempotency_key,
            )
            for row in rows
        ]
