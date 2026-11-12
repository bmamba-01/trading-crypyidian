"""Repository ledger incident và projection runtime chỉ đọc boolean theo workspace."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import exists, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from trading_bot.adapters.database.models import (
    LearningDriftAssessmentMemberRecord,
    LearningDriftAssessmentRecord,
    LearningIncidentHoldEventRecord,
    LearningRetrainProposalRecord,
)
from trading_bot.adapters.database.repositories.learning_drift import (
    _assessment_from_record,
    _proposal_from_record,
)
from trading_bot.domain.learning.drift import LearningDriftAssessment, LearningRetrainProposal
from trading_bot.domain.learning.incident_entry_hold import LearningIncidentHoldEvent
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentMode,
    ScorerDeploymentSnapshot,
    ScorerDeploymentTarget,
)


class LearningIncidentHoldConflict(RuntimeError):
    """Lineage, target hiện hành, CAS hoặc ý định idempotency không còn khớp."""


class LearningIncidentHoldUnavailable(RuntimeError):
    """Projection runtime không trả trạng thái có thể tin cậy."""


@dataclass(frozen=True, slots=True)
class LearningIncidentHoldAppendResult:
    """Event gốc cùng dấu hiệu retry; caller ghi audit cùng transaction khi event mới."""

    event: LearningIncidentHoldEvent
    is_duplicate: bool


@dataclass(frozen=True, slots=True)
class EligibleLearningIncidentTarget:
    """Liên kết metadata đã kiểm qua gateway hiện hành trong UoW có khóa."""

    proposal: LearningRetrainProposal
    assessment: LearningDriftAssessment
    target: ScorerDeploymentSnapshot
    incumbent_artifact_hash: str


class LearningIncidentEntryHoldRepository:
    """Không commit; mọi phương thức nhận workspace từ Principal/runtime context."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        if type(workspace_id) is not UUID or workspace_id.int == 0:
            raise ValueError("workspace_id phải là UUID khác zero.")
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )

    async def lock_runtime_entry_scope(self, *, workspace_id: UUID) -> None:
        """Lấy cùng khóa L1 đang dùng trong transaction PAPER entry."""
        await self._bind_workspace(workspace_id)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:workspace_lock, 0))"),
            {"workspace_lock": f"workspace-settings:{workspace_id}"},
        )

    async def lock_hold_governance_scope(
        self, *, workspace_id: UUID, registry_id: UUID | None = None
    ) -> None:
        """Khóa L1→deployment pointer→active pointer→registry entry tại DB."""
        await self._bind_workspace(workspace_id)
        await self._session.execute(
            text(
                "SELECT public.wp06_lock_learning_incident_scope(CAST(:workspace_id "
                "AS uuid), CAST(:registry_id AS uuid))"
            ),
            {
                "workspace_id": str(workspace_id),
                "registry_id": str(registry_id) if registry_id else None,
            },
        )

    async def active_hold_exists(self, *, workspace_id: UUID) -> bool:
        """Chỉ runtime EXECUTE projection; NULL/sai kiểu luôn gây lỗi fail-closed."""
        try:
            await self._bind_workspace(workspace_id)
            value = await self._session.scalar(
                text(
                    "SELECT public.wp06_learning_incident_hold_active(CAST(:workspace_id AS uuid))"
                ),
                {"workspace_id": str(workspace_id)},
            )
        except DBAPIError as error:
            raise LearningIncidentHoldUnavailable(
                "Không thể xác minh projection chặn entry theo isolation hiện hành."
            ) from error
        if type(value) is not bool:
            raise LearningIncidentHoldUnavailable("Runtime incident projection không trả boolean.")
        return value

    async def find_by_idempotency_key(
        self, *, workspace_id: UUID, idempotency_key: UUID
    ) -> LearningIncidentHoldEvent | None:
        """Tra event gốc trước mọi recheck target/CAS khi service xử lý retry."""
        await self._bind_workspace(workspace_id)
        row = await self._session.scalar(
            select(LearningIncidentHoldEventRecord).where(
                LearningIncidentHoldEventRecord.workspace_id == workspace_id,
                LearningIncidentHoldEventRecord.idempotency_key == idempotency_key,
            )
        )
        return row.to_domain() if row is not None else None

    async def list_history(
        self, *, workspace_id: UUID, proposal_id: UUID | None = None, limit: int = 200
    ) -> tuple[LearningIncidentHoldEvent, ...]:
        """Lịch sử metadata giới hạn, không nhận quyền đọc sample hoặc artifact."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit lịch sử phải trong [1, 1000].")
        await self._bind_workspace(workspace_id)
        statement = select(LearningIncidentHoldEventRecord).where(
            LearningIncidentHoldEventRecord.workspace_id == workspace_id
        )
        if proposal_id is not None:
            statement = statement.where(LearningIncidentHoldEventRecord.proposal_id == proposal_id)
        rows = await self._session.scalars(
            statement.order_by(
                LearningIncidentHoldEventRecord.created_at.desc(),
                LearningIncidentHoldEventRecord.proposal_id,
                LearningIncidentHoldEventRecord.revision.desc(),
            ).limit(limit)
        )
        return tuple(row.to_domain() for row in rows.all())

    async def list_open_holds(
        self, *, workspace_id: UUID, limit: int = 100
    ) -> tuple[LearningIncidentHoldEvent, ...]:
        """Đọc HOLD chưa có RELEASE, không suy trạng thái từ lát lịch sử giới hạn."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit hold đang mở phải trong [1, 100].")
        await self._bind_workspace(workspace_id)
        release = aliased(LearningIncidentHoldEventRecord)
        statement = (
            select(LearningIncidentHoldEventRecord)
            .where(
                LearningIncidentHoldEventRecord.workspace_id == workspace_id,
                LearningIncidentHoldEventRecord.action == "HOLD",
                ~exists(
                    select(1).where(
                        release.workspace_id == LearningIncidentHoldEventRecord.workspace_id,
                        release.proposal_id == LearningIncidentHoldEventRecord.proposal_id,
                        release.action == "RELEASE",
                    )
                ),
            )
            .order_by(
                LearningIncidentHoldEventRecord.created_at.desc(),
                LearningIncidentHoldEventRecord.proposal_id,
            )
            .limit(limit)
        )
        rows = await self._session.scalars(statement)
        return tuple(row.to_domain() for row in rows.all())

    async def list_recent_proposal_ids(
        self, *, workspace_id: UUID, limit: int = 50
    ) -> tuple[UUID, ...]:
        """Lấy ID proposal gần đây; ứng viên phải qua current gateway trước khi hiển thị."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit proposal phải trong [1, 100].")
        await self._bind_workspace(workspace_id)
        rows = await self._session.scalars(
            select(LearningRetrainProposalRecord.id)
            .where(LearningRetrainProposalRecord.workspace_id == workspace_id)
            .order_by(
                LearningRetrainProposalRecord.created_at.desc(),
                LearningRetrainProposalRecord.id.desc(),
            )
            .limit(limit)
        )
        return tuple(rows.all())

    async def append_event(
        self, *, workspace_id: UUID, event: LearningIncidentHoldEvent
    ) -> LearningIncidentHoldAppendResult:
        """Append dưới L1; retry dùng hash ý định, DB guard xác minh lineage/current target."""
        if event.workspace_id != workspace_id:
            raise PermissionError("Event không thuộc workspace UoW.")
        await self.lock_runtime_entry_scope(workspace_id=workspace_id)
        previous = await self.find_by_idempotency_key(
            workspace_id=workspace_id, idempotency_key=event.idempotency_key
        )
        if previous is not None:
            if previous.intent_hash != event.intent_hash:
                raise LearningIncidentHoldConflict(
                    "Idempotency key đã dùng cho ý định incident khác."
                )
            return LearningIncidentHoldAppendResult(previous, True)
        self._session.add(LearningIncidentHoldEventRecord.from_domain(event))
        try:
            await self._session.flush()
        except DBAPIError as error:
            raise LearningIncidentHoldConflict(
                "DB từ chối event incident: lineage/target/CAS không còn hợp lệ."
            ) from error
        return LearningIncidentHoldAppendResult(event, False)

    async def eligible_incident_target(
        self, *, workspace_id: UUID, proposal_id: UUID
    ) -> EligibleLearningIncidentTarget:
        """Kiểm membership/latest assessment/current rights bằng gateway authoritative."""
        await self._bind_workspace(workspace_id)
        try:
            result = await self._session.execute(
                text(
                    "SELECT * FROM "
                    "public.wp06_learning_incident_eligible_target(CAST(:workspace_id "
                    "AS uuid), CAST(:proposal_id AS uuid))"
                ),
                {"workspace_id": str(workspace_id), "proposal_id": str(proposal_id)},
            )
        except DBAPIError as error:
            raise LearningIncidentHoldConflict(
                "Incident không còn eligible theo assessment/target/quyền hiện hành."
            ) from error
        eligible = result.mappings().one_or_none()
        if eligible is None:
            raise LearningIncidentHoldConflict("Gateway không trả target incident eligible.")
        proposal_row = await self._session.scalar(
            select(LearningRetrainProposalRecord).where(
                LearningRetrainProposalRecord.workspace_id == workspace_id,
                LearningRetrainProposalRecord.id == proposal_id,
            )
        )
        if proposal_row is None:
            raise LearningIncidentHoldConflict("Không tìm thấy proposal incident trong workspace.")
        assessment_row = await self._session.scalar(
            select(LearningDriftAssessmentRecord).where(
                LearningDriftAssessmentRecord.workspace_id == workspace_id,
                LearningDriftAssessmentRecord.id == proposal_row.assessment_id,
            )
        )
        if assessment_row is None:
            raise LearningIncidentHoldConflict(
                "Không tìm thấy assessment incident trong workspace."
            )
        member_ids = await self._session.scalars(
            select(LearningDriftAssessmentMemberRecord.observation_id)
            .where(
                LearningDriftAssessmentMemberRecord.workspace_id == workspace_id,
                LearningDriftAssessmentMemberRecord.assessment_id == assessment_row.id,
            )
            .order_by(LearningDriftAssessmentMemberRecord.ordinal)
        )
        assessment = _assessment_from_record(assessment_row, tuple(member_ids.all()))
        if assessment.content_hash != assessment_row.content_hash:
            raise LearningIncidentHoldConflict("Assessment incident hash đã sai.")
        proposal = _proposal_from_record(proposal_row, assessment)
        # SELECT chỉ metadata; gateway không gửi canonical artifact/base payload cho repository này.
        columns = ", ".join(
            (
                "workspace_id",
                "mode",
                "deployment_event_id",
                "deployment_revision",
                *ScorerDeploymentTarget.__dataclass_fields__,
            )
        )
        result = await self._session.execute(
            text(
                f"SELECT {columns} FROM "
                f"public.wp06_resolve_scorer_runtime_target(CAST(:workspace_id AS "
                f"uuid), NULL::text, NULL::jsonb, NULL::uuid, NULL::integer, false)"
            ),
            {"workspace_id": str(workspace_id)},
        )
        metadata = result.mappings().one_or_none()
        if metadata is None or metadata["workspace_id"] != workspace_id:
            raise LearningIncidentHoldConflict("Target scorer đã đổi sau kiểm eligibility.")
        target = ScorerDeploymentTarget(
            **{name: metadata[name] for name in ScorerDeploymentTarget.__dataclass_fields__}
        )
        snapshot = ScorerDeploymentSnapshot(
            workspace_id=workspace_id,
            mode=ScorerDeploymentMode(metadata["mode"]),
            deployment_event_id=metadata["deployment_event_id"],
            deployment_revision=metadata["deployment_revision"],
            active_pointer_version=target.active_pointer_version,
            target=target,
        )
        if (
            target.registry_id,
            target.registry_revision,
            target.active_pointer_version,
            snapshot.deployment_revision,
        ) != (
            eligible["incumbent_registry_id"],
            eligible["incumbent_registry_revision"],
            eligible["active_pointer_version"],
            eligible["deployment_revision"],
        ):
            raise LearningIncidentHoldConflict("Tuple incumbent/deployment đã đổi trong UoW.")
        return EligibleLearningIncidentTarget(
            proposal, assessment, snapshot, eligible["incumbent_artifact_hash"]
        )
