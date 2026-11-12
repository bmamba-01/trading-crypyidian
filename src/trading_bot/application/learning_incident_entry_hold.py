"""Use case xác nhận và giải phóng entry hold do learning incident."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.learning_incident_entry_holds import (
    LearningIncidentEntryHoldRepository,
    LearningIncidentHoldConflict,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import (
    RECENT_MFA_LIFETIME,
    MfaMethod,
    Permission,
    Principal,
    authorize,
)
from trading_bot.domain.learning.incident_entry_hold import (
    LearningIncidentHoldAction,
    LearningIncidentHoldEvent,
    create_hold_event,
    create_release_event,
)

UnitOfWorkFactory = Callable[..., UnitOfWork]
RepositoryFactory = Callable[[AsyncSession], LearningIncidentEntryHoldRepository]


@dataclass(frozen=True, slots=True)
class LearningIncidentHoldCandidate:
    """Proposal mới nhất đủ điều kiện hiển thị thao tác HOLD."""

    proposal_id: UUID
    assessment_id: UUID
    assessment_hash: str
    reason_codes: tuple[str, ...]
    created_at: datetime
    expected_revision: int
    expected_deployment_revision: int
    expected_active_pointer_version: int


class LearningIncidentEntryHoldService:
    """Tạo facade cố định workspace/actor từ Principal đã xác thực."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        unit_of_work_factory: UnitOfWorkFactory = UnitOfWork,
        repository_factory: RepositoryFactory = LearningIncidentEntryHoldRepository,
    ) -> None:
        if not callable(unit_of_work_factory) or not callable(repository_factory):
            raise ValueError("Factory UnitOfWork và repository phải gọi được.")
        self._sessions = sessions
        self._unit_of_work_factory = unit_of_work_factory
        self._repository_factory = repository_factory

    def for_principal(self, principal: Principal) -> PrincipalLearningIncidentEntryHoldService:
        if type(principal) is not Principal:
            raise TypeError("principal phải đến từ authentication boundary.")
        if not authorize(principal, Permission.LEARNING_INCIDENT_MANAGE):
            raise PermissionError("Principal không có quyền quản lý learning incident.")
        return PrincipalLearningIncidentEntryHoldService(
            self._sessions,
            principal,
            unit_of_work_factory=self._unit_of_work_factory,
            repository_factory=self._repository_factory,
        )


class PrincipalLearningIncidentEntryHoldService:
    """Thực thi command/query với scope bất biến lấy từ Principal."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        principal: Principal,
        *,
        unit_of_work_factory: UnitOfWorkFactory,
        repository_factory: RepositoryFactory,
    ) -> None:
        self._sessions = sessions
        self._principal = principal
        self._unit_of_work_factory = unit_of_work_factory
        self._repository_factory = repository_factory

    async def hold(
        self,
        *,
        proposal_id: UUID,
        assessment_id: UUID,
        assessment_hash: str,
        expected_revision: int,
        expected_deployment_revision: int,
        expected_active_pointer_version: int,
        idempotency_key: UUID,
        reason: str,
    ) -> LearningIncidentHoldEvent:
        verified_at = self._require_recent_mfa()
        _validate_hold_request(
            proposal_id=proposal_id,
            assessment_id=assessment_id,
            assessment_hash=assessment_hash,
            expected_revision=expected_revision,
            expected_deployment_revision=expected_deployment_revision,
            expected_active_pointer_version=expected_active_pointer_version,
            idempotency_key=idempotency_key,
            reason=reason,
        )
        normalized_reason = reason.strip()
        async with self._uow() as unit:
            repository = self._repository_factory(unit.session)
            await repository.lock_runtime_entry_scope(workspace_id=self._principal.workspace_id)
            existing = await repository.find_by_idempotency_key(
                workspace_id=self._principal.workspace_id,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                if not _matches_hold_retry(
                    existing,
                    workspace_id=self._principal.workspace_id,
                    actor_membership_id=self._principal.membership_id,
                    proposal_id=proposal_id,
                    assessment_id=assessment_id,
                    assessment_hash=assessment_hash,
                    expected_revision=expected_revision,
                    expected_deployment_revision=expected_deployment_revision,
                    expected_active_pointer_version=expected_active_pointer_version,
                    idempotency_key=idempotency_key,
                    reason=normalized_reason,
                ):
                    raise LearningIncidentHoldConflict(
                        "Idempotency key đã dùng cho ý định incident khác."
                    )
                return existing
            if expected_revision != 0:
                raise LearningIncidentHoldConflict("Revision incident đã cũ hoặc chưa hợp lệ.")
            history = await repository.list_history(
                workspace_id=self._principal.workspace_id,
                proposal_id=proposal_id,
                limit=2,
            )
            if history:
                raise LearningIncidentHoldConflict(
                    "Proposal đã có HOLD hoặc RELEASE; cần proposal mới để xác nhận lại."
                )
            eligible = await repository.eligible_incident_target(
                workspace_id=self._principal.workspace_id,
                proposal_id=proposal_id,
            )
            if (
                eligible.proposal.proposal_id != proposal_id
                or eligible.assessment.assessment_id != assessment_id
                or eligible.assessment.content_hash != assessment_hash
            ):
                raise LearningIncidentHoldConflict(
                    "Assessment đã đổi hoặc không còn khớp proposal hiện hành."
                )
            target = eligible.target
            if (
                target.deployment_revision != expected_deployment_revision
                or target.active_pointer_version != expected_active_pointer_version
            ):
                raise LearningIncidentHoldConflict(
                    "CAS deployment scorer đã đổi; hãy tải lại target hiện hành."
                )
            event = create_hold_event(
                event_id=uuid4(),
                workspace_id=self._principal.workspace_id,
                proposal=eligible.proposal,
                assessment=eligible.assessment,
                target=target,
                incumbent_artifact_hash=eligible.incumbent_artifact_hash,
                revision=expected_revision + 1,
                previous_event=None,
                actor_membership_id=self._principal.membership_id,
                mfa_verified_at=verified_at,
                mfa_method=MfaMethod.TOTP,
                reason=normalized_reason,
                idempotency_key=idempotency_key,
                request_id=str(unit.request_id),
                created_at=datetime.now(UTC),
            )
            appended = await repository.append_event(
                workspace_id=self._principal.workspace_id,
                event=event,
            )
            if not appended.is_duplicate:
                unit.record_audit(
                    action="learning.incident_entry_hold.hold",
                    target_type="learning_incident_entry_hold",
                    target_id=event.proposal_id,
                    reason=event.reason,
                    after_state=event.canonical_payload,
                )
            return appended.event

    async def release(
        self,
        *,
        proposal_id: UUID,
        expected_revision: int,
        idempotency_key: UUID,
        reason: str,
    ) -> LearningIncidentHoldEvent:
        verified_at = self._require_recent_mfa()
        _validate_release_request(
            proposal_id=proposal_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            reason=reason,
        )
        normalized_reason = reason.strip()
        async with self._uow() as unit:
            repository = self._repository_factory(unit.session)
            await repository.lock_runtime_entry_scope(workspace_id=self._principal.workspace_id)
            existing = await repository.find_by_idempotency_key(
                workspace_id=self._principal.workspace_id,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                if not _matches_release_retry(
                    existing,
                    workspace_id=self._principal.workspace_id,
                    actor_membership_id=self._principal.membership_id,
                    proposal_id=proposal_id,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    reason=normalized_reason,
                ):
                    raise LearningIncidentHoldConflict(
                        "Idempotency key đã dùng cho ý định incident khác."
                    )
                return existing
            if expected_revision != 1:
                raise LearningIncidentHoldConflict("Revision release incident đã cũ.")
            history = await repository.list_history(
                workspace_id=self._principal.workspace_id,
                proposal_id=proposal_id,
                limit=2,
            )
            if not history or history[0].action is not LearningIncidentHoldAction.HOLD:
                raise LearningIncidentHoldConflict(
                    "Proposal không có HOLD đang mở hoặc RELEASE đã là cuối."
                )
            previous = history[0]
            event = create_release_event(
                event_id=uuid4(),
                previous_event=previous,
                actor_membership_id=self._principal.membership_id,
                mfa_verified_at=verified_at,
                mfa_method=MfaMethod.TOTP,
                reason=normalized_reason,
                idempotency_key=idempotency_key,
                request_id=str(unit.request_id),
                created_at=datetime.now(UTC),
            )
            appended = await repository.append_event(
                workspace_id=self._principal.workspace_id,
                event=event,
            )
            if not appended.is_duplicate:
                unit.record_audit(
                    action="learning.incident_entry_hold.release",
                    target_type="learning_incident_entry_hold",
                    target_id=event.proposal_id,
                    reason=event.reason,
                    before_state=previous.canonical_payload,
                    after_state=event.canonical_payload,
                )
            return appended.event

    async def list_history(self, *, limit: int = 200) -> tuple[LearningIncidentHoldEvent, ...]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit lịch sử phải trong [1, 200].")
        async with self._uow(snapshot_read_only=True) as unit:
            return await self._repository_factory(unit.session).list_history(
                workspace_id=self._principal.workspace_id,
                limit=limit,
            )

    async def list_open_holds(self, *, limit: int = 100) -> tuple[LearningIncidentHoldEvent, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit hold đang mở phải trong [1, 100].")
        async with self._uow(snapshot_read_only=True) as unit:
            return await self._repository_factory(unit.session).list_open_holds(
                workspace_id=self._principal.workspace_id,
                limit=limit,
            )

    async def list_actionable_proposals(
        self, *, limit: int = 20
    ) -> tuple[LearningIncidentHoldCandidate, ...]:
        """Chỉ trả proposal chưa có ledger và vượt gateway target/quyền hiện hành."""
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit proposal phải trong [1, 20].")
        async with self._uow() as unit:
            repository = self._repository_factory(unit.session)
            proposal_ids = await repository.list_recent_proposal_ids(
                workspace_id=self._principal.workspace_id,
                limit=100,
            )
            candidates: list[LearningIncidentHoldCandidate] = []
            for proposal_id in proposal_ids:
                if await repository.list_history(
                    workspace_id=self._principal.workspace_id,
                    proposal_id=proposal_id,
                    limit=1,
                ):
                    continue
                try:
                    eligible = await repository.eligible_incident_target(
                        workspace_id=self._principal.workspace_id,
                        proposal_id=proposal_id,
                    )
                except LearningIncidentHoldConflict:
                    continue
                candidates.append(
                    LearningIncidentHoldCandidate(
                        proposal_id=eligible.proposal.proposal_id,
                        assessment_id=eligible.assessment.assessment_id,
                        assessment_hash=eligible.assessment.content_hash,
                        reason_codes=tuple(
                            reason.value for reason in eligible.proposal.reason_codes
                        ),
                        created_at=eligible.proposal.created_at,
                        expected_revision=0,
                        expected_deployment_revision=eligible.target.deployment_revision,
                        expected_active_pointer_version=eligible.target.active_pointer_version,
                    )
                )
                if len(candidates) == limit:
                    break
            return tuple(candidates)

    def _require_recent_mfa(self) -> datetime:
        principal = self._principal
        if not authorize(principal, Permission.LEARNING_INCIDENT_MANAGE):
            raise PermissionError("Principal không có quyền quản lý learning incident.")
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
            raise PermissionError("Cần xác thực TOTP gần đây để quản lý learning incident.")
        return verified_at.astimezone(UTC)

    def _uow(self, *, snapshot_read_only: bool = False) -> UnitOfWork:
        return self._unit_of_work_factory(
            self._sessions,
            workspace_id=self._principal.workspace_id,
            actor_membership_id=self._principal.membership_id,
            snapshot_read_only=snapshot_read_only,
        )


def _validate_hold_request(
    *,
    proposal_id: UUID,
    assessment_id: UUID,
    assessment_hash: str,
    expected_revision: int,
    expected_deployment_revision: int,
    expected_active_pointer_version: int,
    idempotency_key: UUID,
    reason: str,
) -> None:
    if type(proposal_id) is not UUID or type(assessment_id) is not UUID:
        raise ValueError("Proposal và assessment phải có UUID hợp lệ.")
    if type(idempotency_key) is not UUID:
        raise ValueError("Idempotency key phải là UUID.")
    if not isinstance(assessment_hash, str) or len(assessment_hash) != 64:
        raise ValueError("Assessment hash phải là SHA-256.")
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("expected_revision phải là số nguyên không âm.")
    if type(expected_deployment_revision) is not int or expected_deployment_revision < 1:
        raise ValueError("expected_deployment_revision phải là số nguyên dương.")
    if type(expected_active_pointer_version) is not int or expected_active_pointer_version < 1:
        raise ValueError("expected_active_pointer_version phải là số nguyên dương.")
    _validate_reason(reason)


def _validate_release_request(
    *, proposal_id: UUID, expected_revision: int, idempotency_key: UUID, reason: str
) -> None:
    if type(proposal_id) is not UUID or type(idempotency_key) is not UUID:
        raise ValueError("Proposal và idempotency key phải có UUID hợp lệ.")
    if type(expected_revision) is not int or expected_revision < 1:
        raise ValueError("expected_revision phải là số nguyên dương.")
    _validate_reason(reason)


def _validate_reason(reason: str) -> None:
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or len(reason) > 500
        or any(ord(char) < 32 and char not in "\t\n\r" for char in reason)
    ):
        raise ValueError("reason phải có 1–500 ký tự và không rỗng.")


def _matches_hold_retry(
    event: LearningIncidentHoldEvent,
    *,
    workspace_id: UUID,
    actor_membership_id: UUID,
    proposal_id: UUID,
    assessment_id: UUID,
    assessment_hash: str,
    expected_revision: int,
    expected_deployment_revision: int,
    expected_active_pointer_version: int,
    idempotency_key: UUID,
    reason: str,
) -> bool:
    return (
        event.action is LearningIncidentHoldAction.HOLD
        and event.workspace_id == workspace_id
        and event.actor_membership_id == actor_membership_id
        and event.proposal_id == proposal_id
        and event.assessment_id == assessment_id
        and event.assessment_hash == assessment_hash
        and event.revision - 1 == expected_revision
        and event.deployment_revision == expected_deployment_revision
        and event.active_pointer_version == expected_active_pointer_version
        and event.idempotency_key == idempotency_key
        and event.reason == reason
    )


def _matches_release_retry(
    event: LearningIncidentHoldEvent,
    *,
    workspace_id: UUID,
    actor_membership_id: UUID,
    proposal_id: UUID,
    expected_revision: int,
    idempotency_key: UUID,
    reason: str,
) -> bool:
    return (
        event.action is LearningIncidentHoldAction.RELEASE
        and event.workspace_id == workspace_id
        and event.actor_membership_id == actor_membership_id
        and event.proposal_id == proposal_id
        and event.revision - 1 == expected_revision
        and event.idempotency_key == idempotency_key
        and event.reason == reason
    )


__all__ = [
    "LearningIncidentHoldCandidate",
    "LearningIncidentEntryHoldService",
    "PrincipalLearningIncidentEntryHoldService",
]
