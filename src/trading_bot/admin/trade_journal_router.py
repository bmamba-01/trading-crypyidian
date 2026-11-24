"""HTTP API & form POST cho nhật ký giao dịch, checklist, tag setup và mistake attribution."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, NoReturn, cast
from urllib.parse import parse_qs, urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from trading_bot.application.bot_reporting import (
    BotReportError,
    BotReportingService,
    BotReportQuery,
    ReportScope,
)
from trading_bot.application.trade_journal import TradeJournalError, TradeJournalService
from trading_bot.auth.cookies import read_session_cookie
from trading_bot.auth.policy import Permission, authorize
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity, AuthService
from trading_bot.domain.portfolio.trade_journal import (
    DEFAULT_JOURNAL_RETENTION_DAYS,
    JournalPrivacyMode,
)

router = APIRouter(prefix="/api/v1/trade-journal", tags=["trade journal"])
html_router = APIRouter(tags=["admin web"])


class ChecklistItemInput(BaseModel):
    id: str | None = None
    label_vi: str
    checked: bool = False


class SaveTradeJournalRequest(BaseModel):
    bot_id: UUID
    note_vi: str = ""
    checklist_items: list[ChecklistItemInput] = Field(default_factory=list)
    setup_tags: list[str] = Field(default_factory=list)
    mistake_tags: list[str] = Field(default_factory=list)
    privacy_mode: str = JournalPrivacyMode.WORKSPACE_TEAM.value
    retention_days: int = DEFAULT_JOURNAL_RETENTION_DAYS


class AddAttachmentRequest(BaseModel):
    bot_id: UUID
    file_name: str
    content_type: str
    byte_size: int
    sha256: str
    storage_ref: str
    exif_stripped: bool = True


def get_trade_journal_service(request: Request) -> TradeJournalService:
    service = cast(
        TradeJournalService | None,
        getattr(request.app.state, "trade_journal_service", None),
    )
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ nhật ký giao dịch chưa sẵn sàng.")
    return service


def _reject(response: Response, error: TradeJournalError) -> NoReturn:
    raise HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
        headers={"Cache-Control": response.headers.get("Cache-Control", "no-store")},
    )


@router.get("/mistake-attribution")
async def get_mistake_attribution(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    bot_id: Annotated[UUID, Query()],
    start: Annotated[datetime, Query()],
    end: Annotated[datetime, Query()],
) -> dict[str, Any]:
    """Tổng hợp chi phí lỗi theo mistake_tags và hiệu suất tag setup trên báo cáo bot."""
    response.headers["Cache-Control"] = "no-store"
    reporting = cast(
        BotReportingService | None,
        getattr(request.app.state, "bot_reporting_service", None),
    )
    if reporting is None:
        raise HTTPException(status_code=503, detail="Dịch vụ báo cáo bot chưa sẵn sàng.")
    journal = get_trade_journal_service(request)
    try:
        report = await reporting.get_bot_report(
            BotReportQuery(
                workspace_id=identity.principal.workspace_id,
                scope=ReportScope.BOT,
                bot_id=bot_id,
                start=start,
                end=end,
            ),
            identity.principal,
        )
        trades = report.as_dict().get("trades", [])
        summary = await journal.build_attribution_summary(
            identity.principal, trades=trades, bot_id=bot_id
        )
    except BotReportError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error
    except TradeJournalError as error:
        _reject(response, error)
    return {
        "bot_id": str(bot_id),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        **summary,
    }


@router.get("/{trade_id}")
async def get_trade_journal_entry(
    trade_id: UUID,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    bot_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any]:
    """Đọc ghi chú, checklist, tags, ảnh đính kèm và audit của một giao dịch."""
    response.headers["Cache-Control"] = "no-store"
    service = get_trade_journal_service(request)
    try:
        entry = await service.get_trade_journal(
            identity.principal, trade_id=trade_id, bot_id=bot_id
        )
    except TradeJournalError as error:
        _reject(response, error)
    return {"entry": entry}


@router.put("/{trade_id}")
async def put_trade_journal_entry(
    trade_id: UUID,
    payload: SaveTradeJournalRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, Any]:
    """Tạo hoặc cập nhật nhật ký giao dịch, checklist, setup_tags và mistake_tags."""
    response.headers["Cache-Control"] = "no-store"
    service = get_trade_journal_service(request)
    try:
        entry = await service.save_trade_journal(
            identity.principal,
            bot_id=payload.bot_id,
            trade_id=trade_id,
            note_vi=payload.note_vi,
            checklist_items=[item.model_dump() for item in payload.checklist_items],
            setup_tags=payload.setup_tags,
            mistake_tags=payload.mistake_tags,
            privacy_mode=payload.privacy_mode,
            retention_days=payload.retention_days,
        )
    except TradeJournalError as error:
        _reject(response, error)
    return {"entry": entry}


@router.post("/{trade_id}/attachments", status_code=201)
async def post_trade_journal_attachment(
    trade_id: UUID,
    payload: AddAttachmentRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, Any]:
    """Đính kèm metadata ảnh chụp (đã kiểm tra type/size/sha256/EXIF) vào nhật ký."""
    response.headers["Cache-Control"] = "no-store"
    service = get_trade_journal_service(request)
    try:
        attachment = await service.add_attachment(
            identity.principal,
            bot_id=payload.bot_id,
            trade_id=trade_id,
            file_name=payload.file_name,
            content_type=payload.content_type,
            byte_size=payload.byte_size,
            sha256=payload.sha256,
            storage_ref=payload.storage_ref,
            exif_stripped=payload.exif_stripped,
        )
    except TradeJournalError as error:
        _reject(response, error)
    return {"attachment": attachment}


@router.delete("/{trade_id}")
async def delete_trade_journal_entry(
    trade_id: UUID,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    bot_id: Annotated[UUID, Query()],
) -> dict[str, Any]:
    """Xóa mềm nhật ký giao dịch và ghi nhận sự kiện audit."""
    response.headers["Cache-Control"] = "no-store"
    service = get_trade_journal_service(request)
    try:
        deleted = await service.delete_trade_journal(
            identity.principal, bot_id=bot_id, trade_id=trade_id
        )
    except TradeJournalError as error:
        _reject(response, error)
    return {"deleted": deleted}


@router.post("/retention/purge")
async def purge_expired_trade_journals(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, Any]:
    """Dọn dẹp các nhật ký đã hết hạn retention (chỉ OWNER / quyền WORKSPACE_KILL)."""
    response.headers["Cache-Control"] = "no-store"
    service = get_trade_journal_service(request)
    try:
        purged = await service.purge_expired_journals(identity.principal)
    except TradeJournalError as error:
        _reject(response, error)
    return {"purged_count": purged}


@html_router.post("/admin/reports/journal")
async def save_report_journal_form(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> RedirectResponse:
    """Lưu nhật ký giao dịch từ form HTML trên trang /admin/reports."""
    try:
        authorize(identity.principal, Permission.TRADE_REVIEW, raise_error=True)
    except PermissionError as error:
        raise HTTPException(
            status_code=403,
            detail="Tài khoản thiếu quyền trade:review để sửa nhật ký giao dịch.",
        ) from error
    raw_body = (await request.body()).decode("utf-8")
    parsed = parse_qs(raw_body, keep_blank_values=True)
    form: dict[str, str] = {key: values[0] if values else "" for key, values in parsed.items()}
    csrf_token = str(form.get("csrf_token") or "")
    auth_service = cast(AuthService, request.app.state.auth_service)
    try:
        await auth_service.verify_csrf(read_session_cookie(request), csrf_token)
    except AuthenticationFailure as error:
        raise HTTPException(status_code=403, detail="CSRF token không hợp lệ.") from error

    try:
        bot_id = UUID(str(form.get("bot_id") or ""))
        trade_id = UUID(str(form.get("trade_id") or ""))
    except ValueError as error:
        raise HTTPException(status_code=422, detail="bot_id/trade_id không hợp lệ.") from error

    setup_tags = [t.strip() for t in str(form.get("setup_tags") or "").split(",") if t.strip()]
    mistake_tags = [t.strip() for t in str(form.get("mistake_tags") or "").split(",") if t.strip()]
    checklist_items: list[dict[str, Any]] = []
    for raw_line in str(form.get("checklist_lines") or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        checked = line.startswith("[x]") or line.startswith("[X]")
        label = line[3:].strip() if (checked or line.startswith("[ ]")) else line
        if label:
            checklist_items.append({"label_vi": label, "checked": checked})

    retention_raw = str(form.get("retention_days") or "").strip()
    retention_days = (
        int(retention_raw) if retention_raw.isdigit() else DEFAULT_JOURNAL_RETENTION_DAYS
    )
    privacy_mode = str(form.get("privacy_mode") or JournalPrivacyMode.WORKSPACE_TEAM.value)

    service = get_trade_journal_service(request)
    try:
        await service.save_trade_journal(
            identity.principal,
            bot_id=bot_id,
            trade_id=trade_id,
            note_vi=str(form.get("note_vi") or ""),
            checklist_items=checklist_items,
            setup_tags=setup_tags,
            mistake_tags=mistake_tags,
            privacy_mode=privacy_mode,
            retention_days=retention_days,
        )
    except TradeJournalError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error

    params: dict[str, str] = {
        "scope": "bot",
        "bot_id": str(bot_id),
        "chart_trade_id": str(trade_id),
    }
    for key in ("start", "end", "chart_timeframe"):
        val = str(form.get(key) or "").strip()
        if val:
            params[key] = val
    return RedirectResponse(url=f"/admin/reports?{urlencode(params)}", status_code=303)
