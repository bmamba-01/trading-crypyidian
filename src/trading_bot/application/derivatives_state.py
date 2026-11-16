"""Đọc ảnh chụp phái sinh đã lưu cho digest AI Analyst (AI-4 lát 2)."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import DerivativesSnapshotRecord
from trading_bot.domain.research.derivatives import DIGEST_MAX_AGE, DerivativesSnapshot

_FIELDS = tuple(DerivativesSnapshot.__slots__)


async def load_derivatives_snapshots(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    as_of: datetime,
    max_age: timedelta = DIGEST_MAX_AGE,
) -> list[DerivativesSnapshot]:
    """Ảnh chụp có ``as_of − max_age ≤ collected_at ≤ as_of`` (RLS theo workspace của session)."""
    rows = await session.scalars(
        select(DerivativesSnapshotRecord).where(
            DerivativesSnapshotRecord.workspace_id == workspace_id,
            DerivativesSnapshotRecord.collected_at <= as_of,
            DerivativesSnapshotRecord.collected_at >= as_of - max_age,
        )
    )
    return [DerivativesSnapshot(**{name: getattr(row, name) for name in _FIELDS}) for row in rows]


__all__ = ["load_derivatives_snapshots"]
