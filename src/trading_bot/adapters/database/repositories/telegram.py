"""Repository Telegram cho admin-web (MB-07a, ruling 37–38).

- `TelegramUpdateReceiptRepository.claim`: INSERT … ON CONFLICT DO NOTHING vào
  `telegram_update_receipt` (unique `(workspace_id, update_id)`); trả False khi đã nhận.
- `TelegramBindingRepository.resolve`: binding đã xác minh, chưa thu hồi, membership ACTIVE, user
  ACTIVE và có role trong đúng workspace của transaction (RLS `app.workspace_id`). Không có binding
  hợp lệ → None (cùng kết quả cho user lạ, bị khóa, binding bị thu hồi).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    RoleBinding,
    TelegramBinding,
    TelegramBindingChallenge,
    TelegramLinkRequest,
    TelegramUpdateReceipt,
    UserAccount,
    UserMembership,
)
from trading_bot.auth.policy import Role

# Khớp policy `receipt_purge` của migration f7b9d1e3a5c8 (`interval '7 days'`).
RECEIPT_RETENTION = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class TelegramBindingIdentity:
    """Danh tính đã resolve từ binding; không chứa Telegram user id hay dữ liệu cá nhân khác."""

    user_id: UUID
    membership_id: UUID
    workspace_id: UUID
    roles: frozenset[Role]


class TelegramUpdateReceiptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(self, workspace_id: UUID, update_id: int) -> bool:
        # Retention (fix round 1 minor 3): xóa receipt quá hạn trong cùng transaction trước khi
        # claim. Telegram chọn `update_id` ngẫu nhiên sau ≥1 tuần không có update, nên receipt cũ
        # hơn 7 ngày không còn giá trị chống replay mà có thể chặn nhầm update mới. Policy DB
        # `receipt_purge` chỉ cho xóa hàng quá 7 ngày (hàng gần đây vẫn bất biến).
        await self._session.execute(
            delete(TelegramUpdateReceipt).where(
                TelegramUpdateReceipt.workspace_id == workspace_id,
                TelegramUpdateReceipt.created_at < func.now() - RECEIPT_RETENTION,
            )
        )
        inserted = await self._session.scalar(
            insert(TelegramUpdateReceipt)
            .values(id=uuid4(), workspace_id=workspace_id, update_id=update_id)
            .on_conflict_do_nothing(index_elements=["workspace_id", "update_id"])
            .returning(TelegramUpdateReceipt.id)
        )
        return inserted is not None


class TelegramBindingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(
        self, workspace_id: UUID, telegram_user_id: str
    ) -> TelegramBindingIdentity | None:
        row = (
            await self._session.execute(
                select(UserMembership.id, UserAccount.id)
                .select_from(TelegramBinding)
                .join(
                    UserMembership,
                    (UserMembership.workspace_id == TelegramBinding.workspace_id)
                    & (UserMembership.id == TelegramBinding.membership_id),
                )
                .join(UserAccount, UserAccount.id == UserMembership.user_id)
                .where(
                    TelegramBinding.workspace_id == workspace_id,
                    TelegramBinding.telegram_user_id == telegram_user_id,
                    TelegramBinding.verified_at.is_not(None),
                    TelegramBinding.revoked_at.is_(None),
                    UserMembership.status == "ACTIVE",
                    UserAccount.status == "ACTIVE",
                )
            )
        ).first()
        if row is None:
            return None
        membership_id, user_id = row
        roles = frozenset(
            Role(role)
            for role in (
                await self._session.scalars(
                    select(RoleBinding.role).where(
                        RoleBinding.workspace_id == workspace_id,
                        RoleBinding.membership_id == membership_id,
                    )
                )
            ).all()
        )
        if not roles:
            return None
        return TelegramBindingIdentity(user_id, membership_id, workspace_id, roles)

    async def create_link_request(
        self,
        workspace_id: UUID,
        membership_id: UUID,
        created_by: UUID,
        digest: str,
        expires_at: datetime,
        reason: str,
    ) -> None:
        now = datetime.now(UTC)
        if not await self._membership_is_usable(workspace_id, membership_id, lock=True):
            raise ValueError("Thành viên Telegram không còn hoạt động.")
        await self._session.execute(
            update(TelegramLinkRequest)
            .where(
                TelegramLinkRequest.workspace_id == workspace_id,
                TelegramLinkRequest.membership_id == membership_id,
                TelegramLinkRequest.consumed_at.is_(None),
            )
            .values(expires_at=now)
        )
        await self._session.execute(
            update(TelegramBindingChallenge)
            .where(
                TelegramBindingChallenge.workspace_id == workspace_id,
                TelegramBindingChallenge.membership_id == membership_id,
                TelegramBindingChallenge.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )
        self._session.add(
            TelegramLinkRequest(
                id=uuid4(),
                workspace_id=workspace_id,
                membership_id=membership_id,
                created_by=created_by,
                token_digest=digest,
                expires_at=expires_at,
            )
        )

    async def create_link_challenge(
        self,
        workspace_id: UUID,
        token_digest: str,
        telegram_user_id: str,
    ) -> tuple[str, UUID] | None:
        membership_id = await self._session.scalar(
            select(TelegramLinkRequest.membership_id).where(
                TelegramLinkRequest.workspace_id == workspace_id,
                TelegramLinkRequest.token_digest == token_digest,
            )
        )
        if membership_id is None or not await self._membership_is_usable(
            workspace_id, membership_id, lock=True
        ):
            return None
        request = await self._session.scalar(
            select(TelegramLinkRequest)
            .where(
                TelegramLinkRequest.workspace_id == workspace_id,
                TelegramLinkRequest.membership_id == membership_id,
                TelegramLinkRequest.token_digest == token_digest,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        now = datetime.now(UTC)
        if request is None or request.consumed_at is not None or request.expires_at <= now:
            return None
        request.consumed_at = now
        nonce = secrets.token_urlsafe(32)
        self._session.add(
            TelegramBindingChallenge(
                id=uuid4(),
                workspace_id=workspace_id,
                membership_id=request.membership_id,
                link_request_id=request.id,
                telegram_user_id=telegram_user_id,
                callback_digest=hashlib.sha256(nonce.encode("ascii")).hexdigest(),
                expires_at=now + timedelta(minutes=5),
            )
        )
        return nonce, request.membership_id

    async def confirm_link_challenge(
        self, workspace_id: UUID, nonce_digest: str, telegram_user_id: str
    ) -> tuple[bool, UUID | None]:
        membership_id = await self._session.scalar(
            select(TelegramBindingChallenge.membership_id).where(
                TelegramBindingChallenge.workspace_id == workspace_id,
                TelegramBindingChallenge.callback_digest == nonce_digest,
            )
        )
        if membership_id is None or not await self._membership_is_usable(
            workspace_id, membership_id, lock=True
        ):
            return False, None
        challenge = await self._session.scalar(
            select(TelegramBindingChallenge)
            .where(
                TelegramBindingChallenge.workspace_id == workspace_id,
                TelegramBindingChallenge.membership_id == membership_id,
                TelegramBindingChallenge.callback_digest == nonce_digest,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        now = datetime.now(UTC)
        if (
            challenge is None
            or challenge.consumed_at is not None
            or challenge.expires_at <= now
            or challenge.telegram_user_id != telegram_user_id
        ):
            return False, None
        has_active_user = await self._session.scalar(
            select(TelegramBinding.id).where(
                TelegramBinding.workspace_id == workspace_id,
                TelegramBinding.telegram_user_id == telegram_user_id,
                TelegramBinding.revoked_at.is_(None),
            )
        )
        has_active_membership = await self._session.scalar(
            select(TelegramBinding.id).where(
                TelegramBinding.workspace_id == workspace_id,
                TelegramBinding.membership_id == challenge.membership_id,
                TelegramBinding.revoked_at.is_(None),
            )
        )
        if has_active_user is not None or has_active_membership is not None:
            challenge.consumed_at = now
            return False, challenge.membership_id
        challenge.consumed_at = now
        self._session.add(
            TelegramBinding(
                id=uuid4(),
                workspace_id=workspace_id,
                membership_id=challenge.membership_id,
                telegram_user_id=telegram_user_id,
                verified_at=now,
                revoked_at=None,
            )
        )
        return True, challenge.membership_id

    async def revoke(self, workspace_id: UUID, membership_id: UUID) -> int:
        now = datetime.now(UTC)
        membership = await self._session.scalar(
            select(UserMembership.id)
            .where(
                UserMembership.workspace_id == workspace_id,
                UserMembership.id == membership_id,
            )
            .with_for_update(of=UserMembership)
        )
        if membership is None:
            return 0
        result = await self._session.execute(
            update(TelegramBinding)
            .where(
                TelegramBinding.workspace_id == workspace_id,
                TelegramBinding.membership_id == membership_id,
                TelegramBinding.revoked_at.is_(None),
            )
            .values(revoked_at=now)
            .returning(TelegramBinding.id)
        )
        await self._session.execute(
            update(TelegramLinkRequest)
            .where(
                TelegramLinkRequest.workspace_id == workspace_id,
                TelegramLinkRequest.membership_id == membership_id,
                TelegramLinkRequest.consumed_at.is_(None),
            )
            .values(expires_at=now)
        )
        await self._session.execute(
            update(TelegramBindingChallenge)
            .where(
                TelegramBindingChallenge.workspace_id == workspace_id,
                TelegramBindingChallenge.membership_id == membership_id,
                TelegramBindingChallenge.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )
        return len(result.scalars().all())

    async def list_active(self, workspace_id: UUID) -> list[dict[str, str]]:
        rows = await self._session.execute(
            select(TelegramBinding.id, TelegramBinding.membership_id, TelegramBinding.created_at)
            .where(
                TelegramBinding.workspace_id == workspace_id,
                TelegramBinding.revoked_at.is_(None),
            )
            .order_by(TelegramBinding.created_at.desc())
        )
        return [
            {
                "id": str(binding_id),
                "membership_id": str(membership_id),
                "created_at": created_at.isoformat(),
            }
            for binding_id, membership_id, created_at in rows
        ]

    async def _membership_is_usable(
        self, workspace_id: UUID, membership_id: UUID, *, lock: bool = False
    ) -> bool:
        query = (
            select(UserMembership.id, UserAccount.id)
            .join(UserAccount, UserAccount.id == UserMembership.user_id)
            .where(
                UserMembership.workspace_id == workspace_id,
                UserMembership.id == membership_id,
                UserMembership.status == "ACTIVE",
                UserAccount.status == "ACTIVE",
            )
        )
        if lock:
            query = query.with_for_update(of=(UserMembership, UserAccount), read=True)
        row = (await self._session.execute(query)).first()
        if row is None:
            return False
        role_query = (
            select(RoleBinding.id)
            .where(
                RoleBinding.workspace_id == workspace_id,
                RoleBinding.membership_id == membership_id,
            )
            .limit(1)
        )
        return bool(await self._session.scalar(role_query))
