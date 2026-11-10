from typing import Any
from uuid import UUID

from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import TradingAccount, Workspace
from trading_bot.adapters.database.repositories.account_equity import AccountEquityRepository
from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
    BacktestDatasetManifestRepository,
)
from trading_bot.adapters.database.repositories.dataset_manifest import DatasetManifestRepository
from trading_bot.adapters.database.repositories.learning_attempts import (
    LearningAttemptConflict,
    LearningAttemptRepository,
    LearningAttemptWriteResult,
)
from trading_bot.adapters.database.repositories.learning_evaluation_reports import (
    LearningEvaluationReportConflict,
    LearningEvaluationReportRepository,
    LearningEvaluationReportWriteResult,
)
from trading_bot.adapters.database.repositories.learning_fold_samples import (
    LearningFoldSampleRepository,
    NormalizedLearningDatasetConflict,
    NormalizedLearningDatasetWriteResult,
)
from trading_bot.adapters.database.repositories.learning_scorer_artifacts import (
    LearningScorerArtifactConflict,
    LearningScorerArtifactRepository,
)
from trading_bot.adapters.database.repositories.learning_split_approvals import (
    LearningSplitApprovalRepository,
)
from trading_bot.adapters.database.repositories.learning_split_candidates import (
    LearningSplitCandidateRepository,
)
from trading_bot.adapters.database.repositories.learning_training_artifacts import (
    LearningTrainingArtifactConflict,
    LearningTrainingArtifactRepository,
    LearningTrainingArtifactWriteResult,
)
from trading_bot.adapters.database.repositories.learning_training_jobs import (
    LearningTrainingJobConflict,
    LearningTrainingJobRepository,
)
from trading_bot.adapters.database.repositories.risk_conversion import RiskConversionRepository
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository

__all__ = [
    "AccountEquityRepository",
    "BacktestDatasetManifestRepository",
    "DatasetManifestRepository",
    "LearningSplitApprovalRepository",
    "LearningSplitCandidateRepository",
    "LearningFoldSampleRepository",
    "NormalizedLearningDatasetConflict",
    "NormalizedLearningDatasetWriteResult",
    "LearningEvaluationReportConflict",
    "LearningEvaluationReportRepository",
    "LearningAttemptConflict",
    "LearningAttemptRepository",
    "LearningAttemptWriteResult",
    "LearningTrainingArtifactConflict",
    "LearningTrainingArtifactRepository",
    "LearningTrainingArtifactWriteResult",
    "LearningScorerArtifactConflict",
    "LearningScorerArtifactRepository",
    "LearningScorerTestAccessRepository",
    "LearningScorerTestEvaluationReportRepository",
    "LearningScorerTestOpportunityProofConflict",
    "LearningScorerTestOpportunityProofRepository",
    "LearningScorerTestOpportunityProofWriteResult",
    "LearningTrainingJobConflict",
    "LearningTrainingJobRepository",
    "LearningEvaluationReportWriteResult",
    "RiskConversionRepository",
    "SourceRegistryRepository",
    "WorkspaceRepository",
]


def __getattr__(name: str) -> Any:
    """Carrega repositories do scorer sob demanda para tránh chu trình import."""
    if name in {
        "LearningScorerTestAccessRepository",
        "LearningScorerTestEvaluationReportRepository",
    }:
        from trading_bot.adapters.database.repositories import learning_scorer_test_access

        return getattr(learning_scorer_test_access, name)
    if name in {
        "LearningScorerTestOpportunityProofConflict",
        "LearningScorerTestOpportunityProofRepository",
        "LearningScorerTestOpportunityProofWriteResult",
    }:
        from trading_bot.adapters.database.repositories import (
            learning_scorer_test_opportunity_proofs,
        )

        return getattr(learning_scorer_test_opportunity_proofs, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class WorkspaceRepository:
    """Truy vấn repository luôn giới hạn dữ liệu theo workspace được gọi."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def trading_accounts_statement(workspace_id: UUID) -> Select[TradingAccount]:
        return select(TradingAccount).where(TradingAccount.workspace_id == workspace_id)

    async def get_workspace(self, workspace_id: UUID) -> Workspace | None:
        statement = select(Workspace).where(Workspace.id == workspace_id)
        return await self._session.scalar(statement)

    async def list_trading_accounts(self, workspace_id: UUID) -> list[TradingAccount]:
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        result = await self._session.execute(self.trading_accounts_statement(workspace_id))
        return list(result.scalars().all())
