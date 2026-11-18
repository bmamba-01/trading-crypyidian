"""Repository PostgreSQL cho action_challenge (step-up SEC-20)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import ActionChallengeRecord, MfaFactor, Session


class ActionChallengeRepository:
    """Truy cập `action_challenge` với khóa hàng `FOR UPDATE` và tiêu thụ nguyên tử."""

    def __init__(self, session_or_uow: Any) -> None:
        self._session: AsyncSession = getattr(session_or_uow, "session", session_or_uow)

    async def get_active_session(
        self,
        *,
        workspace_id: UUID,
        membership_id: UUID,
        session_id: UUID,
        now: datetime,
    ) -> Session | None:
        return cast(
            Session | None,
            await self._session.scalar(
                select(Session).where(
                    Session.workspace_id == workspace_id,
                    Session.membership_id == membership_id,
                    Session.id == session_id,
                    Session.revoked_at.is_(None),
                    Session.expires_at > now,
                )
            ),
        )

    async def create(
        self,
        *,
        workspace_id: UUID,
        membership_id: UUID,
        session_id: UUID,
        action: str,
        payload_hash: str,
        created_at: datetime,
        expires_at: datetime,
        max_attempts: int,
    ) -> ActionChallengeRecord:
        record = ActionChallengeRecord(
            id=uuid4(),
            created_at=created_at,
            workspace_id=workspace_id,
            membership_id=membership_id,
            session_id=session_id,
            action=action,
            payload_hash=payload_hash,
            status="PENDING",
            failed_attempts=0,
            max_attempts=max_attempts,
            verification_token_hash=None,
            expires_at=expires_at,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def get_for_update(
        self, *, workspace_id: UUID, challenge_id: UUID
    ) -> ActionChallengeRecord | None:
        return cast(
            ActionChallengeRecord | None,
            await self._session.scalar(
                select(ActionChallengeRecord)
                .where(
                    ActionChallengeRecord.workspace_id == workspace_id,
                    ActionChallengeRecord.id == challenge_id,
                )
                .with_for_update()
            ),
        )

    async def get_totp_factor_for_update(
        self, *, workspace_id: UUID, membership_id: UUID
    ) -> MfaFactor | None:
        return cast(
            MfaFactor | None,
            await self._session.scalar(
                select(MfaFactor)
                .where(
                    MfaFactor.workspace_id == workspace_id,
                    MfaFactor.membership_id == membership_id,
                    MfaFactor.factor_type == "TOTP",
                    MfaFactor.verified_at.is_not(None),
                )
                .with_for_update()
            ),
        )

    async def consume_verified_atomic(
        self,
        *,
        workspace_id: UUID,
        membership_id: UUID,
        session_id: UUID,
        challenge_id: UUID,
        action: str,
        payload_hash: str,
        token_hash: str,
        now: datetime,
    ) -> ActionChallengeRecord | None:
        """Tiêu thụ nguyên tử bằng UPDATE điều kiện chặt để chống race condition/replay."""
        stmt = (
            update(ActionChallengeRecord)
            .where(
                ActionChallengeRecord.workspace_id == workspace_id,
                ActionChallengeRecord.membership_id == membership_id,
                ActionChallengeRecord.session_id == session_id,
                ActionChallengeRecord.id == challenge_id,
                ActionChallengeRecord.action == action,
                ActionChallengeRecord.payload_hash == payload_hash,
                ActionChallengeRecord.status == "VERIFIED",
                ActionChallengeRecord.consumed_at.is_(None),
                ActionChallengeRecord.cancelled_at.is_(None),
                ActionChallengeRecord.expires_at > now,
                ActionChallengeRecord.verification_token_hash == token_hash,
            )
            .values(
                status="CONSUMED",
                consumed_at=now,
                verification_token_hash=None,
            )
            .returning(ActionChallengeRecord)
        )
        return cast(ActionChallengeRecord | None, await self._session.scalar(stmt))
