"""Dịch vụ quản trị grant theo bot: cấp/thu hồi có thời hạn, lý do, audit và rà soát định kỳ."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import BotGrant
from trading_bot.adapters.database.repositories.bot_grants import (
    DEFAULT_GRANT_TTL,
    BotGrantAccessReview,
    BotGrantRepository,
    BotGrantReviewItem,
    normalize_grant_reason,
    normalize_utc,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import RECENT_MFA_LIFETIME, Permission, Principal, Role, authorize


class BotGrantAdminError(Exception):
    """Lỗi nghiệp vụ quản trị grant với mã HTTP ổn định."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def _require_recent_mfa(principal: Principal, now: datetime) -> None:
    if principal.mfa_verified_at > now:
        raise BotGrantAdminError(403, "Mốc xác thực MFA của phiên không hợp lệ.")
    if now - principal.mfa_verified_at > RECENT_MFA_LIFETIME:
        raise BotGrantAdminError(403, "Cần xác thực MFA gần đây trước khi thay đổi quyền bot.")


def _serialize_grant(row: BotGrant) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id),
        "bot_id": str(row.bot_id),
        "membership_id": str(row.membership_id),
        "permission": row.permission,
        "granted_by": str(row.granted_by),
        "created_at": normalize_utc(row.created_at).isoformat() if row.created_at else None,
        "expires_at": normalize_utc(row.expires_at).isoformat() if row.expires_at else None,
        "reason": row.reason,
        "revoked_at": normalize_utc(row.revoked_at).isoformat() if row.revoked_at else None,
        "revoke_reason": row.revoke_reason,
    }


def _serialize_review_item(item: BotGrantReviewItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "bot_id": str(item.bot_id),
        "membership_id": str(item.membership_id),
        "permission": item.permission,
        "granted_by": str(item.granted_by),
        "created_at": normalize_utc(item.created_at).isoformat() if item.created_at else None,
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "revoked_at": item.revoked_at.isoformat() if item.revoked_at else None,
        "reason": item.reason,
        "revoke_reason": item.revoke_reason,
        "status": item.status.value,
        "seconds_until_expiry": item.seconds_until_expiry,
    }


def _serialize_review(
    review: BotGrantAccessReview, *, expiring_within_days: int, include_revoked: bool
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "workspace_id": str(review.workspace_id),
        "as_of": review.as_of.isoformat(),
        "expiring_within_days": expiring_within_days,
        "summary": review.summary,
        "active": [_serialize_review_item(item) for item in review.active],
        "expiring_soon": [_serialize_review_item(item) for item in review.expiring_soon],
        "expired": [_serialize_review_item(item) for item in review.expired],
    }
    if include_revoked:
        payload["revoked"] = [_serialize_review_item(item) for item in review.revoked]
    return payload


class BotGrantAdminService:
    """Quản lý vòng đời BotGrant qua UnitOfWork và ghi AuditEvent đầy đủ."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def grant(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        membership_id: UUID,
        permission: BotPermission,
        reason: str,
        expires_at: datetime | None = None,
        permanent: bool = False,
        request_id: UUID | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Cấp quyền theo bot; mặc định có hạn 90 ngày trừ khi chọn permanent=True."""
        if Role.OWNER not in principal.roles:
            raise BotGrantAdminError(403, "Chỉ owner được cấp quyền theo bot.")
        current_time = normalize_utc(now) if now is not None else datetime.now(UTC)
        _require_recent_mfa(principal, current_time)
        if permanent and expires_at is not None:
            raise BotGrantAdminError(422, "Không thể vừa chọn không thời hạn vừa đặt expires_at.")
        try:
            clean_reason = normalize_grant_reason(reason, required=True)
            effective_expiry: datetime | None
            if permanent:
                effective_expiry = None
            elif expires_at is not None:
                effective_expiry = normalize_utc(expires_at)
            else:
                effective_expiry = current_time + DEFAULT_GRANT_TTL
        except ValueError as error:
            raise BotGrantAdminError(422, str(error)) from error

        async with UnitOfWork(
            self._sessions,
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
            request_id=request_id,
        ) as unit:
            repository = BotGrantRepository(unit.session)
            try:
                row = await repository.grant(
                    principal,
                    bot_id=bot_id,
                    membership_id=membership_id,
                    permission=permission,
                    expires_at=effective_expiry,
                    reason=clean_reason,
                    request_id=unit.request_id,
                    now=current_time,
                )
            except PermissionError as error:
                message = str(error)
                status_code = 403 if "owner" in message.lower() else 404
                raise BotGrantAdminError(status_code, message) from error
            except ValueError as error:
                raise BotGrantAdminError(422, str(error)) from error
            return _serialize_grant(row)

    async def revoke(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        membership_id: UUID,
        permission: BotPermission,
        reason: str,
        request_id: UUID | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Thu hồi grant đang hoạt động kèm lý do và audit event."""
        if Role.OWNER not in principal.roles:
            raise BotGrantAdminError(403, "Chỉ owner được thu hồi quyền theo bot.")
        current_time = normalize_utc(now) if now is not None else datetime.now(UTC)
        _require_recent_mfa(principal, current_time)
        try:
            clean_reason = normalize_grant_reason(reason, required=True)
            parsed_permission = BotPermission(permission)
        except ValueError as error:
            raise BotGrantAdminError(422, str(error)) from error

        async with UnitOfWork(
            self._sessions,
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
            request_id=request_id,
        ) as unit:
            repository = BotGrantRepository(unit.session)
            revoked = await repository.revoke(
                principal,
                bot_id=bot_id,
                membership_id=membership_id,
                permission=parsed_permission,
                reason=clean_reason,
                request_id=unit.request_id,
                now=current_time,
            )
            return {
                "revoked": revoked,
                "bot_id": str(bot_id),
                "membership_id": str(membership_id),
                "permission": parsed_permission.value,
            }

    async def review_grants(
        self,
        principal: Principal,
        *,
        bot_id: UUID | None = None,
        membership_id: UUID | None = None,
        expiring_within_days: int = 7,
        include_revoked: bool = False,
        record_audit: bool = False,
        reason: str | None = None,
        request_id: UUID | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Tạo báo cáo rà soát quyền định kỳ (đang hoạt động, sắp hết hạn, đã hết hạn)."""
        if not authorize(principal, Permission.MEMBER_WRITE):
            raise BotGrantAdminError(
                403, "Chỉ owner được rà soát toàn bộ quyền bot trong workspace."
            )
        if expiring_within_days < 1 or expiring_within_days > 365:
            raise BotGrantAdminError(422, "expiring_within_days phải nằm trong khoảng 1–365 ngày.")
        try:
            clean_reason = normalize_grant_reason(reason, required=False)
        except ValueError as error:
            raise BotGrantAdminError(422, str(error)) from error
        current_time = normalize_utc(now) if now is not None else datetime.now(UTC)

        async with UnitOfWork(
            self._sessions,
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
            request_id=request_id,
            snapshot_read_only=not record_audit,
        ) as unit:
            review = await BotGrantRepository(unit.session).review_grants(
                workspace_id=principal.workspace_id,
                as_of=current_time,
                expiring_within=timedelta(days=expiring_within_days),
                bot_id=bot_id,
                membership_id=membership_id,
            )
            if record_audit:
                audit_reason = clean_reason or (
                    f"Rà soát quyền bot định kỳ (cửa sổ {expiring_within_days} ngày)."
                )
                unit.record_audit(
                    action="bot.grant.access_reviewed",
                    target_type="workspace",
                    target_id=principal.workspace_id,
                    reason=audit_reason,
                    after_state=review.summary,
                )
            return _serialize_review(
                review,
                expiring_within_days=expiring_within_days,
                include_revoked=include_revoked,
            )
