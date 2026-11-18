"""HTTP API quản lý thành viên, lời mời và khôi phục MFA theo workspace (US-04 / WP-04A)."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import (
    AuditEvent,
    MfaFactor,
    RoleBinding,
    UserAccount,
    UserInvitation,
    UserMembership,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import RECENT_MFA_LIFETIME, Permission, Principal, Role, authorize
from trading_bot.auth.route_policy import CsrfMode, permission_route
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity
from trading_bot.domain.events import EventEnvelope

router = APIRouter(prefix="/api/v1/members", tags=["quản lý thành viên"])


class UpdateMemberRoleRequest(BaseModel):
    """Yêu cầu cập nhật vai trò cho thành viên workspace."""

    role: Role = Field(description="Vai trò mới trong workspace.")
    reason: str = Field(min_length=3, max_length=500, description="Lý do thay đổi vai trò.")


class CreateInvitationRequest(BaseModel):
    """Yêu cầu tạo lời mời tham gia workspace."""

    email: str = Field(min_length=3, max_length=320, description="Email người được mời.")
    role: Role = Field(description="Vai trò gán khi người được mời chấp nhận.")
    expires_hours: int = Field(default=72, ge=1, le=720, description="Thời hạn lời mời (giờ).")
    reason: str = Field(min_length=3, max_length=500, description="Lý do mời thành viên.")


class RevokeInvitationRequest(BaseModel):
    """Yêu cầu thu hồi lời mời đang chờ."""

    reason: str = Field(min_length=3, max_length=500, description="Lý do thu hồi lời mời.")


class MfaRecoveryRequest(BaseModel):
    """Yêu cầu khôi phục / đặt lại MFA cho thành viên khi mất thiết bị."""

    membership_id: UUID = Field(description="Mã thành viên cần đặt lại MFA.")
    reason: str = Field(min_length=5, max_length=500, description="Lý do khôi phục MFA.")


def _get_sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    sessions = cast(
        async_sessionmaker[AsyncSession] | None,
        getattr(request.app.state, "session_factory", None),
    )
    if sessions is None:
        raise HTTPException(status_code=503, detail="Cơ sở dữ liệu thành viên chưa sẵn sàng")
    return sessions


def _require_permission(identity: AuthIdentity, permission: Permission) -> None:
    if not authorize(identity.principal, permission):
        raise HTTPException(status_code=403, detail="Không đủ quyền thực hiện thao tác thành viên")


def _require_recent_mfa(principal: Principal) -> None:
    if datetime.now(UTC) - principal.mfa_verified_at > RECENT_MFA_LIFETIME:
        raise HTTPException(
            status_code=403, detail="Thao tác nhạy cảm yêu cầu xác thực MFA gần đây"
        )


def _request_id(request: Request) -> UUID:
    header = request.headers.get("X-Request-ID")
    if header:
        try:
            return UUID(header)
        except ValueError:
            pass
    return uuid4()


@router.get(
    "",
    summary="Danh sách thành viên workspace",
    description="Liệt kê toàn bộ thành viên và vai trò hiện tại trong workspace.",
    dependencies=[permission_route(Permission.WORKSPACE_READ)],
)
async def list_members(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, object]:
    _require_permission(identity, Permission.WORKSPACE_READ)
    response.headers["Cache-Control"] = "no-store"
    sessions = _get_sessions(request)
    async with UnitOfWork(sessions, workspace_id=identity.principal.workspace_id) as unit:
        rows = (
            await unit.session.execute(
                select(UserMembership, UserAccount)
                .join(UserAccount, UserAccount.id == UserMembership.user_id)
                .where(UserMembership.workspace_id == identity.principal.workspace_id)
                .order_by(UserMembership.created_at.asc())
            )
        ).all()
        role_rows = (
            await unit.session.scalars(
                select(RoleBinding).where(
                    RoleBinding.workspace_id == identity.principal.workspace_id
                )
            )
        ).all()
        roles_by_member: dict[UUID, list[str]] = {}
        for binding in role_rows:
            roles_by_member.setdefault(binding.membership_id, []).append(binding.role)
        members: list[dict[str, Any]] = [
            {
                "membership_id": str(membership.id),
                "user_id": str(account.id),
                "email": account.email_normalized,
                "status": membership.status,
                "roles": sorted(roles_by_member.get(membership.id, [])),
                "created_at": membership.created_at.isoformat(),
            }
            for membership, account in rows
        ]
    return {"members": members}


@router.patch(
    "/{membership_id}/role",
    summary="Cập nhật vai trò thành viên",
    description=(
        "Thay đổi vai trò của một thành viên trong workspace; "
        "không cho phép hạ quyền owner cuối cùng."
    ),
    dependencies=[permission_route(Permission.MEMBER_WRITE, csrf=CsrfMode.HEADER)],
)
async def update_member_role(
    membership_id: UUID,
    body: UpdateMemberRoleRequest,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    request: Request,
    response: Response,
) -> dict[str, object]:
    _require_permission(identity, Permission.MEMBER_WRITE)
    _require_recent_mfa(identity.principal)
    response.headers["Cache-Control"] = "no-store"
    sessions = _get_sessions(request)
    req_id = _request_id(request)
    async with UnitOfWork(
        sessions, workspace_id=identity.principal.workspace_id, request_id=req_id
    ) as unit:
        target = await unit.session.scalar(
            select(UserMembership).where(
                UserMembership.workspace_id == identity.principal.workspace_id,
                UserMembership.id == membership_id,
            )
        )
        if target is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy thành viên trong workspace")
        existing_bindings = (
            await unit.session.scalars(
                select(RoleBinding).where(
                    RoleBinding.workspace_id == identity.principal.workspace_id,
                    RoleBinding.membership_id == membership_id,
                )
            )
        ).all()
        old_roles = sorted(item.role for item in existing_bindings)
        if Role.OWNER.value in old_roles and body.role is not Role.OWNER:
            owner_count = len(
                (
                    await unit.session.scalars(
                        select(RoleBinding).where(
                            RoleBinding.workspace_id == identity.principal.workspace_id,
                            RoleBinding.role == Role.OWNER.value,
                        )
                    )
                ).all()
            )
            if owner_count <= 1:
                raise HTTPException(
                    status_code=409,
                    detail="Không thể hạ quyền owner cuối cùng của workspace.",
                )
        for binding in existing_bindings:
            await unit.session.delete(binding)
        unit.session.add(
            RoleBinding(
                workspace_id=identity.principal.workspace_id,
                membership_id=membership_id,
                role=body.role.value,
            )
        )
        now = datetime.now(UTC)
        unit.session.add(
            AuditEvent(
                workspace_id=identity.principal.workspace_id,
                actor_membership_id=identity.principal.membership_id,
                action="member.role_updated",
                target_type="user_membership",
                target_id=membership_id,
                reason=body.reason.strip(),
                before_hash=",".join(old_roles) or None,
                after_hash=body.role.value,
                request_id=req_id,
                occurred_at=now,
            )
        )
        unit.enqueue(
            EventEnvelope(
                event_type="member.role_updated",
                aggregate_type="user_membership",
                aggregate_id=membership_id,
                workspace_id=identity.principal.workspace_id,
                occurred_at=now,
                correlation_id=req_id,
                idempotency_key=f"member.role_updated:{membership_id}:{req_id}",
                schema_version=1,
                payload={"roles": [body.role.value]},
            )
        )
    return {"membership_id": str(membership_id), "role": body.role.value}


@router.get(
    "/invitations",
    summary="Danh sách lời mời thành viên",
    description="Liệt kê các lời mời tham gia workspace (không trả về token bí mật).",
    dependencies=[permission_route(Permission.MEMBER_WRITE)],
)
async def list_invitations(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, object]:
    _require_permission(identity, Permission.MEMBER_WRITE)
    response.headers["Cache-Control"] = "no-store"
    sessions = _get_sessions(request)
    async with UnitOfWork(sessions, workspace_id=identity.principal.workspace_id) as unit:
        records = (
            await unit.session.scalars(
                select(UserInvitation)
                .where(UserInvitation.workspace_id == identity.principal.workspace_id)
                .order_by(UserInvitation.created_at.desc())
            )
        ).all()
        invitations = [
            {
                "id": str(item.id),
                "email": item.email,
                "role": item.role,
                "status": item.status,
                "expires_at": item.expires_at.isoformat(),
                "created_at": item.created_at.isoformat(),
            }
            for item in records
        ]
    return {"invitations": invitations}


@router.post(
    "/invitations",
    status_code=201,
    summary="Tạo lời mời thành viên mới",
    description="Tạo lời mời có thời hạn và chỉ lưu hash token; token thô trả về đúng một lần.",
    dependencies=[permission_route(Permission.MEMBER_WRITE, csrf=CsrfMode.HEADER)],
)
async def create_invitation(
    body: CreateInvitationRequest,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    request: Request,
    response: Response,
) -> dict[str, object]:
    _require_permission(identity, Permission.MEMBER_WRITE)
    _require_recent_mfa(identity.principal)
    response.headers["Cache-Control"] = "no-store"
    sessions = _get_sessions(request)
    req_id = _request_id(request)
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    now = datetime.now(UTC)
    expires_at = now + timedelta(hours=body.expires_hours)
    invitation = UserInvitation(
        workspace_id=identity.principal.workspace_id,
        email=body.email.strip().lower(),
        role=body.role.value,
        token_hash=token_hash,
        invited_by=identity.principal.membership_id,
        status="PENDING",
        expires_at=expires_at,
    )
    async with UnitOfWork(
        sessions, workspace_id=identity.principal.workspace_id, request_id=req_id
    ) as unit:
        unit.session.add(invitation)
        await unit.session.flush()
        unit.session.add(
            AuditEvent(
                workspace_id=identity.principal.workspace_id,
                actor_membership_id=identity.principal.membership_id,
                action="member.invitation_created",
                target_type="user_invitation",
                target_id=invitation.id,
                reason=body.reason.strip(),
                after_hash=token_hash[:16],
                request_id=req_id,
                occurred_at=now,
            )
        )
    return {
        "invitation": {
            "id": str(invitation.id),
            "email": invitation.email,
            "role": invitation.role,
            "status": invitation.status,
            "expires_at": invitation.expires_at.isoformat(),
            "invite_token": raw_token,
        }
    }


@router.post(
    "/invitations/{invitation_id}/revoke",
    summary="Thu hồi lời mời thành viên",
    description="Thu hồi lời mời đang ở trạng thái PENDING và ghi nhật ký audit.",
    dependencies=[permission_route(Permission.MEMBER_WRITE, csrf=CsrfMode.HEADER)],
)
async def revoke_invitation(
    invitation_id: UUID,
    body: RevokeInvitationRequest,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    request: Request,
    response: Response,
) -> dict[str, object]:
    _require_permission(identity, Permission.MEMBER_WRITE)
    response.headers["Cache-Control"] = "no-store"
    sessions = _get_sessions(request)
    req_id = _request_id(request)
    async with UnitOfWork(
        sessions, workspace_id=identity.principal.workspace_id, request_id=req_id
    ) as unit:
        invitation = await unit.session.scalar(
            select(UserInvitation).where(
                UserInvitation.workspace_id == identity.principal.workspace_id,
                UserInvitation.id == invitation_id,
            )
        )
        if invitation is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy lời mời")
        if invitation.status != "PENDING":
            raise HTTPException(status_code=409, detail="Chỉ có thể thu hồi lời mời đang PENDING")
        invitation.status = "REVOKED"
        unit.session.add(
            AuditEvent(
                workspace_id=identity.principal.workspace_id,
                actor_membership_id=identity.principal.membership_id,
                action="member.invitation_revoked",
                target_type="user_invitation",
                target_id=invitation.id,
                reason=body.reason.strip(),
                request_id=req_id,
                occurred_at=datetime.now(UTC),
            )
        )
    return {"id": str(invitation_id), "status": "REVOKED"}


@router.post(
    "/recovery/mfa-reset",
    summary="Khôi phục MFA cho thành viên",
    description=(
        "Vô hiệu hóa yếu tố MFA cũ của thành viên khi mất thiết bị; yêu cầu MFA gần đây và lý do."
    ),
    dependencies=[permission_route(Permission.MEMBER_WRITE, csrf=CsrfMode.HEADER)],
)
async def reset_member_mfa(
    body: MfaRecoveryRequest,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    request: Request,
    response: Response,
) -> dict[str, object]:
    _require_permission(identity, Permission.MEMBER_WRITE)
    _require_recent_mfa(identity.principal)
    response.headers["Cache-Control"] = "no-store"
    sessions = _get_sessions(request)
    req_id = _request_id(request)
    now = datetime.now(UTC)
    async with UnitOfWork(
        sessions, workspace_id=identity.principal.workspace_id, request_id=req_id
    ) as unit:
        target = await unit.session.scalar(
            select(UserMembership).where(
                UserMembership.workspace_id == identity.principal.workspace_id,
                UserMembership.id == body.membership_id,
            )
        )
        if target is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy thành viên trong workspace")
        factors = (
            await unit.session.scalars(
                select(MfaFactor).where(
                    MfaFactor.workspace_id == identity.principal.workspace_id,
                    MfaFactor.membership_id == body.membership_id,
                )
            )
        ).all()
        for factor in factors:
            await unit.session.delete(factor)
        unit.session.add(
            AuditEvent(
                workspace_id=identity.principal.workspace_id,
                actor_membership_id=identity.principal.membership_id,
                action="member.mfa_recovery_reset",
                target_type="user_membership",
                target_id=body.membership_id,
                reason=body.reason.strip(),
                request_id=req_id,
                occurred_at=now,
            )
        )
    return {
        "membership_id": str(body.membership_id),
        "disabled_factors": len(factors),
        "status": "RESET",
    }
