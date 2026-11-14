"""Tính assessment dữ liệu từ cohort shadow đã lưu, không kích hoạt huấn luyện."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.learning_drift import (
    LearningDriftCohortMember,
    LearningDriftPersistenceResult,
    LearningDriftRepository,
)
from trading_bot.adapters.database.repositories.learning_shadow import LearningShadowRepository
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.learning.drift import (
    DriftAssessmentStatus,
    LearningDriftAssessment,
    LearningRetrainProposal,
    assess_learning_drift,
    create_learning_retrain_proposal,
)


class LearningDriftRightsConflict(RuntimeError):
    """Receipt quyền nguồn hiện hành thiếu hoặc không còn khớp cohort."""


class LearningDriftIdempotencyConflict(RuntimeError):
    """Key assessment đã lưu nhưng cohort hoặc policy hiện tại khác."""


class LearningDriftTimestampConflict(RuntimeError):
    """Transaction timestamp cũ hơn observation đang thuộc cohort."""


class LearningDriftMembershipConflict(RuntimeError):
    """Exact observation/source member set đổi trước khi khóa workspace fence."""


@dataclass(frozen=True, slots=True)
class LearningDriftRun:
    """Kết quả một lượt assessment; proposal chỉ là metadata chờ review."""

    assessment: LearningDriftAssessment
    proposal: LearningRetrainProposal | None
    is_duplicate: bool


class LearningDriftApplication:
    """Đánh giá coverage/chất lượng cohort hiện hành theo policy server cố định."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        unit_of_work_factory: Callable[..., UnitOfWork] = UnitOfWork,
        repository_factory: Callable[[AsyncSession], LearningDriftRepository] = (
            LearningDriftRepository
        ),
        shadow_repository_factory: Callable[[AsyncSession], LearningShadowRepository] = (
            LearningShadowRepository
        ),
    ) -> None:
        if not callable(unit_of_work_factory):
            raise ValueError("unit_of_work_factory phải gọi được.")
        if not callable(repository_factory) or not callable(shadow_repository_factory):
            raise ValueError("Repository factories phải gọi được.")
        self._sessions = sessions
        self._unit_of_work_factory = unit_of_work_factory
        self._repository_factory = repository_factory
        self._shadow_repository_factory = shadow_repository_factory

    async def assess_cohort(
        self,
        *,
        workspace_id: UUID,
        challenger_registry_id: UUID,
        challenger_revision: int,
    ) -> LearningDriftRun | None:
        """Khóa quyền nguồn rồi cohort, đọc metadata và append assessment nguyên tử."""
        _validate_scope(workspace_id, challenger_registry_id, challenger_revision)
        policy = LearningDriftRepository.server_policy()

        async with self._unit_of_work_factory(
            self._sessions,
            workspace_id=workspace_id,
        ) as uow:
            repository = self._repository_factory(uow.session)
            shadow_repository = self._shadow_repository_factory(uow.session)

            await repository.lock_registry_entry(
                workspace_id=workspace_id,
                registry_id=challenger_registry_id,
            )
            initial_at = await repository.transaction_timestamp(workspace_id=workspace_id)
            initial_members = await repository.list_cohort(
                workspace_id=workspace_id,
                registry_id=challenger_registry_id,
                revision=challenger_revision,
                window_start=initial_at - timedelta(days=policy.observation_window_days),
                window_end=initial_at,
            )
            # Không lưu cohort rỗng vì ledger không có cột ghim scope được yêu cầu.
            if not initial_members:
                return None
            _validate_members(
                members=initial_members,
                workspace_id=workspace_id,
                registry_id=challenger_registry_id,
                revision=challenger_revision,
            )
            initial_members = await _load_member_source_ids(
                shadow_repository=shadow_repository,
                workspace_id=workspace_id,
                members=initial_members,
            )
            source_ids = tuple(
                sorted({source_id for member in initial_members for source_id in member.source_ids})
            )
            await shadow_repository.lock_source_ids(
                workspace_id=workspace_id,
                source_ids=source_ids,
            )
            await shadow_repository.lock_member_set(workspace_id=workspace_id)
            created_at = await repository.transaction_timestamp(workspace_id=workspace_id)
            members = await repository.list_cohort(
                workspace_id=workspace_id,
                registry_id=challenger_registry_id,
                revision=challenger_revision,
                window_start=created_at - timedelta(days=policy.observation_window_days),
                window_end=created_at,
            )
            _validate_members(
                members=members,
                workspace_id=workspace_id,
                registry_id=challenger_registry_id,
                revision=challenger_revision,
            )
            members = await _load_member_source_ids(
                shadow_repository=shadow_repository,
                workspace_id=workspace_id,
                members=members,
            )
            if _member_source_identity(members) != _member_source_identity(initial_members):
                raise LearningDriftMembershipConflict(
                    "Exact cohort/source member set đổi khi lấy khóa; cần thử lại."
                )
            membership_digest = LearningDriftRepository.membership_digest(
                tuple(member.observation.observation_id for member in members)
            )
            checked_at = await shadow_repository.authorization_clock(workspace_id=workspace_id)
            members = await _authorize_members_current_scope(
                repository=repository,
                workspace_id=workspace_id,
                members=members,
                checked_at=checked_at,
            )
            source_rights_digest = _source_rights_digest(members)
            effective_observations = tuple(
                replace(
                    member.observation,
                    rights_eligible=member.current_rights_eligible is True,
                )
                for member in members
            )
            idempotency_key = LearningDriftRepository.assessment_idempotency_key(
                workspace_id=workspace_id,
                registry_id=challenger_registry_id,
                revision=challenger_revision,
                policy_version=policy.policy_version,
                membership_digest=membership_digest,
                source_rights_digest=source_rights_digest,
            )
            existing = await repository.find_assessment(
                workspace_id=workspace_id,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                _validate_existing(
                    existing=existing,
                    workspace_id=workspace_id,
                    registry_id=challenger_registry_id,
                    revision=challenger_revision,
                    policy_version=policy.policy_version,
                    membership_digest=membership_digest,
                    source_rights_digest=source_rights_digest,
                )
                return LearningDriftRun(
                    assessment=existing.assessment,
                    proposal=existing.proposal,
                    is_duplicate=True,
                )

            if created_at < max(member.observation.observed_at for member in members):
                raise LearningDriftTimestampConflict(
                    "transaction_timestamp cũ hơn observation mới nhất trong cohort."
                )
            assessment = assess_learning_drift(
                workspace_id=workspace_id,
                assessment_id=uuid4(),
                policy=policy,
                observations=effective_observations,
                created_at=created_at,
                source_rights_digest=source_rights_digest,
            )
            if assessment.membership_digest != membership_digest:
                raise LearningDriftIdempotencyConflict("Domain digest không khớp cohort đã khóa.")
            proposal = (
                create_learning_retrain_proposal(
                    assessment,
                    proposal_id=uuid4(),
                    created_at=created_at,
                )
                if assessment.data_quality_status is DriftAssessmentStatus.DETECTED
                else None
            )
            result = await repository.persist(
                assessment=assessment,
                members=members,
                proposal=proposal,
                policy=policy,
                registry_id=challenger_registry_id,
                revision=challenger_revision,
                idempotency_key=idempotency_key,
            )
            _validate_existing(
                existing=result,
                workspace_id=workspace_id,
                registry_id=challenger_registry_id,
                revision=challenger_revision,
                policy_version=policy.policy_version,
                membership_digest=membership_digest,
                source_rights_digest=source_rights_digest,
            )
            return LearningDriftRun(
                assessment=result.assessment,
                proposal=result.proposal,
                is_duplicate=result.is_duplicate,
            )


def _validate_members(
    *,
    members: tuple[LearningDriftCohortMember, ...],
    workspace_id: UUID,
    registry_id: UUID,
    revision: int,
) -> None:
    if type(members) is not tuple or any(
        type(member) is not LearningDriftCohortMember for member in members
    ):
        raise RuntimeError("Repository trả cohort không hợp lệ.")
    for member in members:
        observation = member.observation
        if (
            observation.workspace_id != workspace_id
            or observation.challenger_registry_id != registry_id
            or observation.challenger_revision != revision
        ):
            raise PermissionError("Repository trả observation ngoài cohort đã khóa.")
        if member.incumbent_decision_id is not None and member.incumbent_decision_id.int == 0:
            raise PermissionError("Repository trả incumbent decision ID không hợp lệ.")


def _validate_existing(
    *,
    existing: LearningDriftPersistenceResult,
    workspace_id: UUID,
    registry_id: UUID,
    revision: int,
    policy_version: str,
    membership_digest: str,
    source_rights_digest: str,
) -> None:
    assessment = existing.assessment
    if (
        assessment.workspace_id != workspace_id
        or assessment.policy_version != policy_version
        or assessment.membership_digest != membership_digest
        or assessment.source_rights_digest != source_rights_digest
        or assessment.member_ids != tuple(sorted(assessment.member_ids, key=str))
        or assessment.member_count != len(assessment.member_ids)
        or assessment.cohort_registry_id != registry_id
        or assessment.cohort_revision != revision
        or (assessment.data_quality_status is DriftAssessmentStatus.DETECTED)
        != (existing.proposal is not None)
    ):
        raise LearningDriftIdempotencyConflict(
            "Assessment tìm thấy không khớp policy hoặc membership cohort hiện hành."
        )


async def _load_member_source_ids(
    *,
    shadow_repository: LearningShadowRepository,
    workspace_id: UUID,
    members: tuple[LearningDriftCohortMember, ...],
) -> tuple[LearningDriftCohortMember, ...]:
    resolved: list[LearningDriftCohortMember] = []
    for member in members:
        decision_id = member.incumbent_decision_id
        source_ids = (
            await shadow_repository.decision_source_ids(
                workspace_id=workspace_id,
                decision_id=decision_id,
            )
            if decision_id is not None
            else ()
        )
        resolved.append(replace(member, source_ids=source_ids))
    return tuple(resolved)


async def _authorize_members_current_scope(
    *,
    repository: LearningDriftRepository,
    workspace_id: UUID,
    members: tuple[LearningDriftCohortMember, ...],
    checked_at: datetime,
) -> tuple[LearningDriftCohortMember, ...]:
    authorized: list[LearningDriftCohortMember] = []
    for member in members:
        decision_id = member.incumbent_decision_id
        fingerprint = (
            await repository.authorize_current_source_scope(
                workspace_id=workspace_id,
                decision_id=decision_id,
                checked_at=checked_at,
            )
            if decision_id is not None and member.source_ids
            else None
        )
        authorized.append(
            replace(
                member,
                current_rights_eligible=(
                    fingerprint is not None and member.observation.rights_eligible
                ),
                current_rights_fingerprint=(
                    fingerprint
                    if fingerprint is not None and member.observation.rights_eligible
                    else None
                ),
            )
        )
    return tuple(authorized)


def _member_source_identity(
    members: tuple[LearningDriftCohortMember, ...],
) -> tuple[tuple[UUID, UUID | None, tuple[str, ...]], ...]:
    return tuple(
        (
            member.observation.observation_id,
            member.incumbent_decision_id,
            member.source_ids,
        )
        for member in members
    )


def _source_rights_digest(members: tuple[LearningDriftCohortMember, ...]) -> str:
    document = [
        {
            "decision_id": (
                str(member.incumbent_decision_id)
                if member.incumbent_decision_id is not None
                else None
            ),
            "observation_id": str(member.observation.observation_id),
            "source_ids": list(member.source_ids),
            "source_scope_fingerprint": member.current_rights_fingerprint,
        }
        for member in sorted(members, key=lambda row: str(row.observation.observation_id))
    ]
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_scope(workspace_id: UUID, registry_id: UUID, revision: int) -> None:
    for name, value in (("workspace_id", workspace_id), ("challenger_registry_id", registry_id)):
        if type(value) is not UUID or value.int == 0:
            raise ValueError(f"{name} phải là UUID khác zero.")
    if type(revision) is not int or revision < 1:
        raise ValueError("challenger_revision phải là số nguyên dương.")


__all__ = [
    "LearningDriftApplication",
    "LearningDriftIdempotencyConflict",
    "LearningDriftMembershipConflict",
    "LearningDriftRightsConflict",
    "LearningDriftRun",
    "LearningDriftTimestampConflict",
]
