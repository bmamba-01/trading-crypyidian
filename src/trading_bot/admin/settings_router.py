"""HTTP API cho cấu hình gốc đã xác thực theo workspace."""

from typing import Annotated, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from trading_bot.admin.settings_schemas import (
    CreateDraftRequest,
    PublishDraftRequest,
    RollbackRequest,
    UpdateDraftRequest,
)
from trading_bot.admin.settings_service import SettingsAdminError, SettingsAdminService
from trading_bot.auth.policy import Permission
from trading_bot.auth.route_policy import CsrfMode, permission_route
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity

router = APIRouter(prefix="/api/v1/settings", tags=["settings administration"])


def get_settings_admin_service(request: Request) -> SettingsAdminService:
    service = cast(
        SettingsAdminService | None,
        getattr(request.app.state, "settings_admin_service", None),
    )
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ cấu hình chưa sẵn sàng")
    return service


def raise_settings_error(error: SettingsAdminError) -> NoReturn:
    raise HTTPException(status_code=error.status_code, detail=error.message) from error


_SETTINGS_READ_POLICY = permission_route(
    Permission.SETTINGS_READ,
    denied_detail="Không đủ quyền xem cấu hình",
)
_SETTINGS_WRITE_POLICY = permission_route(
    Permission.SETTINGS_WRITE,
    csrf=CsrfMode.HEADER,
    denied_detail="Không đủ quyền thay đổi cấu hình",
)


@router.get("/current", dependencies=[_SETTINGS_READ_POLICY])
async def get_current_settings(
    response: Response,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, object]:
    service = get_settings_admin_service(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"current": await service.get_current(identity.principal)}
    except SettingsAdminError as error:
        raise_settings_error(error)


@router.get("/versions", dependencies=[_SETTINGS_READ_POLICY])
async def list_settings_versions(
    response: Response,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, object]:
    service = get_settings_admin_service(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"versions": await service.list_versions(identity.principal)}
    except SettingsAdminError as error:
        raise_settings_error(error)


@router.post("/drafts", status_code=201, dependencies=[_SETTINGS_WRITE_POLICY])
async def create_settings_draft(
    body: CreateDraftRequest,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    request: Request,
    response: Response,
) -> dict[str, object]:
    service = get_settings_admin_service(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        draft = await service.create_draft(
            identity.principal,
            settings=body.settings,
            expected_parent_version_id=body.expected_parent_version_id,
            request_id=_request_id(request),
        )
    except SettingsAdminError as error:
        raise_settings_error(error)
    return {"draft": draft}


@router.patch("/drafts/{draft_id}", dependencies=[_SETTINGS_WRITE_POLICY])
async def update_settings_draft(
    draft_id: UUID,
    body: UpdateDraftRequest,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    request: Request,
    response: Response,
) -> dict[str, object]:
    service = get_settings_admin_service(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        draft = await service.update_draft(
            identity.principal,
            draft_id=draft_id,
            expected_revision=body.expected_revision,
            settings=body.settings,
            request_id=_request_id(request),
        )
    except SettingsAdminError as error:
        raise_settings_error(error)
    return {"draft": draft}


@router.post("/drafts/{draft_id}/validate", dependencies=[_SETTINGS_WRITE_POLICY])
async def validate_settings_draft(
    draft_id: UUID,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    request: Request,
    response: Response,
) -> dict[str, object]:
    service = get_settings_admin_service(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.validate_draft(identity.principal, draft_id=draft_id)
    except SettingsAdminError as error:
        raise_settings_error(error)


@router.post("/drafts/{draft_id}/publish", dependencies=[_SETTINGS_WRITE_POLICY])
async def publish_settings_draft(
    draft_id: UUID,
    body: PublishDraftRequest,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    request: Request,
    response: Response,
) -> dict[str, object]:
    service = get_settings_admin_service(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        version = await service.publish_draft(
            identity.principal,
            draft_id=draft_id,
            expected_revision=body.expected_revision,
            request_id=_request_id(request),
        )
    except SettingsAdminError as error:
        raise_settings_error(error)
    return {"version": version}


@router.post("/versions/{version}/rollback", dependencies=[_SETTINGS_WRITE_POLICY])
async def rollback_settings(
    version: int,
    body: RollbackRequest,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    request: Request,
    response: Response,
) -> dict[str, object]:
    service = get_settings_admin_service(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await service.rollback(
            identity.principal,
            version=version,
            reason=body.reason,
            request_id=_request_id(request),
        )
    except SettingsAdminError as error:
        raise_settings_error(error)
    return {"version": result}


def _request_id(request: Request) -> UUID | None:
    value = request.headers.get("X-Request-ID")
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="X-Request-ID phải là UUID hợp lệ") from error
