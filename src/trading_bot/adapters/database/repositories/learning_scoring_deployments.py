"""Repository append-only cho scorer deployment; active modes đang fail-closed."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    LearningModelActivePointerRecord,
    LearningScoringDeploymentEventRecord,
    LearningScoringDeploymentPointerRecord,
)
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentAdminSnapshot,
    ScorerDeploymentEvent,
    ScorerDeploymentHealth,
    ScorerDeploymentMode,
    ScorerDeploymentSnapshot,
)


class ScorerDeploymentConflict(RuntimeError):
    """Idempotency intent, deployment revision, hay registry pointer bị đổi."""


class ScorerDeploymentRightsUnavailable(RuntimeError):
    """Không có gateway kiểm tra quyền nguồn hiện hành an toàn cho scorer."""


class LearningScoringDeploymentRepository:
    """Đọc metadata và append event scorer trong workspace UoW hiện hành.

    Mọi thao tác write và runtime recheck phải dùng transaction READ COMMITTED.
    Không bật SNAPSHOT/REPEATABLE READ vì rights gateway cần thấy chính sách mới
    sau khi đợi source advisory lock. Gateway chưa được nối nên active modes bị
    từ chối tại cả DB trigger và repository.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_active_scoring_deployment(self, workspace_id: UUID) -> ScorerDeploymentSnapshot:
        """Nạp projection hiện tại; mode bật bị từ chối tới khi có rights gateway."""
        state = await self.load_admin_scoring_deployment(workspace_id)
        if state.mode is not ScorerDeploymentMode.DISABLED:
            raise ScorerDeploymentRightsUnavailable(
                "Scorer active nhưng rights gateway chưa xác minh bundle TRAINING và nguồn WP05."
            )
        return ScorerDeploymentSnapshot(
            workspace_id=workspace_id,
            mode=state.mode,
            deployment_event_id=state.deployment_event_id,
            deployment_revision=state.deployment_revision,
            active_pointer_version=state.active_pointer_version,
            target=None,
        )

    async def load_admin_scoring_deployment(
        self, workspace_id: UUID
    ) -> ScorerDeploymentAdminSnapshot:
        """Trả CAS hiện hành kể cả mode stale; không trả target runtime dùng được."""
        await self._bind_workspace(workspace_id)
        await self._lock(workspace_id, "current-pointer")
        pointer = await self._session.scalar(
            select(LearningScoringDeploymentPointerRecord).where(
                LearningScoringDeploymentPointerRecord.workspace_id == workspace_id
            )
        )
        if pointer is None:
            registry_pointer = await self._session.scalar(
                select(LearningModelActivePointerRecord).where(
                    LearningModelActivePointerRecord.workspace_id == workspace_id
                )
            )
            return ScorerDeploymentAdminSnapshot(
                workspace_id=workspace_id,
                mode=ScorerDeploymentMode.DISABLED,
                deployment_event_id=None,
                deployment_revision=0,
                active_pointer_version=(
                    registry_pointer.version if registry_pointer is not None else 0
                ),
                health=ScorerDeploymentHealth.DISABLED,
            )
        registry_pointer = await self._session.scalar(
            select(LearningModelActivePointerRecord).where(
                LearningModelActivePointerRecord.workspace_id == workspace_id
            )
        )
        mode = ScorerDeploymentMode(pointer.mode)
        return ScorerDeploymentAdminSnapshot(
            workspace_id=workspace_id,
            mode=mode,
            deployment_event_id=pointer.event_id,
            deployment_revision=pointer.revision,
            active_pointer_version=(
                registry_pointer.version if registry_pointer is not None else 0
            ),
            health=(
                ScorerDeploymentHealth.DISABLED
                if mode is ScorerDeploymentMode.DISABLED
                else ScorerDeploymentHealth.RIGHTS_RECHECK_REQUIRED
            ),
        )

    async def verify_active_scoring_deployment(
        self,
        *,
        workspace_id: UUID,
        expected: ScorerDeploymentSnapshot,
    ) -> ScorerDeploymentSnapshot:
        """Recheck projection trong decision UoW, thất bại đóng nếu mode đang bật.

        Khi có mode scorer, caller bắt buộc dùng transaction PostgreSQL
        READ COMMITTED. Rights gateway phải lấy các source advisory lock trước
        khi đọc receipt/policy và giữ chúng đến commit cùng BotDecision/score.
        """
        if type(expected) is not ScorerDeploymentSnapshot:
            raise TypeError("expected phải là snapshot deployment scorer đã resolve.")
        if expected.workspace_id != workspace_id:
            raise ScorerDeploymentConflict("Workspace snapshot scorer không khớp UoW.")
        isolation = await self._session.scalar(text("SHOW transaction_isolation"))
        if str(isolation).lower() != "read committed":
            raise ScorerDeploymentConflict(
                "Runtime scorer yêu cầu transaction READ COMMITTED để recheck quyền hiện hành."
            )
        current = await self.load_active_scoring_deployment(workspace_id)
        if current != expected:
            raise ScorerDeploymentConflict("Deployment scorer hoặc Task6 pointer đã đổi.")
        if current.mode is not ScorerDeploymentMode.DISABLED:
            # Defense in depth: currently unreachable because load() fails closed.
            raise ScorerDeploymentRightsUnavailable(
                "Không thể xác minh rights scorer hiện hành trong source lock."
            )
        return current

    async def append_disabled_event(
        self,
        *,
        workspace_id: UUID,
        actor_membership_id: UUID,
        mfa_verified_at: datetime,
        expected_pointer_version: int,
        expected_deployment_revision: int,
        idempotency_key: UUID,
        reason: str,
    ) -> ScorerDeploymentEvent:
        """Ghi event DISABLED; API không được tự cung cấp actor hoặc target."""
        if type(workspace_id) is not UUID or type(actor_membership_id) is not UUID:
            raise ValueError("Workspace và actor phải là UUID từ Principal.")
        if type(idempotency_key) is not UUID:
            raise ValueError("idempotency_key phải là UUID.")
        if type(expected_pointer_version) is not int or expected_pointer_version < 0:
            raise ValueError("expected_pointer_version không hợp lệ.")
        if type(expected_deployment_revision) is not int or expected_deployment_revision < 0:
            raise ValueError("expected_deployment_revision không hợp lệ.")
        await self._bind_workspace(workspace_id)

        db_created_at = await self._session.scalar(text("SELECT pg_catalog.clock_timestamp()"))
        if db_created_at is None:
            raise ScorerDeploymentConflict("Không lấy được thời gian chuẩn PostgreSQL.")

        candidate = ScorerDeploymentEvent(
            event_id=uuid4(),
            workspace_id=workspace_id,
            revision=expected_deployment_revision + 1,
            mode=ScorerDeploymentMode.DISABLED,
            target=None,
            actor_membership_id=actor_membership_id,
            mfa_verified_at=mfa_verified_at,
            mfa_method="TOTP",
            expected_pointer_version=expected_pointer_version,
            expected_deployment_revision=expected_deployment_revision,
            reason=reason,
            idempotency_key=idempotency_key,
            previous_event_id=None,
            previous_event_hash=None,
            created_at=db_created_at.astimezone(UTC),
        )
        await self._lock(workspace_id, f"idempotency:{idempotency_key}")
        prior = await self._session.scalar(
            select(LearningScoringDeploymentEventRecord).where(
                LearningScoringDeploymentEventRecord.workspace_id == workspace_id,
                LearningScoringDeploymentEventRecord.idempotency_key == idempotency_key,
            )
        )
        if prior is not None:
            if prior.intent_hash != candidate.intent_hash:
                raise ScorerDeploymentConflict(
                    "Idempotency key đã được dùng cho ý định deployment khác."
                )
            return _event_snapshot(prior)

        await self._lock(workspace_id, "current-pointer")
        current_pointer = await self._session.scalar(
            select(LearningScoringDeploymentPointerRecord).where(
                LearningScoringDeploymentPointerRecord.workspace_id == workspace_id
            )
        )
        if current_pointer is None:
            raise ScorerDeploymentConflict("Chưa khởi tạo scorer deployment pointer.")
        if current_pointer.revision != expected_deployment_revision:
            raise ScorerDeploymentConflict("Deployment scorer hoặc Task6 pointer CAS đã cũ.")

        registry_pointer = await self._session.scalar(
            select(LearningModelActivePointerRecord).where(
                LearningModelActivePointerRecord.workspace_id == workspace_id
            )
        )
        actual_pointer_version = registry_pointer.version if registry_pointer is not None else 0
        if actual_pointer_version != expected_pointer_version:
            raise ScorerDeploymentConflict("Task6 active pointer CAS đã cũ.")

        prior_event: LearningScoringDeploymentEventRecord | None = None
        if current_pointer.event_id is not None:
            prior_event = await self._session.scalar(
                select(LearningScoringDeploymentEventRecord).where(
                    LearningScoringDeploymentEventRecord.workspace_id == workspace_id,
                    LearningScoringDeploymentEventRecord.id == current_pointer.event_id,
                )
            )
            if prior_event is None:
                raise ScorerDeploymentConflict("Deployment pointer predecessor không tồn tại.")
        event = ScorerDeploymentEvent(
            event_id=candidate.event_id,
            workspace_id=workspace_id,
            revision=candidate.revision,
            mode=candidate.mode,
            target=None,
            actor_membership_id=actor_membership_id,
            mfa_verified_at=mfa_verified_at,
            mfa_method="TOTP",
            expected_pointer_version=expected_pointer_version,
            expected_deployment_revision=expected_deployment_revision,
            reason=reason,
            idempotency_key=idempotency_key,
            previous_event_id=current_pointer.event_id,
            previous_event_hash=prior_event.event_hash if prior_event is not None else None,
            created_at=candidate.created_at,
        )
        self._session.add(LearningScoringDeploymentEventRecord.from_domain(event))
        await self._session.flush()
        return event

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )

    async def _lock(self, workspace_id: UUID, key: str) -> None:
        await self._session.execute(
            text("SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:key, 0))"),
            {"key": f"learning-scoring-deployment:{workspace_id}:{key}"},
        )


def _event_snapshot(row: LearningScoringDeploymentEventRecord) -> ScorerDeploymentEvent:
    event = ScorerDeploymentEvent(
        event_id=row.id,
        workspace_id=row.workspace_id,
        revision=row.revision,
        mode=ScorerDeploymentMode(row.mode),
        target=None,
        actor_membership_id=row.actor_membership_id,
        mfa_verified_at=row.mfa_verified_at,
        mfa_method=row.mfa_method,
        expected_pointer_version=row.expected_pointer_version,
        expected_deployment_revision=row.expected_deployment_revision,
        reason=row.reason,
        idempotency_key=row.idempotency_key,
        previous_event_id=row.previous_event_id,
        previous_event_hash=row.previous_event_hash,
        created_at=row.created_at,
    )
    if event.event_hash != row.event_hash or event.intent_hash != row.intent_hash:
        raise ScorerDeploymentConflict("Deployment event hash không khớp nội dung đã lưu.")
    return event


__all__ = [
    "LearningScoringDeploymentRepository",
    "ScorerDeploymentConflict",
    "ScorerDeploymentRightsUnavailable",
]
