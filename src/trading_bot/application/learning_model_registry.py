"""Use-case registry mô hình với quyền và danh tính lấy từ Principal đã xác thực."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.learning_model_registry import (
    LearningModelRegistryRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import Permission, Principal, authorize
from trading_bot.auth.service import RECENT_MFA_LIFETIME
from trading_bot.domain.learning.model_registry import (
    LearningModelActivePointer,
    LearningModelRegistryEntry,
    LearningModelRegistryEvent,
)

POLICY_VERSION = "learning-model-governance-v1"
_REASON_CODE = re.compile(r"[a-z][a-z0-9_]{1,63}\Z")


class LearningModelRegistryService:
    """Tạo facade chỉ định workspace và actor từ danh tính đã xác thực."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    def for_principal(self, principal: Principal) -> PrincipalLearningModelRegistryService:
        if not isinstance(principal, Principal):
            raise TypeError("principal phải đến từ authentication boundary.")
        return PrincipalLearningModelRegistryService(self._sessions, principal)


class PrincipalLearningModelRegistryService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], principal: Principal) -> None:
        self._sessions = sessions
        self._principal = principal

    async def list_entries(self) -> tuple[LearningModelRegistryEntry, ...]:
        self._require(Permission.LEARNING_MODEL_READ)
        async with self._uow() as unit:
            return await LearningModelRegistryRepository(unit.session).list_entries(
                workspace_id=self._principal.workspace_id
            )

    async def get_entry(self, registry_id: UUID) -> LearningModelRegistryEntry:
        self._require(Permission.LEARNING_MODEL_READ)
        async with self._uow() as unit:
            return await LearningModelRegistryRepository(unit.session).get_entry(
                workspace_id=self._principal.workspace_id, registry_id=registry_id
            )

    async def list_events(self, registry_id: UUID) -> tuple[LearningModelRegistryEvent, ...]:
        self._require(Permission.LEARNING_MODEL_READ)
        async with self._uow() as unit:
            return await LearningModelRegistryRepository(unit.session).list_events(
                workspace_id=self._principal.workspace_id, registry_id=registry_id
            )

    async def get_active_pointer(self) -> LearningModelActivePointer:
        self._require(Permission.LEARNING_MODEL_READ)
        async with self._uow() as unit:
            return await LearningModelRegistryRepository(unit.session).get_active_pointer(
                workspace_id=self._principal.workspace_id
            )

    async def register_artifact(
        self, *, artifact_id: UUID, idempotency_key: UUID, reason: str
    ) -> LearningModelRegistryEntry:
        self._require(Permission.LEARNING_MODEL_WRITE)
        async with self._uow() as unit:
            return await LearningModelRegistryRepository(unit.session).register_artifact(
                workspace_id=self._principal.workspace_id,
                artifact_id=artifact_id,
                actor_membership_id=self._principal.membership_id,
                idempotency_key=idempotency_key,
                policy_version=POLICY_VERSION,
                reason=reason,
            )

    async def attach_evaluation(
        self,
        *,
        registry_id: UUID,
        evaluation_report_id: UUID,
        evaluation_report_hash: str,
        expected_revision: int,
        idempotency_key: UUID,
        reason: str,
    ) -> LearningModelRegistryEntry:
        self._require(Permission.LEARNING_MODEL_WRITE)
        async with self._uow() as unit:
            return await LearningModelRegistryRepository(unit.session).attach_evaluation(
                workspace_id=self._principal.workspace_id,
                registry_id=registry_id,
                evaluation_report_id=evaluation_report_id,
                evaluation_report_hash=evaluation_report_hash,
                actor_membership_id=self._principal.membership_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                policy_version=POLICY_VERSION,
                reason=reason,
            )

    async def approve(
        self,
        *,
        registry_id: UUID,
        expected_revision: int,
        idempotency_key: UUID,
        reason: str,
    ) -> LearningModelRegistryEntry:
        verified_at = self._require_recent_mfa(Permission.LEARNING_MODEL_APPROVE)
        async with self._uow() as unit:
            return await LearningModelRegistryRepository(unit.session).approve(
                workspace_id=self._principal.workspace_id,
                registry_id=registry_id,
                actor_membership_id=self._principal.membership_id,
                mfa_verified_at=verified_at,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                policy_version=POLICY_VERSION,
                reason=reason,
            )

    async def reject(
        self,
        *,
        registry_id: UUID,
        expected_revision: int,
        idempotency_key: UUID,
        reason_code: str,
        reason: str,
    ) -> LearningModelRegistryEntry:
        verified_at = self._require_recent_mfa(Permission.LEARNING_MODEL_APPROVE)
        if not isinstance(reason_code, str) or _REASON_CODE.fullmatch(reason_code) is None:
            raise ValueError("reason_code phải là mã ASCII chữ thường có dấu gạch dưới.")
        async with self._uow() as unit:
            return await LearningModelRegistryRepository(unit.session).reject(
                workspace_id=self._principal.workspace_id,
                registry_id=registry_id,
                actor_membership_id=self._principal.membership_id,
                mfa_verified_at=verified_at,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                policy_version=POLICY_VERSION,
                reason_code=reason_code,
                reason=reason,
            )

    async def activate(
        self,
        *,
        registry_id: UUID,
        expected_revision: int,
        expected_pointer_version: int,
        expected_current_registry_id: UUID | None,
        idempotency_key: UUID,
        reason: str,
    ) -> tuple[LearningModelRegistryEntry, LearningModelActivePointer]:
        verified_at = self._require_recent_mfa(Permission.LEARNING_MODEL_APPROVE)
        async with self._uow() as unit:
            return await LearningModelRegistryRepository(unit.session).activate(
                workspace_id=self._principal.workspace_id,
                registry_id=registry_id,
                actor_membership_id=self._principal.membership_id,
                mfa_verified_at=verified_at,
                expected_revision=expected_revision,
                expected_pointer_version=expected_pointer_version,
                expected_current_registry_id=expected_current_registry_id,
                idempotency_key=idempotency_key,
                policy_version=POLICY_VERSION,
                reason=reason,
            )

    async def rollback(
        self,
        *,
        registry_id: UUID,
        expected_revision: int,
        expected_pointer_version: int,
        expected_current_registry_id: UUID | None,
        idempotency_key: UUID,
        reason: str,
    ) -> tuple[LearningModelRegistryEntry, LearningModelActivePointer]:
        verified_at = self._require_recent_mfa(Permission.LEARNING_MODEL_APPROVE)
        async with self._uow() as unit:
            return await LearningModelRegistryRepository(unit.session).rollback(
                workspace_id=self._principal.workspace_id,
                registry_id=registry_id,
                actor_membership_id=self._principal.membership_id,
                mfa_verified_at=verified_at,
                expected_revision=expected_revision,
                expected_pointer_version=expected_pointer_version,
                expected_current_registry_id=expected_current_registry_id,
                idempotency_key=idempotency_key,
                policy_version=POLICY_VERSION,
                reason=reason,
            )

    async def retire(
        self,
        *,
        registry_id: UUID,
        expected_revision: int,
        idempotency_key: UUID,
        reason: str,
    ) -> LearningModelRegistryEntry:
        verified_at = self._require_recent_mfa(Permission.LEARNING_MODEL_APPROVE)
        async with self._uow() as unit:
            return await LearningModelRegistryRepository(unit.session).retire(
                workspace_id=self._principal.workspace_id,
                registry_id=registry_id,
                actor_membership_id=self._principal.membership_id,
                mfa_verified_at=verified_at,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                policy_version=POLICY_VERSION,
                reason=reason,
            )

    def _require(self, permission: Permission) -> None:
        if not authorize(self._principal, permission):
            raise PermissionError("Principal không có quyền quản lý model registry.")

    def _require_recent_mfa(self, permission: Permission) -> datetime:
        self._require(permission)
        verified_at = self._principal.mfa_verified_at
        now = datetime.now(UTC)
        if (
            not isinstance(verified_at, datetime)
            or verified_at.tzinfo is None
            or verified_at.utcoffset() is None
            or verified_at > now
            or now - verified_at > RECENT_MFA_LIFETIME
        ):
            raise PermissionError("Cần xác thực MFA gần đây để thay đổi registry.")
        return verified_at

    def _uow(self) -> UnitOfWork:
        return UnitOfWork(
            self._sessions,
            workspace_id=self._principal.workspace_id,
            actor_membership_id=self._principal.membership_id,
        )


__all__ = [
    "LearningModelRegistryService",
    "POLICY_VERSION",
    "PrincipalLearningModelRegistryService",
]
