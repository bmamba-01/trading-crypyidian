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
    Session,
    UserAccount,
    UserInvitation,
    UserMembership,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.cookies import clear_session_cookie, set_session_cookie
from trading_bot.auth.policy import RECENT_MFA_LIFETIME, Permission, Principal, Role, authorize
from trading_bot.auth.route_policy import CsrfMode, permission_route
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.schemas import MemberSessionsRevokeAllRequest
from trading_bot.auth.service import AuthIdentity
from trading_bot.auth.session_lifecycle import (
    issue_rotated_session,
    revoke_membership_sessions,
)
from trading_bot.domain.events import EventEnvelope, state_hash

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


def _current_now(request: Request) -> datetime:
    auth_service = getattr(request.app.state, "auth_service", None)
    now_fn = getattr(auth_service, "now", None)
    if callable(now_fn):
        current = now_fn()
        if isinstance(current, datetime):
            return current
    return datetime.now(UTC)


def _require_recent_mfa(principal: Principal, *, now: datetime | None = None) -> None:
    current = now if now is not None else datetime.now(UTC)
    if (
        current < principal.mfa_verified_at
        or current - principal.mfa_verified_at > RECENT_MFA_LIFETIME
    ):
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
    now = _current_now(request)
    _require_recent_mfa(identity.principal, now=now)
    response.headers["Cache-Control"] = "no-store"
    sessions = _get_sessions(request)
    req_id = _request_id(request)
    rotated_cookie_value: str | None = None
    rotated_max_age: int | None = None
    revoked_count = 0
    async with UnitOfWork(
        sessions,
        workspace_id=identity.principal.workspace_id,
        actor_membership_id=identity.principal.membership_id,
        request_id=req_id,
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
        # SEC-10 + SEC-11: khi đổi vai trò thành viên, thu hồi toàn bộ phiên cũ; nếu tự đổi
        # vai trò của chính mình thì xoay sang phiên/token mới và đặt lại cookie trên response.
        if (
            membership_id == identity.principal.membership_id
            and identity.principal.session_id is not None
        ):
            current_session = await unit.session.scalar(
                select(Session).where(
                    Session.workspace_id == identity.principal.workspace_id,
                    Session.id == identity.principal.session_id,
                    Session.revoked_at.is_(None),
                )
            )
            if current_session is not None:
                new_session, new_raw_token, _ = issue_rotated_session(
                    unit.session,
                    current_session,
                    now=now,
                    preserve_csrf=True,
                )
                other_revoked = await revoke_membership_sessions(
                    unit.session,
                    workspace_id=identity.principal.workspace_id,
                    membership_id=membership_id,
                    now=now,
                    exclude_session_id=new_session.id,
                )
                revoked_count = len(other_revoked) + 1
                rotated_cookie_value = f"{identity.principal.workspace_id}:{new_raw_token}"
                rotated_max_age = max(1, int((new_session.expires_at - now).total_seconds()))
            else:
                revoked_ids = await revoke_membership_sessions(
                    unit.session,
                    workspace_id=identity.principal.workspace_id,
                    membership_id=membership_id,
                    now=now,
                )
                revoked_count = len(revoked_ids)
        else:
            revoked_ids = await revoke_membership_sessions(
                unit.session,
                workspace_id=identity.principal.workspace_id,
                membership_id=membership_id,
                now=now,
            )
            revoked_count = len(revoked_ids)

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
                payload={"roles": [body.role.value], "revoked_sessions": revoked_count},
            )
        )
    if rotated_cookie_value is not None and rotated_max_age is not None:
        set_session_cookie(
            response,
            request.app.state.settings,
            rotated_cookie_value,
            max_age=rotated_max_age,
        )
    return {
        "membership_id": str(membership_id),
        "role": body.role.value,
        "revoked_sessions": revoked_count,
    }


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
    "/{membership_id}/sessions/revoke-all",
    summary="Thu hồi toàn bộ phiên của thành viên",
    description="Owner thu hồi mọi phiên đang hoạt động của một thành viên trong cùng workspace.",
    dependencies=[permission_route(Permission.MEMBER_WRITE, csrf=CsrfMode.HEADER)],
)
async def revoke_all_member_sessions(
    membership_id: UUID,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    request: Request,
    response: Response,
    body: MemberSessionsRevokeAllRequest | None = None,
) -> dict[str, object]:
    _require_permission(identity, Permission.MEMBER_WRITE)
    response.headers["Cache-Control"] = "no-store"
    sessions = _get_sessions(request)
    req_id = _request_id(request)
    now = _current_now(request)
    clean_reason = (
        body.reason.strip()
        if body is not None and body.reason is not None and body.reason.strip()
        else "Owner thu hồi toàn bộ phiên đăng nhập của thành viên"
    )
    async with UnitOfWork(
        sessions,
        workspace_id=identity.principal.workspace_id,
        actor_membership_id=identity.principal.membership_id,
        request_id=req_id,
    ) as unit:
        target = await unit.session.scalar(
            select(UserMembership).where(
                UserMembership.workspace_id == identity.principal.workspace_id,
                UserMembership.id == membership_id,
            )
        )
        if target is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy thành viên trong workspace")
        revoked_ids = await revoke_membership_sessions(
            unit.session,
            workspace_id=identity.principal.workspace_id,
            membership_id=membership_id,
            now=now,
        )
        unit.session.add(
            AuditEvent(
                workspace_id=identity.principal.workspace_id,
                actor_membership_id=identity.principal.membership_id,
                action="member.sessions_revoked_all",
                target_type="user_membership",
                target_id=membership_id,
                reason=clean_reason,
                after_hash=state_hash(
                    {
                        "membership_id": str(membership_id),
                        "revoked_count": len(revoked_ids),
                    }
                ),
                request_id=req_id,
                occurred_at=now,
            )
        )
    if identity.principal.session_id in revoked_ids:
        clear_session_cookie(response, request.app.state.settings)
    return {
        "membership_id": str(membership_id),
        "revoked_count": len(revoked_ids),
        "status": "REVOKED",
    }


@router.post(
    "/recovery/mfa-reset",
    summary="Khôi phục MFA cho thành viên",
    description=(
        "Vô hiệu hóa yếu tố MFA cũ và thu hồi toàn bộ phiên đang hoạt động của thành viên "
        "khi mất thiết bị; yêu cầu MFA gần đây và lý do."
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
    now = _current_now(request)
    _require_recent_mfa(identity.principal, now=now)
    response.headers["Cache-Control"] = "no-store"
    sessions = _get_sessions(request)
    req_id = _request_id(request)
    async with UnitOfWork(
        sessions,
        workspace_id=identity.principal.workspace_id,
        actor_membership_id=identity.principal.membership_id,
        request_id=req_id,
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
                    MfaFactor.verified_at.is_not(None),
                )
            )
        ).all()
        # Tuân thủ đặc quyền tối thiểu DB (SEC-01): crypidian_admin_web không có DELETE trên
        # mfa_factor, chỉ cập nhật verified_at=NULL để vô hiệu hóa yếu tố MFA cũ.
        for factor in factors:
            factor.verified_at = None
        # SEC-11: tự động thu hồi toàn bộ phiên đang hoạt động của thành viên khi đặt lại MFA.
        revoked_ids = await revoke_membership_sessions(
            unit.session,
            workspace_id=identity.principal.workspace_id,
            membership_id=body.membership_id,
            now=now,
        )
        unit.session.add(
            AuditEvent(
                workspace_id=identity.principal.workspace_id,
                actor_membership_id=identity.principal.membership_id,
                action="member.mfa_recovery_reset",
                target_type="user_membership",
                target_id=body.membership_id,
                reason=body.reason.strip(),
                after_hash=state_hash(
                    {
                        "disabled_factors": len(factors),
                        "revoked_sessions": len(revoked_ids),
                    }
                ),
                request_id=req_id,
                occurred_at=now,
            )
        )
    if identity.principal.session_id in revoked_ids:
        clear_session_cookie(response, request.app.state.settings)
    return {
        "membership_id": str(body.membership_id),
        "disabled_factors": len(factors),
        "revoked_sessions": len(revoked_ids),
        "status": "RESET",
    }
