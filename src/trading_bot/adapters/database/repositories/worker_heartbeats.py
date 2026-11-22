"""Kho trạng thái heartbeat ingestion-worker theo workspace."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import WorkerHeartbeat


class WorkerHeartbeatRepository:
    """Lưu lần heartbeat mới nhất cho từng worker và process instance."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_many(
        self,
        *,
        workspace_id: UUID,
        instance_id: UUID,
        snapshots: Sequence[Mapping[str, Any]],
    ) -> None:
        if not snapshots:
            return
        values = [
            {
                "workspace_id": workspace_id,
                "component": str(snapshot["component"]),
                "instance_id": instance_id,
                "state": str(snapshot["state"]),
                "reason_code": snapshot.get("reason_code"),
                "detail_vi": snapshot.get("detail_vi"),
                "heartbeat_at": snapshot["heartbeat_at"],
                "last_tick_at": snapshot.get("last_tick_at"),
                "counters": dict(snapshot.get("counters") or {}),
            }
            for snapshot in snapshots
        ]
        statement = postgresql_insert(WorkerHeartbeat).values(values)
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=["workspace_id", "component", "instance_id"],
                set_={
                    "state": statement.excluded.state,
                    "reason_code": statement.excluded.reason_code,
                    "detail_vi": statement.excluded.detail_vi,
                    "heartbeat_at": statement.excluded.heartbeat_at,
                    "last_tick_at": statement.excluded.last_tick_at,
                    "counters": statement.excluded.counters,
                },
            )
        )

    async def latest_by_component(self, *, workspace_id: UUID) -> list[dict[str, Any]]:
        result = await self._session.scalars(
            select(WorkerHeartbeat)
            .where(WorkerHeartbeat.workspace_id == workspace_id)
            .order_by(
                WorkerHeartbeat.component.asc(),
                WorkerHeartbeat.heartbeat_at.desc(),
                WorkerHeartbeat.instance_id.asc(),
            )
        )
        latest: dict[str, WorkerHeartbeat] = {}
        for row in result:
            latest.setdefault(row.component, row)
        return [
            {
                "component": row.component,
                "state": row.state,
                "reason_code": row.reason_code,
                "detail_vi": row.detail_vi,
                "heartbeat_at": row.heartbeat_at,
                "last_tick_at": row.last_tick_at,
                "metadata": dict(row.counters),
            }
            for row in latest.values()
        ]
