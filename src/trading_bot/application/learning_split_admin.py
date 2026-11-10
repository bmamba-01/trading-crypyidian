"""Quản trị metadata candidate và phê duyệt split bằng Principal reviewer."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.learning_split_approvals import (
    LearningSplitApprovalRepository,
)
from trading_bot.adapters.database.repositories.learning_split_candidates import (
    LearningSplitCandidateRepository,
    LearningSplitCandidateReview,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import Permission, Principal, authorize
from trading_bot.auth.service import RECENT_MFA_LIFETIME
from trading_bot.domain.learning.approval import LearningSplitApproval


class LearningSplitAdminService:
    """Tạo facade gắn quyền reviewer và session workspace."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    def for_principal(self, principal: Principal) -> PrincipalLearningSplitAdminService:
        if type(principal) is not Principal:
            raise TypeError("principal phải đến từ authentication boundary.")
        return PrincipalLearningSplitAdminService(self._sessions, principal)


class PrincipalLearningSplitAdminService:
    """Admin chỉ đọc preview safe và ghi approval append-only."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], principal: Principal) -> None:
        self._sessions = sessions
        self._principal = principal

    async def list_candidates(self) -> tuple[LearningSplitCandidateReview, ...]:
        self._require_review_permission()
        async with self._uow(read_only=True) as unit:
            return await LearningSplitCandidateRepository(unit.session).list_for_review(
                workspace_id=self._principal.workspace_id,
            )

    async def approve(
        self,
        *,
        candidate_id: UUID,
        reason: str,
        idempotency_key: UUID,
    ) -> LearningSplitApproval:
        self._require_recent_mfa()
        if type(candidate_id) is not UUID or type(idempotency_key) is not UUID:
            raise ValueError("candidate_id/idempotency_key phải là UUID.")
        if not isinstance(reason, str) or not 12 <= len(reason.strip()) <= 500:
            raise ValueError("Lý do phê duyệt cần 12–500 ký tự.")
        async with self._uow() as unit:
            candidate = await LearningSplitCandidateRepository(unit.session).get_for_review(
                workspace_id=self._principal.workspace_id,
                candidate_id=candidate_id,
            )
            if candidate is None:
                raise LookupError("Không tìm thấy candidate trong workspace.")
            approved_at = datetime.now(UTC)
            approval = LearningSplitApproval(
                workspace_id=self._principal.workspace_id,
                manifest_hash=candidate.manifest_hash,
                split_policy_version=candidate.split_policy_version,
                fold_plan_hash=candidate.fold_plan_hash,
                actor_membership_id=self._principal.membership_id,
                idempotency_key=idempotency_key,
                reason=reason.strip(),
                approved_at=approved_at,
                mfa_verified_at=self._principal.mfa_verified_at,
            )
            return await LearningSplitApprovalRepository(unit.session).append_preseal(
                approval=approval
            )

    def _require_review_permission(self) -> None:
        if not authorize(self._principal, Permission.LEARNING_SPLIT_APPROVE):
            raise PermissionError("Không có quyền xem hoặc duyệt split learning.")

    def _require_recent_mfa(self) -> datetime:
        self._require_review_permission()
        verified_at = self._principal.mfa_verified_at
        now = datetime.now(UTC)
        if (
            verified_at.tzinfo is None
            or verified_at.utcoffset() is None
            or verified_at > now
            or now - verified_at > RECENT_MFA_LIFETIME
        ):
            raise PermissionError("Cần xác thực MFA gần đây để duyệt split.")
        return verified_at

    def _uow(self, *, read_only: bool = False) -> UnitOfWork:
        return UnitOfWork(
            self._sessions,
            workspace_id=self._principal.workspace_id,
            actor_membership_id=self._principal.membership_id,
            snapshot_read_only=read_only,
        )


__all__ = ["LearningSplitAdminService", "PrincipalLearningSplitAdminService"]
