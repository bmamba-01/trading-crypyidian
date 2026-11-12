"""Giao diện HTTP cho chủ sở hữu/quản trị rủi ro xử lý tạm chặn lệnh vào."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.exc import SQLAlchemyError

from trading_bot.adapters.database.repositories.learning_incident_entry_holds import (
    LearningIncidentHoldConflict,
    LearningIncidentHoldUnavailable,
)
from trading_bot.admin.learning_incident_entry_hold_schemas import (
    LearningIncidentHoldEventResponse,
    LearningIncidentHoldHistoryResponse,
    LearningIncidentHoldRequest,
    LearningIncidentReleaseRequest,
)
from trading_bot.application.learning_incident_entry_hold import (
    LearningIncidentEntryHoldService,
    PrincipalLearningIncidentEntryHoldService,
)
from trading_bot.auth.policy import Permission, authorize
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity

router = APIRouter(
    prefix="/api/v1/learning/incident-entry-holds",
    tags=["learning incident entry hold"],
)


def _facade(request: Request, identity: AuthIdentity) -> PrincipalLearningIncidentEntryHoldService:
    principal = identity.principal
    if not authorize(principal, Permission.LEARNING_INCIDENT_MANAGE):
        raise HTTPException(status_code=403, detail="Không có quyền quản lý sự cố học máy.")
    service = getattr(request.app.state, "learning_incident_entry_hold_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ chặn lệnh vào do sự cố chưa sẵn sàng.")
    return cast(LearningIncidentEntryHoldService, service).for_principal(principal)


def _mapped_error(error: Exception) -> HTTPException:
    if isinstance(error, PermissionError):
        return HTTPException(status_code=403, detail="Cần quyền phù hợp và mã xác thực gần đây.")
    if isinstance(error, LearningIncidentHoldConflict):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, (LearningIncidentHoldUnavailable, SQLAlchemyError)):
        return HTTPException(
            status_code=503,
            detail="Chưa thể xác minh trạng thái sự cố an toàn.",
        )
    if isinstance(error, (LookupError, ValueError)):
        return HTTPException(status_code=409, detail="Yêu cầu sự cố không còn hợp lệ.")
    return HTTPException(status_code=500, detail="Không thể xử lý sự cố học máy.")


def _no_store(error: HTTPException) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail=error.detail,
        headers={**(error.headers or {}), "Cache-Control": "no-store"},
    )


@router.get("", response_model=LearningIncidentHoldHistoryResponse)
async def list_incident_hold_history(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> LearningIncidentHoldHistoryResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        events = await _facade(request, identity).list_history(limit=200)
    except HTTPException as error:
        raise _no_store(error) from error
    except (PermissionError, LookupError, ValueError, SQLAlchemyError) as error:
        raise _no_store(_mapped_error(error)) from error
    return LearningIncidentHoldHistoryResponse.from_events(events)


@router.post("/{proposal_id}/hold", response_model=LearningIncidentHoldEventResponse)
async def hold_incident_entry(
    proposal_id: UUID,
    body: LearningIncidentHoldRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> LearningIncidentHoldEventResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        event = await _facade(request, identity).hold(
            proposal_id=proposal_id,
            assessment_id=body.assessment_id,
            assessment_hash=body.assessment_hash,
            expected_revision=body.expected_revision,
            expected_deployment_revision=body.expected_deployment_revision,
            expected_active_pointer_version=body.expected_active_pointer_version,
            idempotency_key=idempotency_key,
            reason=body.reason,
        )
    except HTTPException as error:
        raise _no_store(error) from error
    except (
        PermissionError,
        LookupError,
        ValueError,
        LearningIncidentHoldConflict,
        LearningIncidentHoldUnavailable,
        SQLAlchemyError,
    ) as error:
        raise _no_store(_mapped_error(error)) from error
    return LearningIncidentHoldEventResponse.from_event(event)


@router.post("/{proposal_id}/release", response_model=LearningIncidentHoldEventResponse)
async def release_incident_entry(
    proposal_id: UUID,
    body: LearningIncidentReleaseRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> LearningIncidentHoldEventResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        event = await _facade(request, identity).release(
            proposal_id=proposal_id,
            expected_revision=body.expected_revision,
            idempotency_key=idempotency_key,
            reason=body.reason,
        )
    except HTTPException as error:
        raise _no_store(error) from error
    except (
        PermissionError,
        LookupError,
        ValueError,
        LearningIncidentHoldConflict,
        LearningIncidentHoldUnavailable,
        SQLAlchemyError,
    ) as error:
        raise _no_store(_mapped_error(error)) from error
    return LearningIncidentHoldEventResponse.from_event(event)


__all__ = ["router"]
