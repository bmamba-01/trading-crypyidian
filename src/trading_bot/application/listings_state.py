"""Đọc lần kiểm exchangeInfo đã lưu cho digest AI Analyst (AI-4 lát 3)."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import ListingSnapshotRecord
from trading_bot.domain.research.listings import RECENT_WINDOW, ListingEntry, ListingsFetch


def _fetch(row: ListingSnapshotRecord) -> ListingsFetch:
    return ListingsFetch(
        fetch_id=f"listing:{row.id}",
        fetched_at=row.fetched_at,
        entries=tuple(ListingEntry.from_payload(item) for item in row.entries or ()),
    )


async def load_listing_fetches(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    as_of: datetime,
    window: timedelta = RECENT_WINDOW,
) -> tuple[list[ListingsFetch], datetime | None]:
    """Lần tải có nội dung trong ``[as_of − window, as_of]`` + lần ngay trước đó (để so sánh), và
    mốc kiểm gần nhất ≤ ``as_of`` (kể cả lần không đổi)."""
    base = select(ListingSnapshotRecord).where(
        ListingSnapshotRecord.workspace_id == workspace_id,
        ListingSnapshotRecord.fetched_at <= as_of,
        ListingSnapshotRecord.changed.is_(True),
    )
    recent = list(
        await session.scalars(
            base.where(ListingSnapshotRecord.fetched_at >= as_of - window).order_by(
                ListingSnapshotRecord.fetched_at
            )
        )
    )
    before = await session.scalar(
        base.where(ListingSnapshotRecord.fetched_at < as_of - window)
        .order_by(ListingSnapshotRecord.fetched_at.desc())
        .limit(1)
    )
    last_checked = await session.scalar(
        select(func.max(ListingSnapshotRecord.fetched_at)).where(
            ListingSnapshotRecord.workspace_id == workspace_id,
            ListingSnapshotRecord.fetched_at <= as_of,
        )
    )
    rows = ([before] if before is not None else []) + recent
    return [_fetch(row) for row in rows], last_checked


__all__ = ["load_listing_fetches"]
