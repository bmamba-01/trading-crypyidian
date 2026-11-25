"""Ca sử dụng đọc aggregate latency cho model card scorer."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.bot_grants import BotGrantRepository
from trading_bot.adapters.database.repositories.learning_scorer_model_card import (
    LearningScorerModelCardRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Principal
from trading_bot.domain.learning.scorer_model_card import ScorerActionLatencySummary
from trading_bot.domain.learning.scorer_report_summary import ScorerReportSummary


class ScorerModelCardReadModel:
    """Đọc latency PAPER trong workspace, gắn với report scorer typed."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def load_action_latency(
        self,
        *,
        principal: Principal,
        bot_id: UUID,
        report: ScorerReportSummary,
        window_code: str,
    ) -> ScorerActionLatencySummary:
        """Ủy quyền aggregate cho SQL gateway bằng hash của report đã kiểm quyền."""
        if type(principal) is not Principal or type(bot_id) is not UUID:
            raise ValueError("Principal và bot phải đến từ ranh giới đã xác thực.")
        if type(report) is not ScorerReportSummary:
            raise ValueError("Cần report scorer đã được kiểm quyền và parse typed.")
        async with UnitOfWork(
            self._sessions,
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
        ) as unit:
            await BotGrantRepository(unit.session).require(
                principal,
                bot_id=bot_id,
                permission=BotPermission.REPORT_READ,
            )
            return await LearningScorerModelCardRepository(unit.session).get_action_latency(
                workspace_id=principal.workspace_id,
                bot_id=bot_id,
                report_id=report.evaluation_report_id,
                artifact_hash=report.scorer_artifact_hash,
                report_hash=report.report_hash,
                base_artifact_hash=report.base_artifact_hash,
                policy_hash=report.policy_hash,
                window_code=window_code,
            )


__all__ = ["ScorerModelCardReadModel"]
