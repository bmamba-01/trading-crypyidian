"""Trang tiếng Việt và API tường minh cho chẩn đoán PAPER-only."""

from __future__ import annotations

from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.exc import SQLAlchemyError

from trading_bot.adapters.database.repositories.learning_paper_performance import (
    PaperPerformanceIdempotencyConflict,
)
from trading_bot.admin.learning_paper_performance_schemas import (
    PaperPerformanceAssessRequest,
    PaperPerformanceReportResponse,
)
from trading_bot.admin.web_router import _admin_service, _auth_service, _templates
from trading_bot.application.learning_paper_performance import (
    LearningPaperPerformanceService,
    PaperPerformanceReportSummary,
)
from trading_bot.auth.cookies import read_session_cookie
from trading_bot.auth.policy import Permission, authorize
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity

router = APIRouter(tags=["learning PAPER performance"])


@router.get("/admin/learning/paper-performance")
async def learning_paper_performance_page(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Render report summary theo quyền mô hình và grant từng bot."""
    response.headers["Cache-Control"] = "no-store"
    principal = identity.principal
    if not authorize(principal, Permission.LEARNING_MODEL_READ):
        raise HTTPException(status_code=403, detail="Không đủ quyền xem chẩn đoán PAPER.")
    service = _performance_service(request)
    try:
        reports = await service.for_principal(principal).list_recent()
        bots = await _admin_service(request).list_bots(principal)
        csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    except PermissionError as error:
        raise HTTPException(status_code=403, detail="Không đủ quyền xem báo cáo bot.") from error
    except AuthenticationFailure as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=error.message,
            headers={"Cache-Control": "no-store"},
        ) from error
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=503,
            detail="Chưa thể đọc báo cáo PAPER an toàn.",
            headers={"Cache-Control": "no-store"},
        ) from error
    bot_options = tuple(
        {"id": str(item["id"]), "name": str(item.get("name") or item["id"])}
        for item in bots
        if item.get("account_mode") == "PAPER"
    )
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/learning_paper_performance.html",
        context={
            "identity": identity,
            "reports": tuple(_present_report(item) for item in reports),
            "bot_options": bot_options,
            "csrf_token": csrf_token,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/api/v1/learning/paper-performance/assess",
    response_model=PaperPerformanceReportResponse,
)
async def assess_learning_paper_performance(
    body: PaperPerformanceAssessRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> PaperPerformanceReportResponse:
    """Ghi một snapshot diagnostic; không đổi trạng thái bot hoặc scorer."""
    response.headers["Cache-Control"] = "no-store"
    if not authorize(identity.principal, Permission.LEARNING_MODEL_READ):
        raise HTTPException(
            status_code=403,
            detail="Không đủ quyền tạo chẩn đoán PAPER.",
            headers={"Cache-Control": "no-store"},
        )
    try:
        summary = (
            await _performance_service(request)
            .for_principal(identity.principal)
            .assess(
                bot_id=body.bot_id,
                idempotency_key=idempotency_key,
            )
        )
    except PaperPerformanceIdempotencyConflict as error:
        raise _http_error(409, "Idempotency-Key đã gắn với báo cáo khác.") from error
    except PermissionError as error:
        raise _http_error(403, "Không đủ quyền xem hoặc đánh giá bot này.") from error
    except LookupError as error:
        raise _http_error(409, "Chưa có scorer target hiện hành đủ căn cứ để đánh giá.") from error
    except SQLAlchemyError as error:
        raise _http_error(
            409,
            "Target, quyền dữ liệu hoặc baseline đã đổi; chưa ghi báo cáo mới.",
        ) from error
    except (RuntimeError, ValueError) as error:
        raise _http_error(409, "Chưa thể tạo báo cáo PAPER an toàn.") from error
    return _response(summary)


def _performance_service(request: Request) -> LearningPaperPerformanceService:
    service = cast(
        LearningPaperPerformanceService | None,
        getattr(request.app.state, "learning_paper_performance_service", None),
    )
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Dịch vụ chẩn đoán PAPER chưa sẵn sàng.",
            headers={"Cache-Control": "no-store"},
        )
    return service


def _http_error(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail, headers={"Cache-Control": "no-store"})


def _response(summary: PaperPerformanceReportSummary) -> PaperPerformanceReportResponse:
    return PaperPerformanceReportResponse(
        report_id=summary.report_id,
        bot_id=summary.bot_id,
        as_of=summary.as_of,
        status=summary.status.value,
        policy_version=summary.policy_version,
        policy_hash=summary.policy_hash,
        target_fingerprint=summary.target_fingerprint,
        baseline_report_hash=summary.baseline_report_hash,
        report_hash=summary.report_hash,
        report=summary.report,
    )


def _present_report(summary: PaperPerformanceReportSummary) -> dict[str, Any]:
    report = summary.report
    return {
        "id": summary.report_id,
        "bot_id": summary.bot_id,
        "as_of": summary.as_of,
        "status": summary.status.value,
        "status_label": {
            "healthy": "Chưa vượt ngưỡng suy giảm",
            "degraded": "Cần xem xét hai chỉ số cùng suy giảm",
            "inconclusive": "Chưa đủ bằng chứng",
        }[summary.status.value],
        "policy_version": summary.policy_version,
        "policy_hash": summary.policy_hash,
        "target_fingerprint": summary.target_fingerprint,
        "baseline_report_hash": summary.baseline_report_hash,
        "report_hash": summary.report_hash,
        "selection_bias_note": report["selection_bias_note"],
        "cohorts": tuple(report["cohorts"]),
    }


__all__ = ["router"]
