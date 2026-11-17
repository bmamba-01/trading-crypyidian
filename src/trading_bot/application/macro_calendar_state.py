"""Đọc lịch vĩ mô đã lưu và tính trạng thái macro point-in-time cho snapshot quyết định (AI-4)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import MacroCalendarFetchRecord
from trading_bot.domain.research.macro_calendar import (
    MACRO_SOURCE_IDS,
    MacroCalendarFetch,
    MacroEvent,
    MacroState,
    macro_state,
)

# Release cần trạng thái macro. Chỉ nạp cho các release này để snapshot_id của chiến lược khác
# không đổi khi thêm nguồn lịch.
MACRO_AWARE_RELEASE_PREFIXES = ("h06-",)


def release_uses_macro(release_id: str) -> bool:
    return release_id.startswith(MACRO_AWARE_RELEASE_PREFIXES)


async def load_macro_fetches(
    session: AsyncSession, *, workspace_id: UUID, as_of: datetime
) -> list[MacroCalendarFetch]:
    """Lần tải gần nhất mỗi nguồn có ``fetched_at ≤ as_of`` (RLS theo workspace của session)."""
    fetches: list[MacroCalendarFetch] = []
    for source_id in MACRO_SOURCE_IDS:
        row = await session.scalar(
            select(MacroCalendarFetchRecord)
            .where(
                MacroCalendarFetchRecord.workspace_id == workspace_id,
                MacroCalendarFetchRecord.source_id == source_id,
                MacroCalendarFetchRecord.fetched_at <= as_of,
            )
            .order_by(MacroCalendarFetchRecord.fetched_at.desc())
            .limit(1)
        )
        if row is None:
            continue
        fetches.append(
            MacroCalendarFetch(
                fetch_id=f"macro:{row.id}",
                source_id=row.source_id,
                fetched_at=row.fetched_at,
                events=tuple(MacroEvent.from_payload(row.source_id, e) for e in row.events),
            )
        )
    return fetches


async def load_macro_state(
    session: AsyncSession, *, workspace_id: UUID, decision_time: datetime
) -> MacroState:
    fetches = await load_macro_fetches(session, workspace_id=workspace_id, as_of=decision_time)
    return macro_state(fetches, decision_time)


__all__ = [
    "MACRO_AWARE_RELEASE_PREFIXES",
    "load_macro_fetches",
    "load_macro_state",
    "release_uses_macro",
]
