"""Lưu assessment drift workspace-scoped, chỉ từ metadata shadow đã persist."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import exists, func, literal, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from trading_bot.adapters.database.models import (
    BotDecisionRecord,
    LearningDriftAssessmentMemberRecord,
    LearningDriftAssessmentRecord,
    LearningRetrainProposalRecord,
    LearningShadowObservationRecord,
)
from trading_bot.domain.learning.drift import (
    DriftAssessmentStatus,
    DriftObservation,
    DriftPolicy,
    DriftReason,
    LearningDriftAssessment,
    LearningRetrainProposal,
    create_learning_retrain_proposal,
)
from trading_bot.domain.learning.scoring import ScoreDecision, ScoreReason

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class LearningDriftRepositoryConflict(RuntimeError):
    """Idempotency key đã gắn với assessment hoặc proposal khác."""


class LearningDriftCorruptRecord(RuntimeError):
    """Hash, membership hoặc proposal đã lưu không khớp domain canonical."""


@dataclass(frozen=True, slots=True)
class LearningDriftCohortMember:
    """Metadata lineage của một shadow observation trong cohort."""

    observation: DriftObservation
    rights_fingerprint: str | None

    def __post_init__(self) -> None:
        if type(self.observation) is not DriftObservation:
            raise ValueError("observation phải là DriftObservation.")
        if self.observation.rights_eligible:
            if (
                not isinstance(self.rights_fingerprint, str)
                or _SHA256.fullmatch(self.rights_fingerprint) is None
            ):
                raise ValueError("Quyền đã xác minh cần rights_fingerprint.")
        elif self.rights_fingerprint is not None:
            raise ValueError("Quyền chưa xác minh không được ghim rights_fingerprint.")


@dataclass(frozen=True, slots=True)
class LearningDriftPersistenceResult:
    """Assessment và proposal đã lưu hoặc tìm lại sau retry."""

    assessment: LearningDriftAssessment
    proposal: LearningRetrainProposal | None
    is_duplicate: bool


class LearningDriftRepository:
    """Repository chỉ đọc metadata cohort và append assessment/member/proposal."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def server_policy() -> DriftPolicy:
        """Trả policy cố định do server định nghĩa, không nhận ngưỡng từ request."""
        return DriftPolicy(
            policy_version="coverage-v2-30d",
            observation_window_days=30,
            minimum_observations=3,
            minimum_snapshot_coverage=0.8,
            minimum_quality_rate=0.9,
        )

    @staticmethod
    def cohort_statement(
        *,
        workspace_id: UUID,
        registry_id: UUID,
        revision: int,
        window_start: datetime,
        window_end: datetime,
    ) -> Select[Any]:
        """Project metadata cần cho drift, không đọc payload quyết định hay feature."""
        _validate_cohort_scope(workspace_id, registry_id, revision)
        for value in (window_start, window_end):
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Cận cửa sổ phải có timezone.")
        if window_start > window_end:
            raise ValueError("Cận đầu cửa sổ không được sau cận cuối.")
        observation = LearningShadowObservationRecord
        baseline_available = exists(
            select(literal(1))
            .select_from(BotDecisionRecord)
            .where(
                BotDecisionRecord.workspace_id == observation.workspace_id,
                BotDecisionRecord.id == observation.incumbent_decision_id,
                BotDecisionRecord.decision_hash == observation.decision_hash,
            )
        ).label("baseline_available")
        statement = (
            select(
                observation.id.label("observation_id"),
                observation.workspace_id.label("workspace_id"),
                observation.challenger_registry_id.label("challenger_registry_id"),
                observation.challenger_revision.label("challenger_revision"),
                observation.observed_at.label("observed_at"),
                observation.snapshot_hash.label("snapshot_hash"),
                observation.snapshot_coverage.label("snapshot_coverage"),
                observation.snapshot_quality_ok.label("snapshot_quality_ok"),
                baseline_available,
                observation.schema_compatible.label("schema_compatible"),
                observation.rights_eligible.label("rights_eligible"),
                observation.rights_fingerprint.label("rights_fingerprint"),
                observation.challenger_decision.label("challenger_decision"),
                observation.challenger_reason.label("challenger_reason"),
            )
            .where(
                observation.workspace_id == workspace_id,
                observation.challenger_registry_id == registry_id,
                observation.challenger_revision == revision,
                observation.observed_at >= window_start,
                observation.observed_at <= window_end,
            )
            .order_by(observation.id.asc())
        )
        return cast(Select[Any], statement)

    @staticmethod
    def transaction_clock_statement() -> Select[Any]:
        """Lấy cùng một thời điểm PostgreSQL transaction để trigger xác minh."""
        return select(func.transaction_timestamp())

    @staticmethod
    def assessment_idempotency_key(
        *,
        workspace_id: UUID,
        registry_id: UUID,
        revision: int,
        policy_version: str,
        membership_digest: str,
    ) -> str:
        _validate_cohort_scope(workspace_id, registry_id, revision)
        canonical = json.dumps(
            {
                "membership_digest": membership_digest,
                "policy_version": policy_version,
                "registry_id": str(registry_id),
                "revision": revision,
                "workspace_id": str(workspace_id),
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def membership_digest(observation_ids: tuple[UUID, ...]) -> str:
        if len(set(observation_ids)) != len(observation_ids):
            raise ValueError("Observation ID bị trùng trong membership.")
        canonical = json.dumps(
            [str(item) for item in sorted(observation_ids, key=str)],
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def lock_cohort(self, *, workspace_id: UUID, registry_id: UUID, revision: int) -> None:
        _validate_cohort_scope(workspace_id, registry_id, revision)
        await self._bind_workspace(workspace_id)
        await self._session.execute(
            text(
                "SELECT public.wp06_lock_learning_shadow_cohort("
                ":workspace_id, :registry_id, :revision)"
            ),
            {
                "workspace_id": workspace_id,
                "registry_id": registry_id,
                "revision": revision,
            },
        )

    async def list_cohort(
        self,
        *,
        workspace_id: UUID,
        registry_id: UUID,
        revision: int,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[LearningDriftCohortMember, ...]:
        await self._bind_workspace(workspace_id)
        rows = (
            (
                await self._session.execute(
                    self.cohort_statement(
                        workspace_id=workspace_id,
                        registry_id=registry_id,
                        revision=revision,
                        window_start=window_start,
                        window_end=window_end,
                    )
                )
            )
            .mappings()
            .all()
        )
        return tuple(_member_from_mapping(row) for row in rows)

    async def transaction_timestamp(self, *, workspace_id: UUID) -> datetime:
        await self._bind_workspace(workspace_id)
        value = await self._session.scalar(self.transaction_clock_statement())
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("PostgreSQL transaction_timestamp() thiếu timezone.")
        return value.astimezone(UTC)

    async def find_assessment(
        self, *, workspace_id: UUID, idempotency_key: str
    ) -> LearningDriftPersistenceResult | None:
        await self._bind_workspace(workspace_id)
        record = await self._session.scalar(
            select(LearningDriftAssessmentRecord).where(
                LearningDriftAssessmentRecord.workspace_id == workspace_id,
                LearningDriftAssessmentRecord.idempotency_key == idempotency_key,
            )
        )
        if record is None:
            return None
        members = (
            await self._session.scalars(
                select(LearningDriftAssessmentMemberRecord)
                .where(
                    LearningDriftAssessmentMemberRecord.workspace_id == workspace_id,
                    LearningDriftAssessmentMemberRecord.assessment_id == record.id,
                )
                .order_by(LearningDriftAssessmentMemberRecord.ordinal.asc())
            )
        ).all()
        assessment = _assessment_from_record(record, tuple(row.observation_id for row in members))
        if (
            assessment.content_hash != record.content_hash
            or assessment.membership_digest != record.membership_digest
            or record.idempotency_key != idempotency_key
        ):
            raise LearningDriftCorruptRecord("Assessment hash/idempotency không khớp domain.")
        proposal_record = await self._session.scalar(
            select(LearningRetrainProposalRecord).where(
                LearningRetrainProposalRecord.workspace_id == workspace_id,
                LearningRetrainProposalRecord.assessment_id == assessment.assessment_id,
            )
        )
        proposal: LearningRetrainProposal | None = None
        if proposal_record is not None:
            proposal = _proposal_from_record(proposal_record, assessment)
        if (assessment.data_quality_status is DriftAssessmentStatus.DETECTED) != (
            proposal is not None
        ):
            raise LearningDriftCorruptRecord("Proposal không khớp assessment DETECTED.")
        return LearningDriftPersistenceResult(
            assessment=assessment,
            proposal=proposal,
            is_duplicate=True,
        )

    async def persist(
        self,
        *,
        assessment: LearningDriftAssessment,
        members: tuple[LearningDriftCohortMember, ...],
        proposal: LearningRetrainProposal | None,
        policy: DriftPolicy,
        registry_id: UUID,
        revision: int,
        idempotency_key: str,
    ) -> LearningDriftPersistenceResult:
        if type(assessment) is not LearningDriftAssessment:
            raise ValueError("assessment phải là LearningDriftAssessment.")
        if type(policy) is not DriftPolicy or policy != self.server_policy():
            raise ValueError("Assessment chỉ được lưu bằng policy server hiện hành.")
        if type(members) is not tuple or any(
            type(member) is not LearningDriftCohortMember for member in members
        ):
            raise ValueError("members phải là tuple LearningDriftCohortMember.")
        if tuple(item.observation.observation_id for item in members) != assessment.member_ids:
            raise ValueError("Member rows phải theo thứ tự canonical của assessment.")
        if assessment.member_count == 0 or not assessment.member_ids:
            raise ValueError("Không persist assessment rỗng vì chưa ghim được cohort scope.")
        if assessment.workspace_id.int == 0:
            raise ValueError("workspace_id phải là UUID khác zero.")
        _validate_cohort_scope(assessment.workspace_id, registry_id, revision)
        await self._bind_workspace(assessment.workspace_id)
        expected_key = self.assessment_idempotency_key(
            workspace_id=assessment.workspace_id,
            registry_id=registry_id,
            revision=revision,
            policy_version=policy.policy_version,
            membership_digest=assessment.membership_digest,
        )
        if assessment.member_count and (
            assessment.cohort_registry_id != registry_id or assessment.cohort_revision != revision
        ):
            raise ValueError("Assessment không khớp challenger cohort được yêu cầu.")
        if expected_key != idempotency_key:
            raise ValueError("Assessment idempotency key không khớp cohort.")
        if (assessment.data_quality_status is DriftAssessmentStatus.DETECTED) != (
            proposal is not None
        ):
            raise ValueError("Proposal chỉ tồn tại cho assessment dữ liệu DETECTED.")
        if proposal is not None and (
            proposal.assessment_id != assessment.assessment_id
            or proposal.assessment_hash != assessment.content_hash
            or proposal.workspace_id != assessment.workspace_id
        ):
            raise ValueError("Proposal phải tham chiếu chính xác assessment đã tạo.")

        assessment_record = _assessment_record(
            assessment,
            policy=policy,
            idempotency_key=idempotency_key,
        )
        member_records = tuple(
            _member_record(assessment, ordinal, member) for ordinal, member in enumerate(members)
        )
        proposal_record = _proposal_record(proposal) if proposal is not None else None
        try:
            async with self._session.begin_nested():
                self._session.add(assessment_record)
                await self._session.flush([assessment_record])
                for record in member_records:
                    self._session.add(record)
                await self._session.flush(member_records)
                if proposal_record is not None:
                    self._session.add(proposal_record)
                    await self._session.flush([proposal_record])
        except IntegrityError as error:
            if not _is_idempotency_violation(error):
                raise
            stored = await self.find_assessment(
                workspace_id=assessment.workspace_id,
                idempotency_key=idempotency_key,
            )
            if stored is None:
                raise LearningDriftRepositoryConflict(
                    "Assessment/proposal bị tranh chấp nhưng không có bản ghi để đối chiếu."
                ) from error
            if not _same_assessment_identity(stored.assessment, assessment):
                raise LearningDriftRepositoryConflict(
                    "Idempotency key đã gắn với cohort hoặc hash khác."
                ) from error
            return stored
        return LearningDriftPersistenceResult(
            assessment=assessment,
            proposal=proposal,
            is_duplicate=False,
        )

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        if type(workspace_id) is not UUID or workspace_id.int == 0:
            raise ValueError("workspace_id phải là UUID khác zero.")
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )


def _member_from_mapping(row: Any) -> LearningDriftCohortMember:
    observation = DriftObservation(
        observation_id=row["observation_id"],
        workspace_id=row["workspace_id"],
        challenger_registry_id=row["challenger_registry_id"],
        challenger_revision=row["challenger_revision"],
        observed_at=row["observed_at"],
        snapshot_hash=row["snapshot_hash"],
        snapshot_coverage=float(row["snapshot_coverage"]),
        snapshot_quality_ok=row["snapshot_quality_ok"],
        baseline_available=row["baseline_available"],
        schema_compatible=row["schema_compatible"],
        rights_eligible=row["rights_eligible"],
        challenger_decision=ScoreDecision(row["challenger_decision"]),
        challenger_reason=ScoreReason(row["challenger_reason"]),
    )
    return LearningDriftCohortMember(
        observation=observation,
        rights_fingerprint=row["rights_fingerprint"],
    )


def _assessment_from_record(
    record: LearningDriftAssessmentRecord,
    member_ids: tuple[UUID, ...],
) -> LearningDriftAssessment:
    try:
        return LearningDriftAssessment(
            assessment_id=record.id,
            workspace_id=record.workspace_id,
            policy_version=record.policy_version,
            cohort_registry_id=record.cohort_registry_id,
            cohort_revision=record.cohort_revision,
            window_start=record.window_start,
            window_end=record.window_end,
            member_ids=member_ids,
            membership_digest=record.membership_digest,
            member_count=record.member_count,
            eligible_count=record.eligible_count,
            snapshot_coverage_rate=record.snapshot_coverage_rate,
            quality_rate=record.quality_rate,
            abstention_rate=record.abstention_rate,
            data_quality_status=DriftAssessmentStatus(record.data_quality_status),
            performance_status=DriftAssessmentStatus(record.performance_status),
            reason_codes=tuple(DriftReason(reason) for reason in record.reason_codes),
            created_at=record.created_at,
        )
    except (TypeError, ValueError) as error:
        raise LearningDriftCorruptRecord("Assessment đã lưu không hợp lệ theo domain.") from error


def _proposal_from_record(
    record: LearningRetrainProposalRecord,
    assessment: LearningDriftAssessment,
) -> LearningRetrainProposal:
    try:
        proposal = create_learning_retrain_proposal(
            assessment,
            proposal_id=record.id,
            created_at=record.created_at,
        )
    except (TypeError, ValueError) as error:
        raise LearningDriftCorruptRecord("Proposal đã lưu không hợp lệ theo domain.") from error
    if (
        proposal.workspace_id != record.workspace_id
        or proposal.assessment_id != record.assessment_id
        or proposal.assessment_hash != record.assessment_hash
        or proposal.policy_version != record.policy_version
        or proposal.membership_digest != record.membership_digest
        or proposal.idempotency_key != record.idempotency_key
        or [reason.value for reason in proposal.reason_codes] != record.reason_codes
        or proposal.content_hash != record.content_hash
    ):
        raise LearningDriftCorruptRecord("Proposal hash/lineage không khớp domain.")
    return proposal


def _assessment_record(
    assessment: LearningDriftAssessment,
    *,
    policy: DriftPolicy,
    idempotency_key: str,
) -> LearningDriftAssessmentRecord:
    return LearningDriftAssessmentRecord(
        id=assessment.assessment_id,
        workspace_id=assessment.workspace_id,
        policy_version=assessment.policy_version,
        cohort_registry_id=assessment.cohort_registry_id,
        cohort_revision=assessment.cohort_revision,
        window_start=assessment.window_start,
        window_end=assessment.window_end,
        member_count=assessment.member_count,
        eligible_count=assessment.eligible_count,
        observation_window_days=policy.observation_window_days,
        minimum_observations=policy.minimum_observations,
        minimum_snapshot_coverage=policy.minimum_snapshot_coverage,
        minimum_quality_rate=policy.minimum_quality_rate,
        snapshot_coverage_rate=assessment.snapshot_coverage_rate,
        quality_rate=assessment.quality_rate,
        abstention_rate=assessment.abstention_rate,
        data_quality_status=assessment.data_quality_status.value,
        performance_status=assessment.performance_status.value,
        reason_codes=[reason.value for reason in assessment.reason_codes],
        membership_digest=assessment.membership_digest,
        idempotency_key=idempotency_key,
        content_hash=assessment.content_hash,
        created_at=assessment.created_at,
    )


def _member_record(
    assessment: LearningDriftAssessment,
    ordinal: int,
    member: LearningDriftCohortMember,
) -> LearningDriftAssessmentMemberRecord:
    observation = member.observation
    return LearningDriftAssessmentMemberRecord(
        workspace_id=assessment.workspace_id,
        assessment_id=assessment.assessment_id,
        observation_id=observation.observation_id,
        ordinal=ordinal,
        eligible=(
            observation.baseline_available
            and observation.schema_compatible
            and observation.rights_eligible
        ),
        observed_at=observation.observed_at,
        snapshot_hash=observation.snapshot_hash,
        snapshot_coverage=observation.snapshot_coverage,
        snapshot_quality_ok=observation.snapshot_quality_ok,
        baseline_available=observation.baseline_available,
        schema_compatible=observation.schema_compatible,
        rights_eligible=observation.rights_eligible,
        rights_fingerprint=member.rights_fingerprint,
        challenger_decision=observation.challenger_decision.value,
    )


def _proposal_record(proposal: LearningRetrainProposal) -> LearningRetrainProposalRecord:
    return LearningRetrainProposalRecord(
        id=proposal.proposal_id,
        workspace_id=proposal.workspace_id,
        assessment_id=proposal.assessment_id,
        assessment_hash=proposal.assessment_hash,
        policy_version=proposal.policy_version,
        membership_digest=proposal.membership_digest,
        idempotency_key=proposal.idempotency_key,
        reason_codes=[reason.value for reason in proposal.reason_codes],
        content_hash=proposal.content_hash,
        created_at=proposal.created_at,
    )


def _same_assessment_identity(
    stored: LearningDriftAssessment,
    requested: LearningDriftAssessment,
) -> bool:
    # Retry cùng membership giữ nguyên mốc/hash cũ dù DB as-of mới đã tiến lên.
    return (
        stored.workspace_id == requested.workspace_id
        and stored.policy_version == requested.policy_version
        and stored.cohort_registry_id == requested.cohort_registry_id
        and stored.cohort_revision == requested.cohort_revision
        and stored.window_start == requested.window_start
        and stored.member_ids == requested.member_ids
        and stored.membership_digest == requested.membership_digest
        and stored.member_count == requested.member_count
        and stored.eligible_count == requested.eligible_count
        and stored.snapshot_coverage_rate == requested.snapshot_coverage_rate
        and stored.quality_rate == requested.quality_rate
        and stored.abstention_rate == requested.abstention_rate
        and stored.data_quality_status is requested.data_quality_status
        and stored.performance_status is requested.performance_status
        and stored.reason_codes == requested.reason_codes
    )


def _is_idempotency_violation(error: IntegrityError) -> bool:
    diagnostic = getattr(error.orig, "diag", None)
    return getattr(diagnostic, "constraint_name", None) in {
        "uq_learning_drift_assessment_idempotency",
        "uq_learning_retrain_proposal_idempotency",
    }


def _validate_cohort_scope(workspace_id: UUID, registry_id: UUID, revision: int) -> None:
    for name, value in (("workspace_id", workspace_id), ("registry_id", registry_id)):
        if type(value) is not UUID or value.int == 0:
            raise ValueError(f"{name} phải là UUID khác zero.")
    if type(revision) is not int or revision < 1:
        raise ValueError("revision phải là số nguyên dương.")


__all__ = [
    "LearningDriftCohortMember",
    "LearningDriftCorruptRecord",
    "LearningDriftPersistenceResult",
    "LearningDriftRepository",
    "LearningDriftRepositoryConflict",
]
