"""Repository Telegram cho admin-web (MB-07a, ruling 37–38).

- `TelegramUpdateReceiptRepository.claim`: INSERT … ON CONFLICT DO NOTHING vào
  `telegram_update_receipt` (unique `(workspace_id, update_id)`); trả False khi đã nhận.
- `TelegramBindingRepository.resolve`: binding đã xác minh, chưa thu hồi, membership ACTIVE, user
  ACTIVE và có role trong đúng workspace của transaction (RLS `app.workspace_id`). Không có binding
  hợp lệ → None (cùng kết quả cho user lạ, bị khóa, binding bị thu hồi).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    RoleBinding,
    TelegramBinding,
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
