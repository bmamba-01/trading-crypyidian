"""Đọc lần kiểm exchangeInfo đã lưu cho digest AI Analyst (AI-4 lát 3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, text
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


@dataclass(frozen=True, slots=True)
class EntryBlock:
    """Lý do không mở vị thế mới cho một symbol (mã ghi vào funnel dạng ``PLAN:<mã>``)."""

    symbol: str
    reason_code: str
    detail_vi: str


_SYMBOL_ENTRY = text(
    """
    SELECT (
        SELECT e FROM jsonb_array_elements(s.entries) AS e WHERE e->>'symbol' = :symbol LIMIT 1
    ) AS entry
    FROM listing_snapshot AS s
    WHERE s.workspace_id = :workspace_id AND s.changed AND s.fetched_at <= :as_of
    ORDER BY s.fetched_at DESC
    LIMIT 1
    """
)


async def symbol_entry_block(
    session: AsyncSession, *, workspace_id: UUID, symbol: str, as_of: datetime
) -> EntryBlock | None:
    """Chặn vào lệnh khi danh sách hợp đồng gần nhất (≤ ``as_of``) cho thấy symbol bị lên lịch gỡ,
    không còn TRADING hoặc biến mất.

    Chưa có lần kiểm nào (collector tắt) thì không chặn: thiếu dữ liệu niêm yết không được làm dừng
    mọi giao dịch. Trạng thái gỡ đã thấy thì không tự hết hạn theo tuổi dữ liệu.
    """
    row = (
        await session.execute(
            _SYMBOL_ENTRY, {"workspace_id": workspace_id, "symbol": symbol, "as_of": as_of}
        )
    ).first()
    if row is None:
        return None
    if row.entry is None:
        return EntryBlock(
            symbol, "SYMBOL_NOT_LISTED", "Symbol không còn trong danh sách hợp đồng Binance."
        )
    entry = ListingEntry.from_payload(row.entry)
    if entry.delist_scheduled and entry.delivery_at is not None:
        return EntryBlock(
            symbol,
            "SYMBOL_DELIST_SCHEDULED",
            f"Binance đã lên lịch gỡ hợp đồng lúc {entry.delivery_at.isoformat()}.",
        )
    if entry.status != "TRADING":
        return EntryBlock(
            symbol,
            "SYMBOL_NOT_TRADING",
            f"Hợp đồng đang ở trạng thái {entry.status}, không mở vị thế mới.",
        )
    return None


__all__ = ["EntryBlock", "load_listing_fetches", "symbol_entry_block"]
