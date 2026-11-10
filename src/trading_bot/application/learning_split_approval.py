"""Phê duyệt split/fold plan bằng danh tính admin đã xác thực và MFA gần đây."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from trading_bot.application.learning_fold_plan import build_manifest_fold_plan
from trading_bot.auth.policy import Permission, Principal, authorize
from trading_bot.auth.service import RECENT_MFA_LIFETIME
from trading_bot.domain.learning.approval import LearningSplitApproval
from trading_bot.domain.learning.dataset import DatasetManifest
from trading_bot.domain.learning.evaluation import WalkForwardSplit


@dataclass(frozen=True, slots=True)
class SplitApprovalRequest:
    """Manifest candidate và cửa sổ fold cần reviewer phê duyệt trước khi seal."""

    manifest: DatasetManifest
    folds: tuple[WalkForwardSplit, ...]
    evaluation_cutoff: datetime
    reason: str
    idempotency_key: UUID

    def __post_init__(self) -> None:
        if type(self.manifest) is not DatasetManifest:
            raise ValueError("manifest phải là DatasetManifest đã qua verifier.")
        if (
            type(self.folds) is not tuple
            or not self.folds
            or any(type(item) is not WalkForwardSplit for item in self.folds)
        ):
            raise ValueError("folds phải là tuple không rỗng gồm WalkForwardSplit.")
        if type(self.idempotency_key) is not UUID:
            raise ValueError("idempotency_key phải là UUID.")
        if (
            not isinstance(self.evaluation_cutoff, datetime)
            or self.evaluation_cutoff.tzinfo is None
            or self.evaluation_cutoff.utcoffset() is None
        ):
            raise ValueError("evaluation_cutoff phải có múi giờ.")
        if not isinstance(self.reason, str):
            raise ValueError("reason phải là chuỗi.")


class LearningSplitApprovalRepository(Protocol):
    async def append_preseal(self, *, approval: LearningSplitApproval) -> LearningSplitApproval: ...


class LearningSplitApprovalService:
    """Tạo approval chỉ từ fold plan dựng lại trên manifest candidate authoritative."""

    def __init__(self, repository: LearningSplitApprovalRepository) -> None:
        self._repository = repository

    def for_principal(self, principal: Principal) -> PrincipalLearningSplitApprovalService:
        if type(principal) is not Principal:
            raise TypeError("principal phải đến từ ranh giới xác thực.")
        return PrincipalLearningSplitApprovalService(self._repository, principal)


class PrincipalLearningSplitApprovalService:
    def __init__(self, repository: LearningSplitApprovalRepository, principal: Principal) -> None:
        self._repository = repository
        self._principal = principal

    async def approve(self, request: SplitApprovalRequest) -> LearningSplitApproval:
        """Ghi phê duyệt trước seal, khóa đúng manifest/policy/fold plan."""
        if type(request) is not SplitApprovalRequest:
            raise ValueError("Yêu cầu phê duyệt split không hợp lệ.")
        if not authorize(self._principal, Permission.LEARNING_SPLIT_APPROVE):
            raise PermissionError("Không có quyền phê duyệt split learning.")
        now = datetime.now(UTC)
        verified_at = self._principal.mfa_verified_at
        if (
            verified_at.tzinfo is None
            or verified_at.utcoffset() is None
            or verified_at > now
            or now - verified_at > RECENT_MFA_LIFETIME
        ):
            raise PermissionError("Cần xác thực MFA gần đây để phê duyệt split.")
        if request.manifest.workspace_id != self._principal.workspace_id:
            raise PermissionError("Manifest candidate nằm ngoài workspace hiện hành.")
        plan = build_manifest_fold_plan(
            request.manifest,
            folds=request.folds,
            expected_split_policy_version=request.manifest.split_policy_version,
            evaluation_cutoff=request.evaluation_cutoff,
        )
        approval = LearningSplitApproval(
            workspace_id=self._principal.workspace_id,
            manifest_hash=plan.manifest_hash,
            split_policy_version=plan.split_policy_version,
            fold_plan_hash=plan.content_hash,
            actor_membership_id=self._principal.membership_id,
            idempotency_key=request.idempotency_key,
            reason=request.reason,
            approved_at=now,
            mfa_verified_at=verified_at,
        )
        return await self._repository.append_preseal(approval=approval)


__all__ = [
    "LearningSplitApprovalService",
    "PrincipalLearningSplitApprovalService",
    "SplitApprovalRequest",
]
