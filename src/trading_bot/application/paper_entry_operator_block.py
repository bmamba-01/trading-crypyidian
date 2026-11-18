"""Dịch vụ chặn thêm PAPER entry theo BotDecision với quyền owner/risk-admin + MFA (CRY-440)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.paper_entry_operator_blocks import (
    OperatorEntryBlockError,
    PaperEntryOperatorBlockRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import MfaMethod, Permission, Principal, authorize
from trading_bot.auth.service import RECENT_MFA_LIFETIME


@dataclass(frozen=True, slots=True)
class PaperEntryBlockSummary:
    """Thông tin tóm tắt bản ghi chặn entry hiển thị trên Admin."""

    block_id: UUID
    bot_id: UUID
    bot_decision_id: UUID
    blocked_by: UUID
    reason: str
    mfa_verified_at: datetime
    created_at: datetime


class PaperEntryOperatorBlockService:
    """Cho phép owner/risk-admin chặn một BotDecision APPROVE trước khi tạo OrderIntent."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def list_recent(
        self, principal: Principal, *, limit: int = 20
    ) -> tuple[PaperEntryBlockSummary, ...]:
        if not authorize(principal, Permission.LEARNING_MODEL_READ):
            raise OperatorEntryBlockError(
                "FORBIDDEN",
                "Không đủ quyền xem lịch sử chặn entry.",
                status_code=403,
            )
        async with UnitOfWork(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            rows = await PaperEntryOperatorBlockRepository(unit.session).list_recent(
                workspace_id=principal.workspace_id,
                limit=limit,
            )
        return tuple(
            PaperEntryBlockSummary(
                block_id=row.id,
                bot_id=row.bot_id,
                bot_decision_id=row.bot_decision_id,
                blocked_by=row.blocked_by,
                reason=row.reason,
                mfa_verified_at=row.mfa_verified_at,
                created_at=row.created_at,
            )
            for row in rows
        )

    async def block_paper_entry(
        self,
        principal: Principal,
        *,
        bot_decision_id: UUID,
        reason: str,
        now: datetime | None = None,
    ) -> PaperEntryBlockSummary:
        """Chặn thêm PAPER entry cho ``bot_decision_id`` sau khi xác thực quyền và MFA TOTP."""
        # Chỉ owner hoặc risk_admin (có WORKSPACE_KILL / RISK_PROFILE_WRITE) được chặn thêm entry.
        if not (
            authorize(principal, Permission.WORKSPACE_KILL)
            and authorize(principal, Permission.RISK_PROFILE_WRITE)
        ):
            raise OperatorEntryBlockError(
                "FORBIDDEN",
                "Chỉ owner hoặc risk-admin mới có quyền chặn thêm PAPER entry.",
                status_code=403,
            )
        evaluated_at = now or datetime.now(UTC)
        if (
            principal.mfa_verified_at is None
            or principal.mfa_method is not MfaMethod.TOTP
            or evaluated_at - principal.mfa_verified_at > RECENT_MFA_LIFETIME
        ):
            raise OperatorEntryBlockError(
                "MFA_REQUIRED",
                "Yêu cầu xác thực TOTP MFA gần đây để chặn thêm PAPER entry.",
                status_code=403,
            )
        correlation_id = uuid4()
        async with UnitOfWork(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
        ) as unit:
            repo = PaperEntryOperatorBlockRepository(unit.session)
            record = await repo.block_decision(
                workspace_id=principal.workspace_id,
                bot_decision_id=bot_decision_id,
                blocked_by=principal.membership_id,
                reason=reason,
                mfa_verified_at=principal.mfa_verified_at,
                correlation_id=correlation_id,
            )
            unit.record_audit(
                action="learning.paper_entry_operator_blocked",
                target_type="bot_decision",
                target_id=bot_decision_id,
                reason=record.reason,
                after_state={
                    "bot_id": str(record.bot_id),
                    "bot_decision_id": str(record.bot_decision_id),
                    "block_id": str(record.id),
                },
            )
            return PaperEntryBlockSummary(
                block_id=record.id,
                bot_id=record.bot_id,
                bot_decision_id=record.bot_decision_id,
                blocked_by=record.blocked_by,
                reason=record.reason,
                mfa_verified_at=record.mfa_verified_at,
                created_at=record.created_at,
            )


__all__ = [
    "OperatorEntryBlockError",
    "PaperEntryBlockSummary",
    "PaperEntryOperatorBlockService",
]
