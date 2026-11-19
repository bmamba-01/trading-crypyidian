"""Route Admin chặn thủ công một entry PAPER đã được scorer duyệt."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import SQLAlchemyError

from trading_bot.adapters.database.repositories.learning_scorer_paper_entry_blocks import (
    LearningPaperEntryBlockConflict,
)
from trading_bot.application.learning_paper_entry_block import (
    LearningPaperEntryBlockService,
    PrincipalLearningPaperEntryBlockService,
)
from trading_bot.application.learning_scorer_score_event_read_model import (
    ScorerScoreEventReadModel,
)
from trading_bot.auth.policy import Permission, authorize
from trading_bot.auth.route_policy import CsrfMode, permission_route
from trading_bot.auth.router import require_csrf
from trading_bot.auth.service import AuthIdentity

router = APIRouter(tags=["learning PAPER entry block"])


class PaperEntryBlockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)
    confirm: bool = Field(strict=True)

    @model_validator(mode="after")
    def require_confirmation(self) -> PaperEntryBlockRequest:
        if not self.confirm:
            raise ValueError("Cần xác nhận rõ trước khi chặn entry PAPER.")
        return self


def _facade(request: Request, identity: AuthIdentity) -> PrincipalLearningPaperEntryBlockService:
    principal = identity.principal
    if not authorize(principal, Permission.LEARNING_PAPER_ENTRY_BLOCK):
        raise HTTPException(
            status_code=403,
            detail="Không có quyền chặn entry PAPER cho một quyết định scorer.",
        )
    service = getattr(request.app.state, "learning_paper_entry_block_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ chặn entry PAPER chưa sẵn sàng.")
    return cast(LearningPaperEntryBlockService, service).for_principal(principal)


def _mapped_error(error: Exception) -> HTTPException:
    if isinstance(error, PermissionError):
        return HTTPException(
            status_code=403,
            detail="Cần quyền owner/quản trị rủi ro và TOTP gần đây để chặn entry PAPER.",
        )
    if isinstance(error, LearningPaperEntryBlockConflict):
        return HTTPException(
            status_code=409,
            detail="Quyết định không còn đủ điều kiện chặn hoặc đã gửi OrderIntent PAPER.",
        )
    if isinstance(error, (LookupError, ValueError)):
        return HTTPException(
            status_code=409,
            detail="Quyết định hoặc lý do không còn đủ điều kiện chặn entry PAPER.",
        )
    if isinstance(error, SQLAlchemyError):
        return HTTPException(
            status_code=503,
            detail="Chưa thể xác minh và ghi thao tác chặn entry PAPER an toàn.",
        )
    return HTTPException(status_code=500, detail="Không thể xử lý yêu cầu chặn entry PAPER.")


def _no_store(error: HTTPException) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail=error.detail,
        headers={**(error.headers or {}), "Cache-Control": "no-store"},
    )


@router.post(
    "/admin/learning/scoring/events/{score_event_id}/paper-block",
    dependencies=[
        permission_route(
            Permission.LEARNING_PAPER_ENTRY_BLOCK,
            csrf=CsrfMode.HEADER,
        )
    ],
)
async def block_paper_entry_for_score_event(
    score_event_id: UUID,
    body: PaperEntryBlockRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> dict[str, str]:
    """Dùng Principal, score-event link và Task 2 service làm nguồn xác thực."""
    response.headers["Cache-Control"] = "no-store"
    try:
        facade = _facade(request, identity)
        read_model = getattr(request.app.state, "learning_scoring_score_event_read_model", None)
        if read_model is None:
            raise HTTPException(
                status_code=503,
                detail="Kết nối chỉ đọc cho lịch sử điểm chấm chưa sẵn sàng.",
            )
        bot_decision_id = await cast(
            ScorerScoreEventReadModel, read_model
        ).get_blockable_decision_id(
            identity.principal.workspace_id,
            score_event_id=score_event_id,
        )
        if bot_decision_id is None:
            raise HTTPException(
                status_code=409,
                detail="Không tìm thấy sự kiện APPROVE/PAPER_GATE đã liên kết quyết định.",
            )
        event = await facade.block(
            bot_decision_id=bot_decision_id,
            reason=body.reason,
            idempotency_key=idempotency_key,
        )
    except HTTPException as error:
        raise _no_store(error) from error
    except (
        PermissionError,
        LookupError,
        ValueError,
        LearningPaperEntryBlockConflict,
        SQLAlchemyError,
    ) as error:
        raise _no_store(_mapped_error(error)) from error
    return {
        "status": "blocked",
        "score_event_id": str(event.score_event_id),
        "bot_decision_id": str(event.bot_decision_id),
    }


__all__ = ["PaperEntryBlockRequest", "router"]
