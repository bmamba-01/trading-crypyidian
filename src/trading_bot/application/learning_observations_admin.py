"""Use case read-only cho observation, drift assessment và proposal metadata."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.learning_observations_admin import (
    LearningObservationAdminItem,
    LearningObservationsAdminRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import Permission, Principal, authorize


class LearningObservationsAdminService:
    """Tạo facade chỉ đọc, gắn workspace với Principal đã xác thực."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        unit_of_work_factory: Callable[..., UnitOfWork] = UnitOfWork,
        repository_factory: Callable[[AsyncSession], LearningObservationsAdminRepository] = (
            LearningObservationsAdminRepository
        ),
    ) -> None:
        if not callable(unit_of_work_factory) or not callable(repository_factory):
            raise ValueError("Factory UnitOfWork và repository phải gọi được.")
        self._sessions = sessions
        self._unit_of_work_factory = unit_of_work_factory
        self._repository_factory = repository_factory

    def for_principal(self, principal: Principal) -> PrincipalLearningObservationsAdminService:
        if type(principal) is not Principal:
            raise TypeError("principal phải đến từ authentication boundary.")
        if not authorize(principal, Permission.LEARNING_MODEL_READ):
            raise PermissionError("Không có quyền xem quan sát mô hình.")
        return PrincipalLearningObservationsAdminService(
            self._sessions,
            principal,
            unit_of_work_factory=self._unit_of_work_factory,
            repository_factory=self._repository_factory,
        )


class PrincipalLearningObservationsAdminService:
    """Facade truy vấn feed tối đa 50 hàng trong workspace của Principal."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        principal: Principal,
        *,
        unit_of_work_factory: Callable[..., UnitOfWork],
        repository_factory: Callable[[AsyncSession], LearningObservationsAdminRepository],
    ) -> None:
        self._sessions = sessions
        self._principal = principal
        self._unit_of_work_factory = unit_of_work_factory
        self._repository_factory = repository_factory

    async def list_recent(self, *, limit: int = 50) -> tuple[LearningObservationAdminItem, ...]:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit phải là số nguyên từ 1 đến 50.")
        async with self._unit_of_work_factory(
            self._sessions,
            workspace_id=self._principal.workspace_id,
            actor_membership_id=self._principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            return await self._repository_factory(unit.session).list_recent(
                workspace_id=self._principal.workspace_id,
                limit=limit,
            )


__all__ = [
    "LearningObservationsAdminService",
    "PrincipalLearningObservationsAdminService",
]
