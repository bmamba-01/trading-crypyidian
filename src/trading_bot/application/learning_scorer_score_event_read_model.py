"""Đọc lịch sử score event đã khử payload theo workspace cho admin web."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.learning_scorer_score_event_read_model import (
    LearningScorerScoreEventReadRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.learning.scorer_score_event_summary import ScorerScoreEventPage


class ScorerScoreEventReadModel:
    """Tạo projection workspace-scoped cho lịch sử score event."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def load_page(
        self,
        workspace_id: UUID,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> ScorerScoreEventPage:
        if type(workspace_id) is not UUID:
            raise ValueError("Workspace phải là UUID.")
        async with UnitOfWork(
            self._sessions,
            workspace_id=workspace_id,
            snapshot_read_only=True,
        ) as unit:
            return await LearningScorerScoreEventReadRepository(unit.session).get_page(
                workspace_id=workspace_id,
                page=page,
                page_size=page_size,
            )


__all__ = ["ScorerScoreEventReadModel"]
