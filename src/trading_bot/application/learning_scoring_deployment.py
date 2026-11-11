"""Use-case deployment scorer với actor, workspace và MFA lấy từ Principal."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.learning_scoring_deployments import (
    LearningScoringDeploymentRepository,
    ScorerDeploymentRightsUnavailable,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.scorer_runtime_policy import ScorerRuntimePolicyProvider
from trading_bot.auth.policy import MfaMethod, Permission, Principal, authorize
from trading_bot.auth.service import RECENT_MFA_LIFETIME
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentAdminSnapshot,
    ScorerDeploymentEvent,
    ScorerDeploymentMode,
    ScorerDeploymentSnapshot,
)


class ScorerDeploymentService:
    """Tạo facade cố định danh tính cho từng request đã xác thực."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        policy_provider: ScorerRuntimePolicyProvider | None = None,
    ) -> None:
        self._sessions = sessions
        self._policy_provider = policy_provider

    def for_principal(self, principal: Principal) -> PrincipalScorerDeploymentService:
        if not isinstance(principal, Principal):
            raise TypeError("principal phải đến từ authentication boundary.")
        return PrincipalScorerDeploymentService(
            self._sessions, principal, policy_provider=self._policy_provider
        )


class PrincipalScorerDeploymentService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        principal: Principal,
        *,
        policy_provider: ScorerRuntimePolicyProvider | None = None,
    ) -> None:
        self._sessions = sessions
        self._principal = principal
        self._policy_provider = policy_provider

    async def load_active_scoring_deployment(self) -> ScorerDeploymentSnapshot:
        self._require(Permission.LEARNING_MODEL_READ)
        async with self._uow() as unit:
            return await LearningScoringDeploymentRepository(
                unit.session
            ).load_active_scoring_deployment(self._principal.workspace_id)

    async def load_admin_scoring_deployment(self) -> ScorerDeploymentAdminSnapshot:
        self._require(Permission.LEARNING_MODEL_READ)
        async with self._uow() as unit:
            return await LearningScoringDeploymentRepository(
                unit.session
            ).load_admin_scoring_deployment(self._principal.workspace_id)

    async def set_mode(
        self,
        *,
        mode: ScorerDeploymentMode,
        evaluation_report_id: UUID | None = None,
        expected_pointer_version: int,
        expected_deployment_revision: int,
        idempotency_key: UUID,
        reason: str,
    ) -> ScorerDeploymentEvent:
        verified_at = self._require_recent_mfa(Permission.LEARNING_MODEL_APPROVE)
        if type(mode) is not ScorerDeploymentMode:
            raise ValueError("mode không hợp lệ.")
        if mode is ScorerDeploymentMode.DISABLED:
            if evaluation_report_id is not None:
                raise ValueError("DISABLED không nhận evaluation_report_id.")
            async with self._uow() as unit:
                return await LearningScoringDeploymentRepository(
                    unit.session
                ).append_disabled_event(
                    workspace_id=self._principal.workspace_id,
                    actor_membership_id=self._principal.membership_id,
                    mfa_verified_at=verified_at,
                    expected_pointer_version=expected_pointer_version,
                    expected_deployment_revision=expected_deployment_revision,
                    idempotency_key=idempotency_key,
                    reason=reason,
                )
        if evaluation_report_id is None:
            raise ValueError("SHADOW/PAPER_GATE cần evaluation_report_id đã lưu.")
        policy_provider = self._policy_provider
        if policy_provider is None:
            raise ScorerDeploymentRightsUnavailable(
                "Scorer rights gateway chưa nhận policy runtime đã cấu hình."
            )
        async with self._uow() as unit:
            return await LearningScoringDeploymentRepository(unit.session).append_active_event(
                workspace_id=self._principal.workspace_id,
                actor_membership_id=self._principal.membership_id,
                mfa_verified_at=verified_at,
                mode=mode,
                evaluation_report_id=evaluation_report_id,
                expected_pointer_version=expected_pointer_version,
                expected_deployment_revision=expected_deployment_revision,
                idempotency_key=idempotency_key,
                reason=reason,
                policy_provider=policy_provider,
            )

    def _require(self, permission: Permission) -> None:
        if not authorize(self._principal, permission):
            raise PermissionError("Principal không có quyền quản lý scorer deployment.")

    def _require_recent_mfa(self, permission: Permission) -> datetime:
        self._require(permission)
        principal = self._principal
        verified_at = principal.mfa_verified_at
        now = datetime.now(UTC)
        if (
            principal.mfa_method is not MfaMethod.TOTP
            or not isinstance(verified_at, datetime)
            or verified_at.tzinfo is None
            or verified_at.utcoffset() is None
            or verified_at > now
            or now - verified_at > RECENT_MFA_LIFETIME
        ):
            raise PermissionError("Cần xác thực TOTP gần đây để đổi scorer deployment.")
        return verified_at

    def _uow(self) -> UnitOfWork:
        return UnitOfWork(
            self._sessions,
            workspace_id=self._principal.workspace_id,
            actor_membership_id=self._principal.membership_id,
        )


__all__ = ["PrincipalScorerDeploymentService", "ScorerDeploymentService"]
