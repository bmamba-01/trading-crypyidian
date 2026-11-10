"""Cấp input trainer sau khi transaction ghi event và receipt đã commit."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.learning_training_batches import (
    LearningTrainingBatchRepository,
)
from trading_bot.application.learning_training_batch import AuthorizedTrainerBatch
from trading_bot.application.outbox import UnitOfWork


@dataclass(frozen=True, slots=True)
class LearningTrainingInputRequest:
    """Định danh attempt mà worker đã nhận từ hàng đợi tin cậy."""

    workspace_id: UUID
    attempt_id: UUID

    def __post_init__(self) -> None:
        if type(self.workspace_id) is not UUID or type(self.attempt_id) is not UUID:
            raise ValueError("workspace_id và attempt_id phải là UUID.")


class LearningTrainingInputService:
    """Commit access receipt trước khi trả dữ liệu train/validation cho worker."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def claim_batch(
        self,
        request: LearningTrainingInputRequest,
    ) -> AuthorizedTrainerBatch:
        """Khóa attempt và chỉ trả batch sau khi UoW commit thành công."""
        if type(request) is not LearningTrainingInputRequest:
            raise ValueError("Yêu cầu batch trainer không hợp lệ.")
        async with UnitOfWork(
            self._sessions,
            workspace_id=request.workspace_id,
        ) as uow:
            batch = await LearningTrainingBatchRepository(uow.session).claim_batch(
                workspace_id=request.workspace_id,
                attempt_id=request.attempt_id,
            )
        return batch


__all__ = ["LearningTrainingInputRequest", "LearningTrainingInputService"]
