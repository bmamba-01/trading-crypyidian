"""API quản trị enqueue, theo dõi và yêu cầu hủy training job."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from trading_bot.admin.learning_training_schemas import (
    EnqueueTrainingRequest,
    LearningTrainingFoldOptionResponse,
    LearningTrainingJobResponse,
    LearningTrainingSnapshotResponse,
    LearningTrainingSubmissionResponse,
)
from trading_bot.application.learning_training_admin import (
    PrincipalLearningTrainingAdminService,
)
from trading_bot.auth.policy import Permission, authorize
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity

router = APIRouter(prefix="/api/v1/learning-training", tags=["learning training jobs"])


def _facade(request: Request, identity: AuthIdentity) -> PrincipalLearningTrainingAdminService:
    principal = identity.principal
    if not authorize(principal, Permission.LEARNING_TRAINING_MANAGE):
        raise HTTPException(status_code=403, detail="Không có quyền quản lý training job.")
    service = getattr(request.app.state, "learning_training_admin_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ training admin chưa sẵn sàng.")
    return cast(PrincipalLearningTrainingAdminService, service.for_principal(principal))


def _map_error(error: Exception) -> HTTPException:
    if isinstance(error, PermissionError):
        return HTTPException(status_code=403, detail="Không có quyền hoặc cần MFA gần đây.")
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail="Không tìm thấy fold hoặc training job.")
    if isinstance(error, ValueError):
        return HTTPException(
            status_code=409,
            detail="Yêu cầu training không hợp lệ hoặc đã thay đổi.",
        )
    return HTTPException(status_code=500, detail="Không thể xử lý training job.")


def _no_store(error: HTTPException) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail=error.detail,
        headers={**(error.headers or {}), "Cache-Control": "no-store"},
    )


@router.get("/jobs", response_model=LearningTrainingSnapshotResponse)
async def list_training_jobs(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> LearningTrainingSnapshotResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        facade = _facade(request, identity)
        folds, jobs = await facade.list_eligible_folds(), await facade.list_jobs()
    except HTTPException as error:
        raise _no_store(error) from error
    except (PermissionError, LookupError, ValueError) as error:
        raise _no_store(_map_error(error)) from error
    return LearningTrainingSnapshotResponse(
        eligible_folds=[LearningTrainingFoldOptionResponse.from_domain(item) for item in folds],
        jobs=[LearningTrainingJobResponse.from_domain(item) for item in jobs],
    )


@router.post("/jobs", status_code=201, response_model=LearningTrainingSubmissionResponse)
async def enqueue_training_job(
    body: EnqueueTrainingRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> LearningTrainingSubmissionResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        submission = await _facade(request, identity).enqueue(
            normalized_dataset_id=body.normalized_dataset_id,
            fold_id=body.fold_id,
            idempotency_key=idempotency_key,
        )
    except HTTPException as error:
        raise _no_store(error) from error
    except (PermissionError, LookupError, ValueError) as error:
        raise _no_store(_map_error(error)) from error
    return LearningTrainingSubmissionResponse.from_domain(submission)


@router.post("/jobs/{job_id}/cancel", response_model=LearningTrainingJobResponse)
async def cancel_training_job(
    job_id: UUID,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> LearningTrainingJobResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        snapshot = await _facade(request, identity).cancel(job_id=job_id)
    except HTTPException as error:
        raise _no_store(error) from error
    except (PermissionError, LookupError, ValueError) as error:
        raise _no_store(_map_error(error)) from error
    return LearningTrainingJobResponse.from_domain(snapshot)


__all__ = ["router"]
