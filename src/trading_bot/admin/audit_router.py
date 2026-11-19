"""HTTP API và trang Admin xem nhật ký audit bất biến theo workspace (WP-04C)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import AuditEvent
from trading_bot.admin.web_router import _templates
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.action_challenge import ACTION_SECRET_WRITE
from trading_bot.auth.policy import Permission, authorize
from trading_bot.auth.route_policy import CsrfMode, owner_route, permission_route
from trading_bot.auth.router import current_identity, enforce_endpoint_step_up
from trading_bot.auth.service import AuthIdentity
from trading_bot.observability.audit_chain import verify_audit_chain
from trading_bot.observability.security_audit import (
    SecretRotationService,
    request_security_audit_scope,
)

router = APIRouter(tags=["nhật ký audit"])
_html_router = APIRouter(tags=["admin web"])


def _get_sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    sessions = cast(
        async_sessionmaker[AsyncSession] | None,
        getattr(request.app.state, "session_factory", None),
    )
    if sessions is None:
        raise HTTPException(status_code=503, detail="Cơ sở dữ liệu audit chưa sẵn sàng")
    return sessions


def _require_audit_read(identity: AuthIdentity) -> None:
    if not (
        authorize(identity.principal, Permission.SETTINGS_READ)
        or authorize(identity.principal, Permission.WORKSPACE_READ)
    ):
        raise HTTPException(status_code=403, detail="Không đủ quyền xem nhật ký audit")


async def _query_audit_events(
    sessions: async_sessionmaker[AsyncSession],
    *,
    workspace_id: UUID,
    actor_membership_id: UUID | None = None,
    action: str | None = None,
    target_type: str | None = None,
    request_id: UUID | None = None,
    from_at: datetime | None = None,
    to_at: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
        stmt = select(AuditEvent).where(AuditEvent.workspace_id == workspace_id)
        if actor_membership_id is not None:
            stmt = stmt.where(AuditEvent.actor_membership_id == actor_membership_id)
        if action:
            stmt = stmt.where(AuditEvent.action == action.strip())
        if target_type:
            stmt = stmt.where(AuditEvent.target_type == target_type.strip())
        if request_id is not None:
            stmt = stmt.where(AuditEvent.request_id == request_id)
        if from_at is not None:
            stmt = stmt.where(AuditEvent.occurred_at >= from_at)
        if to_at is not None:
            stmt = stmt.where(AuditEvent.occurred_at <= to_at)
        records = (
            await unit.session.scalars(stmt.order_by(AuditEvent.occurred_at.desc()).limit(limit))
        ).all()
        return [
            {
                "id": str(item.id),
                "actor_membership_id": (
                    str(item.actor_membership_id) if item.actor_membership_id else None
                ),
                "action": item.action,
                "target_type": item.target_type,
                "target_id": str(item.target_id) if item.target_id else None,
                "reason": item.reason,
                "before_hash": item.before_hash,
                "after_hash": item.after_hash,
                "request_id": str(item.request_id),
                "occurred_at": item.occurred_at.isoformat(),
                "seq": getattr(item, "seq", None),
                "prev_hash": getattr(item, "prev_hash", None),
                "row_hash": getattr(item, "row_hash", None),
                "db_role": getattr(item, "db_role", None),
                "recorded_at": (
                    item.recorded_at.isoformat()
                    if getattr(item, "recorded_at", None) is not None
                    else None
                ),
                "session_hash": getattr(item, "session_hash", None),
                "client_ip": getattr(item, "client_ip", None),
                "user_agent": getattr(item, "user_agent", None),
                "outcome": getattr(item, "outcome", None),
            }
            for item in records
        ]


@router.get(
    "/api/v1/audit",
    summary="Tra cứu nhật ký audit",
    description=(
        "Liệt kê các sự kiện audit trong workspace kèm actor, target, "
        "hash trước/sau, lý do và correlation."
    ),
    dependencies=[permission_route(Permission.WORKSPACE_READ)],
)
async def list_audit_events(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    actor_membership_id: Annotated[UUID | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    target_type: Annotated[str | None, Query()] = None,
    request_id: Annotated[UUID | None, Query()] = None,
    from_at: Annotated[datetime | None, Query()] = None,
    to_at: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, object]:
    _require_audit_read(identity)
    response.headers["Cache-Control"] = "no-store"
    sessions = _get_sessions(request)
    events = await _query_audit_events(
        sessions,
        workspace_id=identity.principal.workspace_id,
        actor_membership_id=actor_membership_id,
        action=action,
        target_type=target_type,
        request_id=request_id,
        from_at=from_at,
        to_at=to_at,
        limit=limit,
    )
    return {"events": events}


class SecretRotateRequest(BaseModel):
    """Yêu cầu xoay tham chiếu khóa bí mật trong workspace (SEC-32)."""

    secret_reference_id: UUID
    new_reference_key: str = Field(..., min_length=1, max_length=240)
    reason: str = Field(..., min_length=3, max_length=500)
    new_provider: str | None = Field(default=None, max_length=32)


@router.get(
    "/api/v1/audit/verify",
    summary="Xác minh toàn vẹn chuỗi băm nhật ký audit",
    description=(
        "Kiểm tra tính liên tục của seq, prev_hash và row_hash trên toàn bộ chuỗi audit "
        "của workspace hiện tại và phát cảnh báo CRITICAL nếu phát hiện giả mạo."
    ),
    dependencies=[permission_route(Permission.WORKSPACE_READ)],
)
async def verify_workspace_audit_chain(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    expected_min_last_seq: Annotated[int | None, Query(ge=1)] = None,
    expected_head_hash: Annotated[str | None, Query(min_length=64, max_length=64)] = None,
) -> dict[str, Any]:
    _require_audit_read(identity)
    response.headers["Cache-Control"] = "no-store"
    sessions = _get_sessions(request)
    detector = getattr(request.app.state, "security_anomaly_detector", None)
    result = await verify_audit_chain(
        sessions,
        workspace_id=identity.principal.workspace_id,
        anomaly_detector=detector,
        expected_min_last_seq=expected_min_last_seq,
        expected_head_hash=expected_head_hash,
    )
    return result.to_dict()


@router.post(
    "/api/v1/secrets/rotate",
    summary="Xoay tham chiếu khóa bí mật",
    description=(
        "Cập nhật tham chiếu secret sang khóa mới có bảo vệ step-up challenge, "
        "ghi nhật ký audit bất biến và phát cảnh báo thay đổi đặc quyền."
    ),
    dependencies=[owner_route(csrf=CsrfMode.HEADER)],
)
async def rotate_workspace_secret(
    request: Request,
    response: Response,
    payload: SecretRotateRequest,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    sessions = _get_sessions(request)
    await enforce_endpoint_step_up(
        request,
        identity,
        action=ACTION_SECRET_WRITE,
        target={
            "secret_reference_id": str(payload.secret_reference_id),
            "new_reference_key": payload.new_reference_key.strip(),
        },
    )
    detector = getattr(request.app.state, "security_anomaly_detector", None)
    service = SecretRotationService(sessions=sessions, anomaly_detector=detector)
    with request_security_audit_scope(request):
        try:
            rotated = await service.rotate_secret(
                identity.principal,
                secret_reference_id=payload.secret_reference_id,
                new_reference_key=payload.new_reference_key,
                reason=payload.reason,
                new_provider=payload.new_provider,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "secret_reference_id": str(rotated.secret_reference_id),
        "workspace_id": str(rotated.workspace_id),
        "provider": rotated.provider,
        "previous_reference_key": rotated.previous_reference_key,
        "new_reference_key": rotated.new_reference_key,
        "status": rotated.status,
        "rotated_at": rotated.rotated_at.isoformat(),
    }


@_html_router.get(
    "/admin/audit",
    response_class=HTMLResponse,
    summary="Trang xem nhật ký audit",
    description=(
        "Hiển thị bảng nhật ký audit với bộ lọc theo hành động, đối tượng và correlation ID."
    ),
    dependencies=[permission_route(Permission.WORKSPACE_READ)],
)
async def admin_audit_page(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    action: Annotated[str | None, Query()] = None,
    target_type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> HTMLResponse:
    _require_audit_read(identity)
    sessions = _get_sessions(request)
    events = await _query_audit_events(
        sessions,
        workspace_id=identity.principal.workspace_id,
        action=action,
        target_type=target_type,
        limit=limit,
    )
    templates = _templates(request)
    response = templates.TemplateResponse(
        request,
        "admin_audit.html",
        {
            "events": events,
            "action_filter": action or "",
            "target_type_filter": target_type or "",
            "active_nav": "audit",
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


router.include_router(_html_router)
