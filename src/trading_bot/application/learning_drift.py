"""Tính assessment dữ liệu từ cohort shadow đã lưu, không kích hoạt huấn luyện."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.learning_drift import (
    LearningDriftCohortMember,
    LearningDriftPersistenceResult,
    LearningDriftRepository,
)
from trading_bot.adapters.database.repositories.learning_shadow import (
    LearningShadowRepository,
    PaperSourceRightsReceipt,
)
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

            # Lấy shared source lock trước cohort lock theo thứ tự observer 7A.
            rights_receipt = await shadow_repository.current_paper_rights(
                workspace_id=workspace_id,
                source_id="BINANCE_USDM_REST",
            )

            await repository.lock_cohort(
                workspace_id=workspace_id,
                registry_id=challenger_registry_id,
                revision=challenger_revision,
            )
            created_at = await repository.transaction_timestamp(workspace_id=workspace_id)
            members = await repository.list_cohort(
                workspace_id=workspace_id,
                registry_id=challenger_registry_id,
                revision=challenger_revision,
                window_start=created_at - timedelta(days=policy.observation_window_days),
                window_end=created_at,
            )
            # Không lưu cohort rỗng vì ledger không có cột ghim scope được yêu cầu.
            if not members:
                return None
            _validate_members(
                members=members,
                workspace_id=workspace_id,
                registry_id=challenger_registry_id,
                revision=challenger_revision,
                rights_receipt=rights_receipt,
            )
            membership_digest = LearningDriftRepository.membership_digest(
                tuple(member.observation.observation_id for member in members)
            )
            idempotency_key = LearningDriftRepository.assessment_idempotency_key(
                workspace_id=workspace_id,
                registry_id=challenger_registry_id,
                revision=challenger_revision,
                policy_version=policy.policy_version,
                membership_digest=membership_digest,
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
                observations=tuple(member.observation for member in members),
                created_at=created_at,
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
    rights_receipt: PaperSourceRightsReceipt | None,
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
        if observation.rights_eligible and (
            rights_receipt is None or member.rights_fingerprint != rights_receipt.fingerprint
        ):
            raise LearningDriftRightsConflict(
                "receipt quyền hiện hành thiếu hoặc không khớp observation đủ quyền trong cohort."
            )


def _validate_existing(
    *,
    existing: LearningDriftPersistenceResult,
    workspace_id: UUID,
    registry_id: UUID,
    revision: int,
    policy_version: str,
    membership_digest: str,
) -> None:
    assessment = existing.assessment
    if (
        assessment.workspace_id != workspace_id
        or assessment.policy_version != policy_version
        or assessment.membership_digest != membership_digest
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


def _validate_scope(workspace_id: UUID, registry_id: UUID, revision: int) -> None:
    for name, value in (("workspace_id", workspace_id), ("challenger_registry_id", registry_id)):
        if type(value) is not UUID or value.int == 0:
            raise ValueError(f"{name} phải là UUID khác zero.")
    if type(revision) is not int or revision < 1:
        raise ValueError("challenger_revision phải là số nguyên dương.")


__all__ = [
    "LearningDriftApplication",
    "LearningDriftIdempotencyConflict",
    "LearningDriftRightsConflict",
    "LearningDriftRun",
    "LearningDriftTimestampConflict",
]
