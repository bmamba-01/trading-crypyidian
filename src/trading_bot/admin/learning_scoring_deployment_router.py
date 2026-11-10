"""HTTP governance API cho scorer deployment, với CSRF và Principal server-side."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from trading_bot.adapters.database.repositories.learning_scoring_deployments import (
    ScorerDeploymentConflict,
    ScorerDeploymentRightsUnavailable,
)
from trading_bot.admin.learning_scoring_deployment_schemas import (
    ScorerDeploymentRequest,
    ScorerDeploymentResponse,
)
from trading_bot.application.learning_scoring_deployment import (
    PrincipalScorerDeploymentService,
    ScorerDeploymentService,
)
from trading_bot.auth.policy import Permission, authorize
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity

router = APIRouter(prefix="/api/v1/learning-scorer", tags=["learning scorer deployment"])


def _facade(
    request: Request, identity: AuthIdentity, permission: Permission
) -> PrincipalScorerDeploymentService:
    principal = identity.principal
    if not authorize(principal, permission):
        raise HTTPException(status_code=403, detail="Không có quyền quản lý scorer deployment.")
    service = getattr(request.app.state, "learning_scoring_deployment_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ scorer deployment chưa sẵn sàng.")
    return cast(ScorerDeploymentService, service).for_principal(principal)


def _mapped_error(error: Exception) -> HTTPException:
    if isinstance(error, PermissionError):
        return HTTPException(status_code=403, detail="Cần quyền phù hợp và TOTP gần đây.")
    if isinstance(error, (ScorerDeploymentConflict, ScorerDeploymentRightsUnavailable)):
        return HTTPException(
            status_code=409,
            detail="Deployment scorer đã đổi hoặc chưa đủ quyền hiện hành để bật.",
        )
    if isinstance(error, (LookupError, ValueError)):
        return HTTPException(status_code=409, detail="Yêu cầu scorer deployment không hợp lệ.")
    return HTTPException(status_code=500, detail="Không thể xử lý scorer deployment.")


def _no_store(error: HTTPException) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail=error.detail,
        headers={**(error.headers or {}), "Cache-Control": "no-store"},
    )


@router.get("/deployment", response_model=ScorerDeploymentResponse)
async def get_deployment(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> ScorerDeploymentResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        snapshot = await _facade(
            request, identity, Permission.LEARNING_MODEL_READ
        ).load_admin_scoring_deployment()
    except HTTPException as error:
        raise _no_store(error) from error
    except (
        PermissionError,
        LookupError,
        ValueError,
        ScorerDeploymentConflict,
        ScorerDeploymentRightsUnavailable,
    ) as error:
        raise _no_store(_mapped_error(error)) from error
    return ScorerDeploymentResponse.from_snapshot(snapshot)


@router.post("/deployment", response_model=ScorerDeploymentResponse)
async def set_deployment(
    body: ScorerDeploymentRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> ScorerDeploymentResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        event = await _facade(request, identity, Permission.LEARNING_MODEL_APPROVE).set_mode(
            mode=body.mode,
            expected_pointer_version=body.expected_pointer_version,
            expected_deployment_revision=body.expected_deployment_revision,
            idempotency_key=idempotency_key,
            reason=body.reason,
        )
    except HTTPException as error:
        raise _no_store(error) from error
    except (
        PermissionError,
        LookupError,
        ValueError,
        ScorerDeploymentConflict,
        ScorerDeploymentRightsUnavailable,
    ) as error:
        raise _no_store(_mapped_error(error)) from error
    return ScorerDeploymentResponse.from_event(event)


__all__ = ["router"]
