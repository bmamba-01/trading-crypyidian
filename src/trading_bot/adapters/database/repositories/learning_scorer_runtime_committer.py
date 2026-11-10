"""Fail-closed scorer resolver used while a bot decision is being committed."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import LearningScoringDeploymentEventRecord
from trading_bot.adapters.database.repositories.bot_decisions import BotDecisionScoringCommit
from trading_bot.adapters.database.repositories.learning_scorer_current_rights import (
    LearningScorerCurrentRightsRepository,
)
from trading_bot.adapters.database.repositories.learning_scoring_deployments import (
    LearningScoringDeploymentRepository,
    ScorerDeploymentRightsUnavailable,
)
from trading_bot.application.bot_dispatcher import (
    BotDecision,
    BotDecisionEvaluation,
    BotDecisionJob,
)
from trading_bot.domain.learning.scoring_deployment import ScorerDeploymentMode


class PostgresScorerRuntimeCommitter:
    """Revalidate active rights under the decision UoW and keep DISABLED inert.

    C4 currently has no approved active report or runtime artifact/policy loader. An active
    pointer is therefore checked through the authoritative current-rights gateway and then
    rejected; no caller metadata can manufacture an approval or score event.
    """

    async def __call__(
        self,
        session: AsyncSession,
        job: BotDecisionJob,
        evaluation: BotDecisionEvaluation,
        _incumbent: BotDecision,
    ) -> BotDecisionScoringCommit | None:
        deployment = await LearningScoringDeploymentRepository(
            session
        ).load_admin_scoring_deployment(job.workspace_id)
        if deployment.mode is ScorerDeploymentMode.DISABLED:
            return None
        if deployment.deployment_event_id is None:
            raise ScorerDeploymentRightsUnavailable(
                "Active scorer pointer không có deployment event authoritative."
            )

        event_result = await session.execute(
            select(
                LearningScoringDeploymentEventRecord.workspace_id,
                LearningScoringDeploymentEventRecord.id,
                LearningScoringDeploymentEventRecord.mode,
                LearningScoringDeploymentEventRecord.revision,
                LearningScoringDeploymentEventRecord.scorer_artifact_id,
                LearningScoringDeploymentEventRecord.evaluation_report_id,
            ).where(
                LearningScoringDeploymentEventRecord.workspace_id == job.workspace_id,
                LearningScoringDeploymentEventRecord.id == deployment.deployment_event_id,
            )
        )
        event = event_result.mappings().one_or_none()
        if (
            event is None
            or event["workspace_id"] != job.workspace_id
            or event["mode"] != deployment.mode.value
            or event["revision"] != deployment.deployment_revision
            or event["scorer_artifact_id"] is None
            or event["evaluation_report_id"] is None
        ):
            raise ScorerDeploymentRightsUnavailable(
                "Deployment scorer không khớp projection hiện hành."
            )

        rights = await LearningScorerCurrentRightsRepository(session).verify_deployment_rights(
            workspace_id=job.workspace_id,
            scorer_artifact_id=event["scorer_artifact_id"],
            evaluation_report_id=event["evaluation_report_id"],
        )
        mode_eligible = (
            rights.shadow_eligible
            if deployment.mode is ScorerDeploymentMode.SHADOW
            else rights.paper_gate_eligible
        )
        if not mode_eligible:
            raise ScorerDeploymentRightsUnavailable(
                "Report scorer hoặc quyền nguồn hiện hành không cho phép mode đã bật."
            )
        if evaluation.snapshot is None or evaluation.vector is None:
            raise ScorerDeploymentRightsUnavailable(
                "Candidate không có vector canonical từ DecisionSnapshot strategy."
            )

        # Rights alone do not authenticate the runtime weights or policy. Until a repository
        # loads and verifies those exact artifacts, fail closed and leave the job pending.
        raise ScorerDeploymentRightsUnavailable(
            "Runtime artifact/policy resolver chưa được triển khai; không ghi score event."
        )


__all__ = ["PostgresScorerRuntimeCommitter"]
