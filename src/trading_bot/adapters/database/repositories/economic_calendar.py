"""Repository lịch sự kiện kinh tế (CK-10); RLS theo workspace, không có DELETE vật lý."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.calendar_entities import EconomicEventRecord
from trading_bot.domain.market.economic_calendar import EconomicEventInput


def event_dict(row: EconomicEventRecord) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "title": row.title,
        "region": row.region,
        "impact": row.impact,
        "event_at": row.event_at.isoformat(),
        "note": row.note,
        "revision": row.revision,
    }


class EconomicCalendarRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_between(
        self, *, workspace_id: UUID, start: datetime, end: datetime, limit: int = 200
    ) -> list[dict[str, Any]]:
        rows = await self._session.scalars(
            select(EconomicEventRecord)
            .where(
                EconomicEventRecord.workspace_id == workspace_id,
                EconomicEventRecord.deleted_at.is_(None),
                EconomicEventRecord.event_at >= start,
                EconomicEventRecord.event_at < end,
            )
            .order_by(EconomicEventRecord.event_at, EconomicEventRecord.id)
            .limit(limit)
        )
        return [event_dict(row) for row in rows.all()]

    async def get_for_update(
        self, *, workspace_id: UUID, event_id: UUID
    ) -> EconomicEventRecord | None:
        return await self._session.scalar(
            select(EconomicEventRecord)
            .where(
                EconomicEventRecord.workspace_id == workspace_id,
                EconomicEventRecord.id == event_id,
                EconomicEventRecord.deleted_at.is_(None),
            )
            .with_for_update()
        )

    async def create(
        self, *, workspace_id: UUID, membership_id: UUID, data: EconomicEventInput
    ) -> EconomicEventRecord:
        row = EconomicEventRecord(
            workspace_id=workspace_id,
            title=data.title,
            region=data.region,
            impact=data.impact,
            event_at=data.event_at,
            note=data.note,
            created_by_membership_id=membership_id,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def update(self, row: EconomicEventRecord, data: EconomicEventInput) -> None:
        row.title = data.title
        row.region = data.region
        row.impact = data.impact
        row.event_at = data.event_at
        row.note = data.note
        row.revision = row.revision + 1
        row.updated_at = func.now()
        await self._session.flush()

    async def soft_delete(self, row: EconomicEventRecord) -> None:
        row.deleted_at = func.now()
        row.revision = row.revision + 1
        await self._session.flush()
