"""Đọc point-in-time mức funding đã chốt và lịch mốc funding cho runtime PAPER (FX-3 lát 2).

- Mức đã chốt: ``market_funding`` do ``FundingRateCollector`` ghi từ
  ``GET /fapi/v1/fundingRate`` (lát 1). Không dùng ``lastFundingRate`` của premiumIndex.
- Lịch mốc: ``next_funding_time`` trong ``market_mark_price`` (stream ``markPrice@1s``). Mốc luôn
  lấy từ dữ liệu, không tự suy từ đồng hồ: có symbol dùng chu kỳ khác 8 giờ.

Mọi truy vấn chỉ thấy bản ghi có ``available_at <= now`` (đồng hồ DB của tick).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.market_entities import (
    MarketFunding,
    MarketInstrument,
    MarketMarkPrice,
)

FUNDING_RECORD_SOURCE = "BINANCE_USDM_REST"
# Giới hạn số mốc đọc mỗi lần: chu kỳ ngắn nhất 1 giờ, tick chỉ cần vài mốc gần nhất.
MAX_FUNDING_MOMENTS = 32


@dataclass(frozen=True, slots=True)
class SettledFundingRow:
    """Bản ghi funding đã chốt (bản revision mới nhất đã available tại ``now``)."""

    funding_time: datetime
    funding_rate: Decimal
    mark_price: Decimal | None


class FundingSettlementRepository:
    """Chỉ đọc; session do caller quản lý (UoW tenant-scoped)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def settled_after(
        self,
        *,
        workspace_id: UUID,
        symbol: str,
        after: datetime,
        now: datetime,
        limit: int = MAX_FUNDING_MOMENTS,
    ) -> tuple[SettledFundingRow, ...]:
        """Kỳ funding đã chốt có ``after < funding_time <= now``, tăng dần, một dòng mỗi mốc."""
        rows = await self._session.execute(
            select(MarketFunding.funding_time, MarketFunding.funding_rate, MarketFunding.mark_price)
            .join(
                MarketInstrument,
                (MarketInstrument.workspace_id == MarketFunding.workspace_id)
                & (MarketInstrument.id == MarketFunding.instrument_id),
            )
            .where(
                MarketFunding.workspace_id == workspace_id,
                MarketInstrument.symbol == symbol,
                MarketFunding.source == FUNDING_RECORD_SOURCE,
                MarketFunding.funding_time > after,
                MarketFunding.funding_time <= now,
                MarketFunding.available_at <= now,
            )
            .ext(distinct_on(MarketFunding.funding_time))
            .order_by(
                MarketFunding.funding_time,
                MarketFunding.revision.desc(),
                MarketFunding.available_at.desc(),
            )
            .limit(limit)
        )
        return tuple(
            SettledFundingRow(funding_time=time, funding_rate=rate, mark_price=mark)
            for time, rate, mark in rows.all()
        )

    async def scheduled_after(
        self,
        *,
        workspace_id: UUID,
        symbol: str,
        after: datetime,
        now: datetime,
        limit: int = MAX_FUNDING_MOMENTS,
    ) -> tuple[datetime, ...]:
        """Các mốc funding đã qua (``after < mốc <= now``) theo lịch của mark price.

        Mẫu mark price đầu tiên quan sát từ ``after`` trở đi cho biết mốc kế tiếp sau ``after``;
        lặp từ mốc vừa tìm. Mỗi bước là một truy vấn ``LIMIT 1`` theo index ``event_at`` nên không
        quét cả ngày mẫu 1 giây.
        """
        moments: list[datetime] = []
        lower = after
        while len(moments) < limit:
            moment = await self._session.scalar(
                select(MarketMarkPrice.next_funding_time)
                .join(
                    MarketInstrument,
                    (MarketInstrument.workspace_id == MarketMarkPrice.workspace_id)
                    & (MarketInstrument.id == MarketMarkPrice.instrument_id),
                )
                .where(
                    MarketMarkPrice.workspace_id == workspace_id,
                    MarketInstrument.symbol == symbol,
                    MarketMarkPrice.event_at >= lower,
                    MarketMarkPrice.event_at <= now,
                    MarketMarkPrice.available_at <= now,
                    MarketMarkPrice.next_funding_time > lower,
                )
                .order_by(MarketMarkPrice.event_at, MarketMarkPrice.next_funding_time)
                .limit(1)
            )
            if not isinstance(moment, datetime) or moment > now:
                break
            moments.append(moment)
            lower = moment
        return tuple(moments)
