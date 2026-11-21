"""Chặn một quyết định PAPER đã được scorer phê duyệt bằng yêu cầu có MFA."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.errors import database_sqlstate
from trading_bot.adapters.database.repositories.learning_scorer_paper_entry_blocks import (
    LearningPaperEntryBlockConflict,
    LearningPaperEntryBlockRepository,
    PaperEntryBlockEvidence,
)
from trading_bot.adapters.database.repositories.learning_scorer_runtime_targets import (
    PostgresScorerRuntimeTargetRepository,
    ResolvedScorerRuntimeTarget,
)
from trading_bot.adapters.database.repositories.learning_scoring_deployments import (
    ScorerDeploymentRightsUnavailable,
)
from trading_bot.adapters.database.repositories.workspace_settings import (
    WorkspaceSettingsRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import (
    RECENT_MFA_LIFETIME,
    MfaMethod,
    Permission,
    Principal,
    authorize,
)
from trading_bot.domain.learning.paper_entry_block import (
    LearningPaperEntryBlockEvent,
    normalize_block_reason,
)
from trading_bot.domain.learning.scoring_deployment import ScorerDeploymentMode


class LearningPaperEntryBlockService:
    """Gắn workspace và người thực hiện từ Principal đã xác thực."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        self._sessions = sessions

    def for_principal(self, principal: Principal) -> PrincipalLearningPaperEntryBlockService:
        if type(principal) is not Principal:
            raise TypeError("Principal phải đến từ lớp xác thực.")
        authorize(principal, Permission.LEARNING_PAPER_ENTRY_BLOCK, raise_error=True)
        return PrincipalLearningPaperEntryBlockService(self._sessions, principal)


class PrincipalLearningPaperEntryBlockService:
    """Giữ khóa, đối chiếu dữ liệu DB và ghi bản ghi audit trong một transaction."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        principal: Principal,
    ) -> None:
        self._sessions = sessions
        self._principal = principal

    async def eligible_decision_ids(self, bot_decision_ids: Sequence[UUID]) -> frozenset[UUID]:
        """Đọc các quyết định còn đủ điều kiện để trang Admin có thể hiện thao tác."""
        principal = self._principal
        authorize(principal, Permission.LEARNING_PAPER_ENTRY_BLOCK, raise_error=True)
        _require_recent_totp(principal, datetime.now(UTC))
        if not isinstance(bot_decision_ids, Sequence) or isinstance(bot_decision_ids, (str, bytes)):
            raise ValueError("Danh sách BotDecision không hợp lệ.")
        decision_ids = tuple(dict.fromkeys(bot_decision_ids))
        if any(
            type(decision_id) is not UUID or decision_id.int == 0 for decision_id in decision_ids
        ):
            raise ValueError("BotDecision phải có UUID khác zero.")
        if not decision_ids:
            return frozenset()

        workspace_id = principal.workspace_id
        eligible: set[UUID] = set()
        async with UnitOfWork(
            self._sessions,
            workspace_id=workspace_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            ledger = LearningPaperEntryBlockRepository(unit.session)
            active = await _resolve_current_target(unit.session, workspace_id)
            if active is None or active.mode is not ScorerDeploymentMode.PAPER_GATE:
                return frozenset()
            for bot_decision_id in decision_ids:
                if (
                    await ledger.find_by_decision(
                        workspace_id=workspace_id, bot_decision_id=bot_decision_id
                    )
                    is not None
                ):
                    continue
                candidate = await ledger.load_blockable_evidence(
                    workspace_id=workspace_id, bot_decision_id=bot_decision_id
                )
                if candidate is None:
                    continue
                if await ledger.has_paper_order_intent(
                    workspace_id=workspace_id,
                    bot_id=candidate.bot_id,
                    bot_decision_id=bot_decision_id,
                ):
                    continue
                if _active_target_matches(candidate, active):
                    eligible.add(bot_decision_id)
        return frozenset(eligible)

    async def block(
        self, *, bot_decision_id: UUID, reason: str, idempotency_key: UUID
    ) -> LearningPaperEntryBlockEvent:
        principal = self._principal
        authorize(principal, Permission.LEARNING_PAPER_ENTRY_BLOCK, raise_error=True)
        _require_recent_totp(principal, datetime.now(UTC))
        if type(bot_decision_id) is not UUID or bot_decision_id.int == 0:
            raise ValueError("Quyết định phải có UUID khác zero.")
        if type(idempotency_key) is not UUID or idempotency_key.int == 0:
            raise ValueError("Khóa idempotency phải có UUID khác zero.")
        normalized_reason = normalize_block_reason(reason)
        workspace_id = principal.workspace_id
        async with UnitOfWork(
            self._sessions, workspace_id=workspace_id, actor_membership_id=principal.membership_id
        ) as unit:
            await WorkspaceSettingsRepository(unit.session).lock_workspace(workspace_id)
            ledger = LearningPaperEntryBlockRepository(unit.session)
            await ledger.lock_decision(workspace_id=workspace_id, bot_decision_id=bot_decision_id)
            _require_recent_totp(principal, datetime.now(UTC))
            existing = await ledger.find_by_idempotency_key(
                workspace_id=workspace_id, idempotency_key=idempotency_key
            )
            if existing is not None:
                if (
                    existing.bot_decision_id != bot_decision_id
                    or existing.actor_user_id != principal.user_id
                    or existing.reason != normalized_reason
                ):
                    raise LearningPaperEntryBlockConflict(
                        "Khóa idempotency đã dùng cho ý định chặn khác."
                    )
                return existing

            candidate = await ledger.load_blockable_evidence(
                workspace_id=workspace_id, bot_decision_id=bot_decision_id
            )
            if candidate is None:
                raise LearningPaperEntryBlockConflict(
                    "Không tìm thấy quyết định PAPER_GATE APPROVE đủ điều kiện chặn."
                )
            if await ledger.has_paper_order_intent(
                workspace_id=workspace_id, bot_id=candidate.bot_id, bot_decision_id=bot_decision_id
            ):
                raise LearningPaperEntryBlockConflict(
                    "Quyết định đã gửi OrderIntent. Thao tác chặn đã quá muộn."
                )
            active = await _resolve_current_target(unit.session, workspace_id)
            if active is None or not _active_target_matches(candidate, active):
                raise LearningPaperEntryBlockConflict(
                    "Target scorer của quyết định không còn hiệu lực."
                )
            target = active.target
            created_at = datetime.now(UTC)
            verified_at = _require_recent_totp(principal, created_at)
            event = LearningPaperEntryBlockEvent(
                event_id=uuid4(),
                workspace_id=workspace_id,
                bot_id=candidate.bot_id,
                bot_decision_id=bot_decision_id,
                bot_decision_hash=candidate.bot_decision_hash,
                candidate_id=candidate.candidate_id,
                score_event_id=candidate.score_event_id,
                score_event_hash=candidate.score_event_hash,
                deployment_event_id=active.deployment_event_id,
                deployment_revision=active.deployment_revision,
                registry_id=target.registry_id,
                registry_revision=target.registry_revision,
                active_pointer_version=target.active_pointer_version,
                scorer_artifact_id=target.scorer_artifact_id,
                scorer_artifact_hash=target.scorer_artifact_hash,
                actor_user_id=principal.user_id,
                mfa_verified_at=verified_at,
                reason=normalized_reason,
                created_at=created_at,
                idempotency_key=idempotency_key,
            )
            return (await ledger.append_block(workspace_id=workspace_id, event=event)).event


def _active_target_matches(
    candidate: PaperEntryBlockEvidence,
    active: ResolvedScorerRuntimeTarget | None,
) -> bool:
    """Dùng chung một phép đối chiếu target cho nút xem trước và lệnh ghi."""
    return bool(
        active is not None
        and active.mode is ScorerDeploymentMode.PAPER_GATE
        and active.deployment_event_id == candidate.deployment_event_id
        and active.deployment_revision == candidate.deployment_revision
        and active.source_rights_fingerprint == candidate.source_rights_fingerprint
        and active.target.registry_id == candidate.registry_id
        and active.target.registry_revision == candidate.registry_revision
        and active.target.active_pointer_version == candidate.active_pointer_version
        and active.target.scorer_artifact_id == candidate.scorer_artifact_id
        and active.target.scorer_artifact_hash == candidate.scorer_artifact_hash
    )


async def _resolve_current_target(
    session: AsyncSession, workspace_id: UUID
) -> ResolvedScorerRuntimeTarget | None:
    try:
        return await PostgresScorerRuntimeTargetRepository(session).resolve_active(
            workspace_id=workspace_id
        )
    except ScorerDeploymentRightsUnavailable as error:
        raise LearningPaperEntryBlockConflict(
            "Không xác minh được target scorer hiện hành."
        ) from error
    except DBAPIError as error:
        # Gateway báo từ chối ở DB bằng RAISE EXCEPTION. Lỗi kết nối/SQL khác để caller xử lý.
        if database_sqlstate(error) != "P0001":
            raise
        raise LearningPaperEntryBlockConflict(
            "Không xác minh được target scorer hiện hành."
        ) from error


def _require_recent_totp(principal: Principal, now: datetime) -> datetime:
    verified_at = principal.mfa_verified_at
    if (
        principal.mfa_method is not MfaMethod.TOTP
        or not isinstance(verified_at, datetime)
        or verified_at.utcoffset() is None
        or verified_at > now
        or now - verified_at > RECENT_MFA_LIFETIME
    ):
        raise PermissionError("Cần xác thực TOTP gần đây để chặn entry PAPER.")
    return verified_at.astimezone(UTC)
