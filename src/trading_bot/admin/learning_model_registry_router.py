"""API quản trị registry mô hình, MFA và active pointer metadata PAPER."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from trading_bot.adapters.database.repositories.learning_model_registry import (
    LearningModelRegistryConflict,
)
from trading_bot.admin.learning_model_registry_schemas import (
    ActivePointerResponse,
    AttachEvaluationRequest,
    PointerChangeRequest,
    RegisterArtifactRequest,
    RegistryDecisionRequest,
    RegistryEntryResponse,
    RegistryEventResponse,
    RejectModelRequest,
)
from trading_bot.application.learning_model_registry import (
    PrincipalLearningModelRegistryService,
)
from trading_bot.auth.policy import Permission, authorize
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity

router = APIRouter(prefix="/api/v1/learning-models", tags=["learning model registry"])


def _facade(
    request: Request, identity: AuthIdentity, permission: Permission
) -> PrincipalLearningModelRegistryService:
    principal = identity.principal
    if not authorize(principal, permission):
        raise HTTPException(status_code=403, detail="Không có quyền quản lý registry mô hình.")
    service = getattr(request.app.state, "learning_model_registry_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ registry mô hình chưa sẵn sàng.")
    return cast(PrincipalLearningModelRegistryService, service.for_principal(principal))


def _map_error(error: Exception) -> HTTPException:
    if isinstance(error, PermissionError):
        return HTTPException(status_code=403, detail="Không có quyền thực hiện thao tác này.")
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail="Không tìm thấy artifact hoặc registry entry.")
    if isinstance(error, (ValueError, LearningModelRegistryConflict)):
        return HTTPException(status_code=409, detail="Registry đã thay đổi hoặc thao tác xung đột.")
    return HTTPException(status_code=500, detail="Không thể xử lý registry mô hình.")


def _raise_no_store(error: HTTPException) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail=error.detail,
        headers={**(error.headers or {}), "Cache-Control": "no-store"},
    )


@router.get("", response_model=list[RegistryEntryResponse])
async def list_entries(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> list[RegistryEntryResponse]:
    response.headers["Cache-Control"] = "no-store"
    try:
        entries = await _facade(request, identity, Permission.LEARNING_MODEL_READ).list_entries()
    except HTTPException as error:
        raise _raise_no_store(error) from error
    except (PermissionError, LookupError, ValueError, LearningModelRegistryConflict) as error:
        raise _raise_no_store(_map_error(error)) from error
    return [RegistryEntryResponse.from_domain(entry) for entry in entries]


@router.get("/active-pointer", response_model=ActivePointerResponse)
async def get_active_pointer(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> ActivePointerResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        pointer = await _facade(
            request, identity, Permission.LEARNING_MODEL_READ
        ).get_active_pointer()
    except HTTPException as error:
        raise _raise_no_store(error) from error
    except (PermissionError, LookupError, ValueError, LearningModelRegistryConflict) as error:
        raise _raise_no_store(_map_error(error)) from error
    return ActivePointerResponse.from_domain(pointer)


@router.get("/{registry_id}", response_model=RegistryEntryResponse)
async def get_entry(
    registry_id: UUID,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> RegistryEntryResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        entry = await _facade(request, identity, Permission.LEARNING_MODEL_READ).get_entry(
            registry_id
        )
    except HTTPException as error:
        raise _raise_no_store(error) from error
    except (PermissionError, LookupError, ValueError, LearningModelRegistryConflict) as error:
        raise _raise_no_store(_map_error(error)) from error
    return RegistryEntryResponse.from_domain(entry)


@router.get("/{registry_id}/events", response_model=list[RegistryEventResponse])
async def list_events(
    registry_id: UUID,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> list[RegistryEventResponse]:
    response.headers["Cache-Control"] = "no-store"
    try:
        events = await _facade(request, identity, Permission.LEARNING_MODEL_READ).list_events(
            registry_id
        )
    except HTTPException as error:
        raise _raise_no_store(error) from error
    except (PermissionError, LookupError, ValueError, LearningModelRegistryConflict) as error:
        raise _raise_no_store(_map_error(error)) from error
    return [RegistryEventResponse.from_domain(event) for event in events]


@router.post("", status_code=201, response_model=RegistryEntryResponse)
async def register_artifact(
    body: RegisterArtifactRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> RegistryEntryResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        entry = await _facade(request, identity, Permission.LEARNING_MODEL_WRITE).register_artifact(
            artifact_id=body.artifact_id,
            idempotency_key=idempotency_key,
            reason=body.reason,
        )
    except HTTPException as error:
        raise _raise_no_store(error) from error
    except (PermissionError, LookupError, ValueError, LearningModelRegistryConflict) as error:
        raise _raise_no_store(_map_error(error)) from error
    return RegistryEntryResponse.from_domain(entry)


@router.post("/{registry_id}/evaluation", response_model=RegistryEntryResponse)
async def attach_evaluation(
    registry_id: UUID,
    body: AttachEvaluationRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> RegistryEntryResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        entry = await _facade(request, identity, Permission.LEARNING_MODEL_WRITE).attach_evaluation(
            registry_id=registry_id,
            evaluation_report_id=body.evaluation_report_id,
            evaluation_report_hash=body.evaluation_report_hash,
            expected_revision=body.expected_revision,
            idempotency_key=idempotency_key,
            reason=body.reason,
        )
    except HTTPException as error:
        raise _raise_no_store(error) from error
    except (PermissionError, LookupError, ValueError, LearningModelRegistryConflict) as error:
        raise _raise_no_store(_map_error(error)) from error
    return RegistryEntryResponse.from_domain(entry)


@router.post("/{registry_id}/approve", response_model=RegistryEntryResponse)
async def approve_entry(
    registry_id: UUID,
    body: RegistryDecisionRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> RegistryEntryResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        entry = await _facade(request, identity, Permission.LEARNING_MODEL_APPROVE).approve(
            registry_id=registry_id,
            expected_revision=body.expected_revision,
            idempotency_key=idempotency_key,
            reason=body.reason,
        )
    except HTTPException as error:
        raise _raise_no_store(error) from error
    except (PermissionError, LookupError, ValueError, LearningModelRegistryConflict) as error:
        raise _raise_no_store(_map_error(error)) from error
    return RegistryEntryResponse.from_domain(entry)


@router.post("/{registry_id}/reject", response_model=RegistryEntryResponse)
async def reject_entry(
    registry_id: UUID,
    body: RejectModelRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> RegistryEntryResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        entry = await _facade(request, identity, Permission.LEARNING_MODEL_APPROVE).reject(
            registry_id=registry_id,
            expected_revision=body.expected_revision,
            idempotency_key=idempotency_key,
            reason_code=body.reason_code,
            reason=body.reason,
        )
    except HTTPException as error:
        raise _raise_no_store(error) from error
    except (PermissionError, LookupError, ValueError, LearningModelRegistryConflict) as error:
        raise _raise_no_store(_map_error(error)) from error
    return RegistryEntryResponse.from_domain(entry)


@router.post("/{registry_id}/activate", response_model=RegistryEntryResponse)
async def activate_entry(
    registry_id: UUID,
    body: PointerChangeRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> RegistryEntryResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        entry, _pointer = await _facade(
            request, identity, Permission.LEARNING_MODEL_APPROVE
        ).activate(
            registry_id=registry_id,
            expected_revision=body.expected_revision,
            expected_pointer_version=body.expected_pointer_version,
            expected_current_registry_id=body.expected_current_registry_id,
            idempotency_key=idempotency_key,
            reason=body.reason,
        )
    except HTTPException as error:
        raise _raise_no_store(error) from error
    except (PermissionError, LookupError, ValueError, LearningModelRegistryConflict) as error:
        raise _raise_no_store(_map_error(error)) from error
    return RegistryEntryResponse.from_domain(entry)


@router.post("/{registry_id}/rollback", response_model=RegistryEntryResponse)
async def rollback_entry(
    registry_id: UUID,
    body: PointerChangeRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> RegistryEntryResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        entry, _pointer = await _facade(
            request, identity, Permission.LEARNING_MODEL_APPROVE
        ).rollback(
            registry_id=registry_id,
            expected_revision=body.expected_revision,
            expected_pointer_version=body.expected_pointer_version,
            expected_current_registry_id=body.expected_current_registry_id,
            idempotency_key=idempotency_key,
            reason=body.reason,
        )
    except HTTPException as error:
        raise _raise_no_store(error) from error
    except (PermissionError, LookupError, ValueError, LearningModelRegistryConflict) as error:
        raise _raise_no_store(_map_error(error)) from error
    return RegistryEntryResponse.from_domain(entry)


@router.post("/{registry_id}/retire", response_model=RegistryEntryResponse)
async def retire_entry(
    registry_id: UUID,
    body: RegistryDecisionRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> RegistryEntryResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        entry = await _facade(request, identity, Permission.LEARNING_MODEL_APPROVE).retire(
            registry_id=registry_id,
            expected_revision=body.expected_revision,
            idempotency_key=idempotency_key,
            reason=body.reason,
        )
    except HTTPException as error:
        raise _raise_no_store(error) from error
    except (PermissionError, LookupError, ValueError, LearningModelRegistryConflict) as error:
        raise _raise_no_store(_map_error(error)) from error
    return RegistryEntryResponse.from_domain(entry)


__all__ = ["router"]
