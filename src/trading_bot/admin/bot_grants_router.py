"""HTTP API quản trị BotGrant và báo cáo rà soát quyền định kỳ (SEC-25)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from trading_bot.admin.bot_grant_service import BotGrantAdminError, BotGrantAdminService
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.route_policy import CsrfMode, owner_route
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity

router = APIRouter(prefix="/api/v1/bot-grants", tags=["bot grants"])


class GrantBotPermissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bot_id: UUID
    membership_id: UUID
    permission: BotPermission
    reason: str = Field(min_length=1, max_length=500)
    expires_at: datetime | None = None
    permanent: bool = False


class RevokeBotPermissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bot_id: UUID
    membership_id: UUID
    permission: BotPermission
    reason: str = Field(min_length=1, max_length=500)


class AccessReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bot_id: UUID | None = None
    membership_id: UUID | None = None
    expiring_within_days: int = Field(default=7, ge=1, le=365)
    include_revoked: bool = True
    reason: str | None = Field(default=None, min_length=1, max_length=500)


def _service(request: Request) -> BotGrantAdminService:
    service = cast(
        BotGrantAdminService | None, getattr(request.app.state, "bot_grant_service", None)
    )
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ quản trị grant bot chưa sẵn sàng")
    return service


def _raise_error(error: BotGrantAdminError) -> NoReturn:
    raise HTTPException(status_code=error.status_code, detail=error.message) from error


def _request_id(request: Request) -> UUID | None:
    value = request.headers.get("X-Request-ID")
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="X-Request-ID phải là UUID hợp lệ") from error


@router.post("", status_code=201, dependencies=[owner_route(csrf=CsrfMode.HEADER)])
async def grant_bot_permission(
    body: GrantBotPermissionRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    try:
        grant = await _service(request).grant(
            identity.principal,
            bot_id=body.bot_id,
            membership_id=body.membership_id,
            permission=body.permission,
            reason=body.reason,
            expires_at=body.expires_at,
            permanent=body.permanent,
            request_id=_request_id(request),
        )
    except BotGrantAdminError as error:
        _raise_error(error)
    return {"grant": grant}


@router.post("/revoke", dependencies=[owner_route(csrf=CsrfMode.HEADER)])
async def revoke_bot_permission(
    body: RevokeBotPermissionRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await _service(request).revoke(
            identity.principal,
            bot_id=body.bot_id,
            membership_id=body.membership_id,
            permission=body.permission,
            reason=body.reason,
            request_id=_request_id(request),
        )
    except BotGrantAdminError as error:
        _raise_error(error)
    return result


@router.get("/review", dependencies=[owner_route()])
async def review_bot_grants(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    bot_id: UUID | None = None,
    membership_id: UUID | None = None,
    expiring_within_days: int = Query(default=7, ge=1, le=365),
    include_revoked: bool = False,
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    try:
        review = await _service(request).review_grants(
            identity.principal,
            bot_id=bot_id,
            membership_id=membership_id,
            expiring_within_days=expiring_within_days,
            include_revoked=include_revoked,
            record_audit=False,
        )
    except BotGrantAdminError as error:
        _raise_error(error)
    return {"review": review}


@router.post("/review", dependencies=[owner_route(csrf=CsrfMode.HEADER)])
async def record_bot_grant_review(
    body: AccessReviewRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    try:
        review = await _service(request).review_grants(
            identity.principal,
            bot_id=body.bot_id,
            membership_id=body.membership_id,
            expiring_within_days=body.expiring_within_days,
            include_revoked=body.include_revoked,
            record_audit=True,
            reason=body.reason,
            request_id=_request_id(request),
        )
    except BotGrantAdminError as error:
        _raise_error(error)
    return {"review": review}
