"""Đọc report scorer đã lưu theo workspace cho admin web."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.learning_scorer_report_read_model import (
    LearningScorerReportReadRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.learning.scorer_report_summary import ScorerReportSummary


class ScorerReportReadModel:
    """Tạo projection đọc-only, workspace-scoped cho báo cáo TEST mới nhất."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def load_latest_summary(self, workspace_id: UUID) -> ScorerReportSummary | None:
        if type(workspace_id) is not UUID:
            raise ValueError("Workspace phải là UUID.")
        async with UnitOfWork(
            self._sessions,
            workspace_id=workspace_id,
        ) as unit:
            return await LearningScorerReportReadRepository(unit.session).get_latest(
                workspace_id=workspace_id
            )

    async def load_report_summary(
        self, workspace_id: UUID, report_id: UUID
    ) -> ScorerReportSummary | None:
        """Đọc report scorer theo ID cụ thể sau current-rights recheck."""
        if type(workspace_id) is not UUID or type(report_id) is not UUID:
            raise ValueError("Workspace và report phải là UUID.")
        async with UnitOfWork(
            self._sessions,
            workspace_id=workspace_id,
        ) as unit:
            return await LearningScorerReportReadRepository(unit.session).get_by_id(
                workspace_id=workspace_id,
                report_id=report_id,
            )


__all__ = ["ScorerReportReadModel"]
