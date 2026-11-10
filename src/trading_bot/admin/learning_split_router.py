"""API metadata và MFA approval cho candidate split WP06."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from trading_bot.adapters.database.repositories.learning_split_approvals import (
    LearningSplitApprovalConflict,
)
from trading_bot.admin.learning_split_schemas import (
    ApproveLearningSplitRequest,
    LearningSplitApprovalResponse,
    LearningSplitCandidateListResponse,
    LearningSplitCandidateResponse,
)
from trading_bot.application.learning_split_admin import PrincipalLearningSplitAdminService
from trading_bot.auth.policy import Permission, authorize
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity

router = APIRouter(prefix="/api/v1/learning-splits", tags=["learning split approvals"])


def _facade(request: Request, identity: AuthIdentity) -> PrincipalLearningSplitAdminService:
    principal = identity.principal
    if not authorize(principal, Permission.LEARNING_SPLIT_APPROVE):
        raise HTTPException(status_code=403, detail="Không có quyền duyệt split learning.")
    service = getattr(request.app.state, "learning_split_admin_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ duyệt split chưa sẵn sàng.")
    return cast(PrincipalLearningSplitAdminService, service.for_principal(principal))


def _map_error(error: Exception) -> HTTPException:
    if isinstance(error, PermissionError):
        return HTTPException(status_code=403, detail="Không có quyền hoặc cần MFA gần đây.")
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail="Không tìm thấy candidate trong workspace.")
    if isinstance(error, LearningSplitApprovalConflict):
        return HTTPException(
            status_code=409,
            detail="Approval split đã xung đột với dữ liệu hiện có.",
        )
    if isinstance(error, ValueError):
        return HTTPException(status_code=409, detail="Candidate hoặc approval đã thay đổi.")
    return HTTPException(status_code=500, detail="Không thể xử lý approval split.")


def _no_store(error: HTTPException) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail=error.detail,
        headers={**(error.headers or {}), "Cache-Control": "no-store"},
    )


@router.get("/candidates", response_model=LearningSplitCandidateListResponse)
async def list_split_candidates(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> LearningSplitCandidateListResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        candidates = await _facade(request, identity).list_candidates()
    except HTTPException as error:
        raise _no_store(error) from error
    except (PermissionError, LookupError, ValueError) as error:
        raise _no_store(_map_error(error)) from error
    return LearningSplitCandidateListResponse(
        candidates=[LearningSplitCandidateResponse.from_domain(item) for item in candidates]
    )


@router.post(
    "/candidates/{candidate_id}/approve",
    status_code=201,
    response_model=LearningSplitApprovalResponse,
)
async def approve_split_candidate(
    candidate_id: UUID,
    body: ApproveLearningSplitRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> LearningSplitApprovalResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        approval = await _facade(request, identity).approve(
            candidate_id=candidate_id,
            reason=body.reason,
            idempotency_key=idempotency_key,
        )
    except HTTPException as error:
        raise _no_store(error) from error
    except (PermissionError, LookupError, LearningSplitApprovalConflict, ValueError) as error:
        raise _no_store(_map_error(error)) from error
    return LearningSplitApprovalResponse.from_domain(candidate_id, approval)


__all__ = ["router"]
