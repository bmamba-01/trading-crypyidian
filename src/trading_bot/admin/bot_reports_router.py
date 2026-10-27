"""HTTP API báo cáo MB-06: một endpoint GET, scope bắt buộc khai báo tường minh.

``scope=bot`` cần ``bot_id``; ``scope=workspace`` không nhận ``bot_id``. Thiếu scope bị từ chối
thay vì mặc định gộp bot. Workspace lấy từ phiên, quyền kiểm trong ``BotReportingService``.
"""

from datetime import datetime
from typing import Annotated, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from trading_bot.application.bot_reporting import (
    BotReportError,
    BotReportingService,
    BotReportQuery,
    ReportScope,
)
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.domain.portfolio.reporting import (
    REPORT_MODE,
    REPORT_VERSION,
    BotReport,
    WorkspaceAggregateReport,
)

router = APIRouter(prefix="/api/v1/reports", tags=["bot reports"])


def get_bot_reporting_service(request: Request) -> BotReportingService:
    service = cast(
        BotReportingService | None, getattr(request.app.state, "bot_reporting_service", None)
    )
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ báo cáo bot chưa sẵn sàng")
    return service


def _reject(response: Response, status_code: int, code: str, message: str) -> NoReturn:
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
        headers={"Cache-Control": response.headers.get("Cache-Control", "no-store")},
    )


@router.get("")
async def get_report(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    start: Annotated[datetime, Query(description="Mốc đầu (gồm), ISO-8601 có timezone")],
    end: Annotated[datetime, Query(description="Mốc cuối (không gồm), ISO-8601 có timezone")],
    scope: Annotated[str | None, Query(description="bot | workspace (bắt buộc)")] = None,
    bot_id: Annotated[UUID | None, Query()] = None,
    mode: Annotated[str, Query()] = REPORT_MODE,
    report_version: Annotated[str, Query()] = REPORT_VERSION,
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    if scope is None:
        _reject(
            response,
            422,
            "REPORT_SCOPE_REQUIRED",
            "Cần scope=bot (kèm bot_id) hoặc scope=workspace; không tự gộp bot.",
        )
    if scope not in {item.value for item in ReportScope}:
        _reject(response, 422, "REPORT_SCOPE_INVALID", "scope chỉ nhận bot hoặc workspace.")
    report_scope = ReportScope(scope)
    if report_scope is ReportScope.BOT and bot_id is None:
        _reject(response, 422, "BOT_ID_REQUIRED", "scope=bot cần bot_id.")
    if report_scope is ReportScope.WORKSPACE and bot_id is not None:
        _reject(response, 422, "BOT_ID_NOT_ALLOWED", "scope=workspace không nhận bot_id.")
    service = get_bot_reporting_service(request)
    principal = identity.principal
    query = BotReportQuery(
        workspace_id=principal.workspace_id,
        bot_id=bot_id,
        start=start,
        end=end,
        mode=mode,
        report_version=report_version,
        scope=report_scope,
    )
    report: BotReport | WorkspaceAggregateReport
    try:
        if report_scope is ReportScope.WORKSPACE:
            report = await service.get_workspace_report(query, principal)
        else:
            report = await service.get_bot_report(query, principal)
    except BotReportError as error:
        _reject(response, error.status_code, error.code, error.message)
    return {"report": report.as_dict()}
