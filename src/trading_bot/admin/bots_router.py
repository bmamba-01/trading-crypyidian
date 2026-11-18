"""HTTP API quản trị bot và vòng đời overlay cấu hình theo bot.

Quyền theo bot (``BotPermission``), MFA cho thay đổi risk và scope workspace được kiểm
trong ``BotConfigAdminService``; router lo CSRF, header và ánh xạ lỗi HTTP.
"""

import re
from typing import Annotated, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from trading_bot.admin.bot_schemas import (
    CreateBotDraftRequest,
    CreateBotRequest,
    PublishBotDraftRequest,
    RollbackBotConfigRequest,
    UpdateBotDraftRequest,
)
from trading_bot.admin.bot_service import BotAdminError, BotConfigAdminService
from trading_bot.auth.action_challenge import ActionChallengeError, http_step_up_scope
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Permission
from trading_bot.auth.route_policy import (
    BotScopePolicy,
    CsrfMode,
    bot_scoped_route,
    owner_route,
    permission_route,
)
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity

router = APIRouter(prefix="/api/v1/bots", tags=["bot administration"])
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,120}$")


def get_bot_admin_service(request: Request) -> BotConfigAdminService:
    service = cast(
        BotConfigAdminService | None, getattr(request.app.state, "bot_admin_service", None)
    )
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ quản trị bot chưa sẵn sàng")
    return service


def raise_bot_error(error: BotAdminError) -> NoReturn:
    detail: object = (
        {"message": error.message, "errors": error.errors}
        if error.errors
        else {"code": error.code, "message": error.message}
        if error.code is not None
        else error.message
    )
    raise HTTPException(status_code=error.status_code, detail=detail) from error


@router.get(
    "",
    dependencies=[
        permission_route(
            Permission.WORKSPACE_READ,
            bot_scope=BotScopePolicy(BotPermission.READ, param_name=None, param_location="service"),
        )
    ],
)
async def list_bots(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, object]:
    service = get_bot_admin_service(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"bots": await service.list_bots(identity.principal)}
    except BotAdminError as error:
        raise_bot_error(error)


@router.post(
    "",
    status_code=201,
    dependencies=[
        owner_route(
            csrf=CsrfMode.HEADER,
            denied_detail="Chỉ owner được tạo bot và account PAPER mới.",
        )
    ],
)
async def create_bot(
    body: CreateBotRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, object]:
    service = get_bot_admin_service(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        with http_step_up_scope(request):
            bot, created = await service.create_bot(
                identity.principal,
                name=body.name,
                max_notional=body.max_notional,
                request_id=_request_id(request),
            )
    except ActionChallengeError as error:
        raise HTTPException(status_code=error.status_code, detail=error.as_detail()) from error
    except BotAdminError as error:
        raise_bot_error(error)
    if not created:
        response.status_code = 200
    return {"bot": bot}


@router.get(
    "/{bot_id}",
    dependencies=[bot_scoped_route(BotPermission.READ)],
)
async def get_bot(
    bot_id: UUID,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, object]:
    service = get_bot_admin_service(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"bot": await service.get_bot(identity.principal, bot_id=bot_id)}
    except BotAdminError as error:
        raise_bot_error(error)


@router.post(
    "/{bot_id}/config-drafts",
    status_code=201,
    dependencies=[bot_scoped_route(BotPermission.CONFIG_WRITE, csrf=CsrfMode.HEADER)],
)
async def create_bot_config_draft(
    bot_id: UUID,
    body: CreateBotDraftRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, object]:
    service = get_bot_admin_service(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        draft = await service.create_draft(
            identity.principal,
            bot_id=bot_id,
            overlay=body.overlay,
            expected_parent_version_id=body.expected_parent_version_id,
            idempotency_key=_idempotency_key(request),
            request_id=_request_id(request),
        )
    except BotAdminError as error:
        raise_bot_error(error)
    return {"draft": draft}


@router.put(
    "/{bot_id}/config-drafts/{draft_id}",
    dependencies=[bot_scoped_route(BotPermission.CONFIG_WRITE, csrf=CsrfMode.HEADER)],
)
async def update_bot_config_draft(
    bot_id: UUID,
    draft_id: UUID,
    body: UpdateBotDraftRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, object]:
    service = get_bot_admin_service(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        draft = await service.update_draft(
            identity.principal,
            bot_id=bot_id,
            draft_id=draft_id,
            expected_revision=body.expected_revision,
            overlay=body.overlay,
            idempotency_key=_idempotency_key(request),
            request_id=_request_id(request),
        )
    except BotAdminError as error:
        raise_bot_error(error)
    return {"draft": draft}


@router.post(
    "/{bot_id}/config-drafts/{draft_id}/validate",
    dependencies=[bot_scoped_route(BotPermission.CONFIG_WRITE, csrf=CsrfMode.HEADER)],
)
async def validate_bot_config_draft(
    bot_id: UUID,
    draft_id: UUID,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, object]:
    service = get_bot_admin_service(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.validate_draft(identity.principal, bot_id=bot_id, draft_id=draft_id)
    except BotAdminError as error:
        raise_bot_error(error)


@router.post(
    "/{bot_id}/config-drafts/{draft_id}/publish",
    dependencies=[bot_scoped_route(BotPermission.CONFIG_PUBLISH, csrf=CsrfMode.HEADER)],
)
async def publish_bot_config_draft(
    bot_id: UUID,
    draft_id: UUID,
    body: PublishBotDraftRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, object]:
    service = get_bot_admin_service(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        with http_step_up_scope(request):
            version = await service.publish(
                identity.principal,
                bot_id=bot_id,
                draft_id=draft_id,
                expected_revision=body.expected_revision,
                idempotency_key=_idempotency_key(request),
                request_id=_request_id(request),
            )
    except ActionChallengeError as error:
        raise HTTPException(status_code=error.status_code, detail=error.as_detail()) from error
    except BotAdminError as error:
        raise_bot_error(error)
    return {"version": version}


@router.post(
    "/{bot_id}/config-versions/{version}/rollback",
    dependencies=[bot_scoped_route(BotPermission.CONFIG_PUBLISH, csrf=CsrfMode.HEADER)],
)
async def rollback_bot_config(
    bot_id: UUID,
    version: int,
    body: RollbackBotConfigRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, object]:
    service = get_bot_admin_service(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        with http_step_up_scope(request):
            result = await service.rollback(
                identity.principal,
                bot_id=bot_id,
                version=version,
                expected_current_version_id=body.expected_current_version_id,
                reason=body.reason,
                idempotency_key=_idempotency_key(request),
                request_id=_request_id(request),
            )
    except ActionChallengeError as error:
        raise HTTPException(status_code=error.status_code, detail=error.as_detail()) from error
    except BotAdminError as error:
        raise_bot_error(error)
    return {"version": result}


def _idempotency_key(request: Request) -> str | None:
    value = request.headers.get("Idempotency-Key")
    if value is None:
        return None
    if not _IDEMPOTENCY_KEY.fullmatch(value):
        raise HTTPException(
            status_code=400, detail="Idempotency-Key phải gồm 8–120 ký tự [A-Za-z0-9._:-]."
        )
    return value


def _request_id(request: Request) -> UUID | None:
    value = request.headers.get("X-Request-ID")
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="X-Request-ID phải là UUID hợp lệ") from error
