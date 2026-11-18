"""Cấp/thu hồi quyền bot, kiểm tra hết hạn, audit đầy đủ và báo cáo rà soát quyền."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    AuditEvent,
    BotGrant,
    BotInstance,
    UserMembership,
)
from trading_bot.auth.bot_policy import (
    WORKSPACE_BOT_ROLES,
    BotPermission,
    role_allows_bot_permission,
)
from trading_bot.auth.policy import Principal, Role
from trading_bot.domain.events import state_hash

DEFAULT_GRANT_TTL = timedelta(days=90)
DEFAULT_EXPIRING_SOON_WINDOW = timedelta(days=7)


class BotGrantStatus(StrEnum):
    """Trạng thái hiệu lực của một grant theo thời điểm rà soát."""

    ACTIVE = "ACTIVE"
    EXPIRING_SOON = "EXPIRING_SOON"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


@dataclass(frozen=True, slots=True)
class BotGrantReviewItem:
    """Thông tin tóm tắt từng grant cho báo cáo rà soát quyền định kỳ."""

    id: UUID
    workspace_id: UUID
    bot_id: UUID
    membership_id: UUID
    permission: str
    granted_by: UUID
    created_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    reason: str | None
    revoke_reason: str | None
    status: BotGrantStatus
    seconds_until_expiry: int | None


@dataclass(frozen=True, slots=True)
class BotGrantAccessReview:
    """Tổng hợp rà soát quyền bot theo thời điểm `as_of`."""

    workspace_id: UUID
    as_of: datetime
    expiring_within_seconds: int
    active: tuple[BotGrantReviewItem, ...]
    expiring_soon: tuple[BotGrantReviewItem, ...]
    expired: tuple[BotGrantReviewItem, ...]
    revoked: tuple[BotGrantReviewItem, ...]

    @property
    def summary(self) -> dict[str, int]:
        return {
            "active": len(self.active),
            "expiring_soon": len(self.expiring_soon),
            "expired": len(self.expired),
            "revoked": len(self.revoked),
        }


def _now_utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    return normalize_utc(now)


def normalize_utc(value: datetime) -> datetime:
    """Chuẩn hóa timestamp có múi giờ về UTC; từ chối datetime naive."""
    if value.tzinfo is None:
        raise ValueError("Mốc thời gian grant phải có múi giờ (timezone-aware).")
    return value.astimezone(UTC)


def normalize_grant_reason(reason: str | None, *, required: bool = False) -> str | None:
    """Làm sạch và kiểm tra độ dài lý do cấp/thu hồi grant."""
    if reason is None:
        if required:
            raise ValueError("Lý do cấp hoặc thu hồi quyền là bắt buộc.")
        return None
    cleaned = reason.strip()
    if not cleaned:
        raise ValueError("Lý do cấp hoặc thu hồi quyền không được để trống.")
    if len(cleaned) > 500:
        raise ValueError("Lý do cấp hoặc thu hồi quyền tối đa 500 ký tự.")
    return cleaned


def is_grant_active(grant: Any, *, now: datetime | None = None) -> bool:
    """Kiểm tra grant chưa bị thu hồi và chưa vượt mốc `expires_at`."""
    if getattr(grant, "revoked_at", None) is not None:
        return False
    expires_at = getattr(grant, "expires_at", None)
    if expires_at is None:
        return True
    return normalize_utc(expires_at) > _now_utc(now)


def classify_bot_grant(
    grant: BotGrant,
    *,
    as_of: datetime | None = None,
    expiring_within: timedelta = DEFAULT_EXPIRING_SOON_WINDOW,
) -> BotGrantStatus:
    """Phân loại grant đang hoạt động, sắp hết hạn, đã hết hạn hay đã thu hồi."""
    if expiring_within <= timedelta(0):
        raise ValueError("Cửa sổ cảnh báo sắp hết hạn phải lớn hơn 0.")
    now_ts = _now_utc(as_of)
    if grant.revoked_at is not None:
        return BotGrantStatus.REVOKED
    if grant.expires_at is not None:
        expiry_utc = normalize_utc(grant.expires_at)
        if expiry_utc <= now_ts:
            return BotGrantStatus.EXPIRED
        if expiry_utc <= now_ts + expiring_within:
            return BotGrantStatus.EXPIRING_SOON
    return BotGrantStatus.ACTIVE


def build_bot_grant_review(
    *,
    workspace_id: UUID,
    grants: Sequence[BotGrant],
    as_of: datetime | None = None,
    expiring_within: timedelta = DEFAULT_EXPIRING_SOON_WINDOW,
) -> BotGrantAccessReview:
    """Tổng hợp báo cáo rà soát quyền định kỳ từ danh sách BotGrant."""
    now_ts = _now_utc(as_of)
    if expiring_within <= timedelta(0):
        raise ValueError("Cửa sổ cảnh báo sắp hết hạn phải lớn hơn 0.")
    active_items: list[BotGrantReviewItem] = []
    expiring_soon_items: list[BotGrantReviewItem] = []
    expired_items: list[BotGrantReviewItem] = []
    revoked_items: list[BotGrantReviewItem] = []

    for row in grants:
        status = classify_bot_grant(row, as_of=now_ts, expiring_within=expiring_within)
        expiry_utc = normalize_utc(row.expires_at) if row.expires_at is not None else None
        seconds_left = (
            int((expiry_utc - now_ts).total_seconds()) if expiry_utc is not None else None
        )
        item = BotGrantReviewItem(
            id=row.id,
            workspace_id=row.workspace_id,
            bot_id=row.bot_id,
            membership_id=row.membership_id,
            permission=row.permission,
            granted_by=row.granted_by,
            created_at=row.created_at,
            expires_at=expiry_utc,
            revoked_at=normalize_utc(row.revoked_at) if row.revoked_at is not None else None,
            reason=row.reason,
            revoke_reason=row.revoke_reason,
            status=status,
            seconds_until_expiry=seconds_left,
        )
        if status in {BotGrantStatus.ACTIVE, BotGrantStatus.EXPIRING_SOON}:
            active_items.append(item)
            if status is BotGrantStatus.EXPIRING_SOON:
                expiring_soon_items.append(item)
        elif status is BotGrantStatus.EXPIRED:
            expired_items.append(item)
        else:
            revoked_items.append(item)

    expiring_soon_items.sort(key=lambda item: item.expires_at or datetime.max.replace(tzinfo=UTC))
    return BotGrantAccessReview(
        workspace_id=workspace_id,
        as_of=now_ts,
        expiring_within_seconds=int(expiring_within.total_seconds()),
        active=tuple(active_items),
        expiring_soon=tuple(expiring_soon_items),
        expired=tuple(expired_items),
        revoked=tuple(revoked_items),
    )


def _grant_snapshot(row: BotGrant) -> dict[str, Any]:
    return {
        "grant_id": str(row.id),
        "bot_id": str(row.bot_id),
        "membership_id": str(row.membership_id),
        "permission": row.permission,
        "granted_by": str(row.granted_by),
        "expires_at": normalize_utc(row.expires_at).isoformat() if row.expires_at else None,
        "reason": row.reason,
        "revoked_at": normalize_utc(row.revoked_at).isoformat() if row.revoked_at else None,
        "revoke_reason": row.revoke_reason,
    }


def _format_audit_reason(
    *,
    action_label: str,
    bot_id: UUID,
    membership_id: UUID,
    permission: BotPermission,
    expires_at: datetime | None,
    reason: str | None,
) -> str:
    expiry_text = normalize_utc(expires_at).isoformat() if expires_at is not None else "vô thời hạn"
    reason_text = reason or "(không ghi chú)"
    return (
        f"{action_label} [{permission.value}] bot={bot_id} member={membership_id} | "
        f"expires_at={expiry_text} | lý do: {reason_text}"
    )


class BotGrantRepository:
    """Application authorization bổ sung cho row-scope RLS."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def require(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        permission: BotPermission,
        now: datetime | None = None,
    ) -> None:
        permission = BotPermission(permission)
        if not role_allows_bot_permission(principal.roles, permission):
            raise PermissionError("Role không cho phép thao tác bot này.")
        # Chỉ bỏ qua grant khi chính role workspace-wide cho phép thao tác này;
        # quyền của role theo bot (operator) không được mượn phạm vi workspace.
        if role_allows_bot_permission(principal.roles & WORKSPACE_BOT_ROLES, permission):
            return
        current_time = _now_utc(now)
        grant = await self._active(
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            membership_id=principal.membership_id,
            permission=permission,
            now=current_time,
        )
        if grant is None or not is_grant_active(grant, now=current_time):
            raise PermissionError("Thiếu grant đang hiệu lực cho bot và thao tác này.")

    async def grant(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        membership_id: UUID,
        permission: BotPermission,
        expires_at: datetime | None = None,
        reason: str | None = None,
        request_id: UUID | None = None,
        now: datetime | None = None,
    ) -> BotGrant:
        """Chỉ owner cấp quyền; kiểm hạn tương lai, đóng bản ghi cũ hết hạn và ghi audit."""
        self._require_owner(principal)
        permission = BotPermission(permission)
        current_time = _now_utc(now)
        normalized_expires = normalize_utc(expires_at) if expires_at is not None else None
        if normalized_expires is not None and normalized_expires <= current_time:
            raise ValueError("Thời điểm hết hạn grant phải lớn hơn thời điểm hiện tại.")
        clean_reason = normalize_grant_reason(reason, required=False)

        bot = await self._session.scalar(
            select(BotInstance).where(
                BotInstance.workspace_id == principal.workspace_id, BotInstance.id == bot_id
            )
        )
        member = await self._session.scalar(
            select(UserMembership).where(
                UserMembership.workspace_id == principal.workspace_id,
                UserMembership.id == membership_id,
                UserMembership.status == "ACTIVE",
            )
        )
        if bot is None or member is None:
            raise PermissionError("Bot hoặc thành viên không thuộc workspace đang hoạt động.")
        # Khóa membership để tuần tự hóa grant/revoke cho cùng người nhận.
        await self._session.scalar(
            select(UserMembership)
            .where(
                UserMembership.workspace_id == principal.workspace_id,
                UserMembership.id == membership_id,
            )
            .with_for_update()
        )
        active_request_id = request_id or uuid4()
        existing = await self._unrevoked(
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            membership_id=membership_id,
            permission=permission,
            lock=True,
        )
        if existing is not None:
            existing_expiry = (
                normalize_utc(existing.expires_at) if existing.expires_at is not None else None
            )
            if is_grant_active(existing, now=current_time):
                if normalized_expires == existing_expiry:
                    return existing
                self._revoke_row_with_audit(
                    principal=principal,
                    row=existing,
                    revoked_at=current_time,
                    reason=clean_reason or "Cập nhật thời hạn cấp quyền mới.",
                    request_id=active_request_id,
                )
            else:
                # Bản ghi cũ đã hết hạn nhưng chưa set revoked_at: đóng lại để giữ chỉ mục
                # `uq_bot_grant_active` duy nhất khi cấp lại quyền mới.
                self._revoke_row_with_audit(
                    principal=principal,
                    row=existing,
                    revoked_at=current_time,
                    reason="Tự động đóng bản ghi đã hết hạn khi cấp lại quyền.",
                    request_id=active_request_id,
                )
            await self._session.flush()

        row = BotGrant(
            id=uuid4(),
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            membership_id=membership_id,
            permission=permission.value,
            granted_by=principal.membership_id,
            expires_at=normalized_expires,
            reason=clean_reason,
        )
        self._session.add(row)
        self._session.add(
            AuditEvent(
                id=uuid4(),
                workspace_id=principal.workspace_id,
                actor_membership_id=principal.membership_id,
                action="bot.grant.granted",
                target_type="bot_grant",
                target_id=row.id,
                reason=_format_audit_reason(
                    action_label="Cấp quyền",
                    bot_id=bot_id,
                    membership_id=membership_id,
                    permission=permission,
                    expires_at=normalized_expires,
                    reason=clean_reason,
                ),
                before_hash=None,
                after_hash=state_hash(_grant_snapshot(row)),
                request_id=active_request_id,
                occurred_at=current_time,
            )
        )
        await self._session.flush()
        return row

    async def revoke(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        membership_id: UUID,
        permission: BotPermission,
        reason: str | None = None,
        request_id: UUID | None = None,
        now: datetime | None = None,
    ) -> bool:
        self._require_owner(principal)
        current_time = _now_utc(now)
        clean_reason = normalize_grant_reason(reason, required=False)
        parsed_permission = BotPermission(permission)
        row = await self._active(
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            membership_id=membership_id,
            permission=parsed_permission,
            lock=True,
            now=current_time,
        )
        if row is None or not is_grant_active(row, now=current_time):
            return False
        self._revoke_row_with_audit(
            principal=principal,
            row=row,
            revoked_at=current_time,
            reason=clean_reason,
            request_id=request_id or uuid4(),
        )
        await self._session.flush()
        return True

    async def list_grants(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID | None = None,
        membership_id: UUID | None = None,
    ) -> Sequence[BotGrant]:
        """Liệt kê toàn bộ lịch sử grant trong phạm vi workspace (tùy chọn lọc bot/member)."""
        query = select(BotGrant).where(BotGrant.workspace_id == workspace_id)
        if bot_id is not None:
            query = query.where(BotGrant.bot_id == bot_id)
        if membership_id is not None:
            query = query.where(BotGrant.membership_id == membership_id)
        query = query.order_by(BotGrant.created_at.desc(), BotGrant.id.desc())
        result = await self._session.scalars(query)
        return tuple(result.all())

    async def review_grants(
        self,
        *,
        workspace_id: UUID,
        as_of: datetime | None = None,
        expiring_within: timedelta = DEFAULT_EXPIRING_SOON_WINDOW,
        bot_id: UUID | None = None,
        membership_id: UUID | None = None,
    ) -> BotGrantAccessReview:
        """Lập báo cáo rà soát quyền định kỳ cho workspace."""
        rows = await self.list_grants(
            workspace_id=workspace_id, bot_id=bot_id, membership_id=membership_id
        )
        return build_bot_grant_review(
            workspace_id=workspace_id,
            grants=rows,
            as_of=as_of,
            expiring_within=expiring_within,
        )

    def _revoke_row_with_audit(
        self,
        *,
        principal: Principal,
        row: BotGrant,
        revoked_at: datetime,
        reason: str | None,
        request_id: UUID,
    ) -> None:
        before_state = _grant_snapshot(row)
        row.revoked_at = revoked_at
        row.revoke_reason = reason
        after_state = _grant_snapshot(row)
        self._session.add(
            AuditEvent(
                id=uuid4(),
                workspace_id=principal.workspace_id,
                actor_membership_id=principal.membership_id,
                action="bot.grant.revoked",
                target_type="bot_grant",
                target_id=row.id,
                reason=_format_audit_reason(
                    action_label="Thu hồi quyền",
                    bot_id=row.bot_id,
                    membership_id=row.membership_id,
                    permission=BotPermission(row.permission),
                    expires_at=row.expires_at,
                    reason=reason,
                ),
                before_hash=state_hash(before_state),
                after_hash=state_hash(after_state),
                request_id=request_id,
                occurred_at=revoked_at,
            )
        )

    async def _unrevoked(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        membership_id: UUID,
        permission: BotPermission,
        lock: bool = False,
    ) -> BotGrant | None:
        query = select(BotGrant).where(
            BotGrant.workspace_id == workspace_id,
            BotGrant.bot_id == bot_id,
            BotGrant.membership_id == membership_id,
            BotGrant.permission == permission.value,
            BotGrant.revoked_at.is_(None),
        )
        return await self._session.scalar(query.with_for_update() if lock else query)

    async def _active(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        membership_id: UUID,
        permission: BotPermission,
        lock: bool = False,
        now: datetime | None = None,
    ) -> BotGrant | None:
        now_ts = _now_utc(now)
        query = select(BotGrant).where(
            BotGrant.workspace_id == workspace_id,
            BotGrant.bot_id == bot_id,
            BotGrant.membership_id == membership_id,
            BotGrant.permission == permission.value,
            BotGrant.revoked_at.is_(None),
            or_(BotGrant.expires_at.is_(None), BotGrant.expires_at > now_ts),
        )
        return await self._session.scalar(query.with_for_update() if lock else query)

    @staticmethod
    def _require_owner(principal: Principal) -> None:
        if Role.OWNER not in principal.roles:
            raise PermissionError("Chỉ owner được cấp hoặc thu hồi grant bot.")
