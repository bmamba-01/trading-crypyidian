"""Kho approval append-only, tenant scoped và chỉ chấp nhận phê duyệt trước seal."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.learning_entities import (
    DatasetManifestRecord,
    LearningSplitApprovalRecord,
)
from trading_bot.domain.learning.approval import LearningSplitApproval


class LearningSplitApprovalConflict(RuntimeError):
    """Approval đã tồn tại với payload khác hoặc manifest đã được seal trước đó."""


class LearningSplitApprovalRepository:
    """Mỗi phương thức dùng chung transaction/workspace của UnitOfWork."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_preseal(self, *, approval: LearningSplitApproval) -> LearningSplitApproval:
        """Ghi approval trước khi manifest cùng hash được niêm phong."""
        if type(approval) is not LearningSplitApproval:
            raise ValueError("approval phải là LearningSplitApproval.")
        await self._bind_workspace(approval.workspace_id)
        await self._lock(approval.workspace_id, approval.manifest_hash)
        existing_key = await self._session.scalar(
            select(LearningSplitApprovalRecord).where(
                LearningSplitApprovalRecord.workspace_id == approval.workspace_id,
                LearningSplitApprovalRecord.idempotency_key == approval.idempotency_key,
            )
        )
        if existing_key is not None:
            existing = existing_key.to_domain()
            if not _same_approval_intent(existing, approval):
                raise LearningSplitApprovalConflict("idempotency key đã gắn payload approval khác.")
            return existing

        existing_manifest_approval = await self._session.scalar(
            select(LearningSplitApprovalRecord).where(
                LearningSplitApprovalRecord.workspace_id == approval.workspace_id,
                LearningSplitApprovalRecord.manifest_hash == approval.manifest_hash,
            )
        )
        if existing_manifest_approval is not None:
            raise LearningSplitApprovalConflict("manifest đã có approval bất biến.")

        sealed = await self._session.scalar(
            select(DatasetManifestRecord.id).where(
                DatasetManifestRecord.workspace_id == approval.workspace_id,
                DatasetManifestRecord.manifest_hash == approval.manifest_hash,
            )
        )
        if sealed is not None:
            raise LearningSplitApprovalConflict("Không thể phê duyệt manifest đã seal.")

        row = LearningSplitApprovalRecord.from_domain(approval)
        row.id = uuid4()
        self._session.add(row)
        await self._session.flush()
        return row.to_domain()

    async def get_for_training_seal(
        self,
        *,
        workspace_id: UUID,
        manifest_hash: str,
        split_policy_version: str,
        fold_plan_hash: str,
    ) -> LearningSplitApproval | None:
        """Tìm approval exact-match để manifest repository có thể khóa training seal."""
        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        await self._bind_workspace(workspace_id)
        row = await self._session.scalar(
            select(LearningSplitApprovalRecord).where(
                LearningSplitApprovalRecord.workspace_id == workspace_id,
                LearningSplitApprovalRecord.manifest_hash == manifest_hash,
                LearningSplitApprovalRecord.split_policy_version == split_policy_version,
                LearningSplitApprovalRecord.fold_plan_hash == fold_plan_hash,
            )
        )
        return row.to_domain() if row is not None else None

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )

    async def _lock(self, workspace_id: UUID, manifest_hash: str) -> None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"dataset-manifest:{workspace_id}:{manifest_hash}"},
        )


def _same_approval_intent(stored: LearningSplitApproval, requested: LearningSplitApproval) -> bool:
    """Retry giữ nguyên nội dung duyệt nhưng có thể mang bằng chứng MFA mới hơn."""
    return (
        stored.workspace_id == requested.workspace_id
        and stored.actor_membership_id == requested.actor_membership_id
        and stored.idempotency_key == requested.idempotency_key
        and stored.manifest_hash == requested.manifest_hash
        and stored.split_policy_version == requested.split_policy_version
        and stored.fold_plan_hash == requested.fold_plan_hash
        and stored.reason == requested.reason.strip()
    )


__all__ = [
    "LearningSplitApprovalConflict",
    "LearningSplitApprovalRepository",
]
