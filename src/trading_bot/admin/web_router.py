"""Các trang HTML quản trị; dữ liệu và quyền vẫn do service/backend quyết định."""

import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any, cast
from urllib.parse import parse_qs
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError

from trading_bot.admin.bot_schemas import CreateBotRequest
from trading_bot.admin.bot_service import BotAdminError, BotConfigAdminService
from trading_bot.admin.strategy_card_service import StrategyCardAdminService
from trading_bot.application.bot_lifecycle_guard import BotNotFlatError
from trading_bot.application.bot_operations import BotOperationsService, PrincipalBotOperations
from trading_bot.application.bot_reporting import (
    BotReportError,
    BotReportingService,
    BotReportQuery,
    ReportScope,
)
from trading_bot.application.bot_runtime_control import BotRuntimeControlError
from trading_bot.application.flatten_all import FlattenAllError, FlattenAllService
from trading_bot.application.strategy_pipeline.microscope import MicroscopeReplayService
from trading_bot.auth.cookies import read_session_cookie, set_session_cookie
from trading_bot.auth.policy import Role
from trading_bot.auth.router import (
    authenticate_login,
    client_address,
    current_identity,
    get_rate_limiter,
)
from trading_bot.auth.schemas import LoginRequest
from trading_bot.auth.service import (
    SESSION_LIFETIME,
    AuthenticationFailure,
    AuthIdentity,
    AuthService,
)
from trading_bot.config.bot_settings import BotConfigOverlay
from trading_bot.domain.portfolio.reporting import (
    REPORT_MODE,
    REPORT_VERSION,
)
from trading_bot.domain.strategy_card.schema import compute_canonical_hash

try:
    VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:  # pragma: no cover
    VN_TZ = timezone(timedelta(hours=7))

router = APIRouter(prefix="/admin", tags=["admin web"])
LOGIN_CSRF_COOKIE = "crypidian_login_csrf"
LOGIN_CSRF_HOST_COOKIE = "__Host-crypidian_login_csrf"


@dataclass(frozen=True, slots=True)
class AdminBotView:
    """Thông tin bot an toàn để render, không chuyển ORM vào template."""

    id: str
    name: str
    status: str
    account_mode: str
    account_status: str
    readiness: str
    entry_state: str
    entry_allowed: bool
    control_revision: int


def _admin_service(request: Request) -> BotConfigAdminService:
    service = cast(
        BotConfigAdminService | None, getattr(request.app.state, "bot_admin_service", None)
    )
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ quản trị bot chưa sẵn sàng")
    return service


def _operations(request: Request, identity: AuthIdentity) -> PrincipalBotOperations:
    service = cast(
        BotOperationsService | None, getattr(request.app.state, "bot_operations_service", None)
    )
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ vận hành bot chưa sẵn sàng")
    return service.for_principal(identity.principal)


def _reporting_service(request: Request) -> BotReportingService:
    service = cast(
        BotReportingService | None,
        getattr(request.app.state, "bot_reporting_service", None),
    )
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ báo cáo chưa sẵn sàng")
    return service


def _flatten_service(request: Request) -> FlattenAllService:
    service = cast(
        FlattenAllService | None, getattr(request.app.state, "flatten_all_service", None)
    )
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ flatten chưa sẵn sàng")
    return service


def _telegram_binding_service(request: Request) -> Any:
    service = getattr(request.app.state, "telegram_binding_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ liên kết Telegram chưa sẵn sàng")
    return service


def _auth_service(request: Request) -> AuthService:
    service = cast(AuthService | None, getattr(request.app.state, "auth_service", None))
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ xác thực chưa sẵn sàng")
    return service


def _strategy_card_service(request: Request) -> StrategyCardAdminService:
    service = cast(
        StrategyCardAdminService | None,
        getattr(request.app.state, "strategy_card_service", None),
    )
    if service is None:
        service = StrategyCardAdminService()
        request.app.state.strategy_card_service = service
    return service


def _microscope_service(request: Request) -> MicroscopeReplayService:
    service = cast(
        MicroscopeReplayService | None,
        getattr(request.app.state, "microscope_replay_service", None),
    )
    if service is None:
        service = MicroscopeReplayService()
        request.app.state.microscope_replay_service = service
    return service


class InspectBarRequest(BaseModel):
    """Yêu cầu kiểm định nến tức thời gửi từ giao diện Microscope Replay."""

    ohlcv: dict[str, Any]
    preset_id: str | None = None
    context: dict[str, Any] | None = None


def _login_csrf_cookie_name(request: Request) -> str:
    settings = request.app.state.settings
    return LOGIN_CSRF_HOST_COOKIE if settings.app_env == "production" else LOGIN_CSRF_COOKIE


def _set_login_csrf_cookie(response: Response, request: Request, token: str) -> None:
    """Cookie host-only, HttpOnly và Strict ràng buộc token form trước khi có session."""
    response.set_cookie(
        _login_csrf_cookie_name(request),
        token,
        max_age=300,
        path="/",
        secure=request.app.state.settings.app_env == "production",
        httponly=True,
        samesite="strict",
    )


def _clear_login_csrf_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        _login_csrf_cookie_name(request),
        path="/",
        secure=request.app.state.settings.app_env == "production",
        httponly=True,
        samesite="strict",
    )


def _login_page(request: Request, *, error: str | None = None) -> Response:
    token = secrets.token_urlsafe(32)
    templates = cast(Jinja2Templates, request.app.state.templates)
    page = templates.TemplateResponse(
        request=request,
        name="admin/login.html",
        context={"identity": None, "csrf_token": token, "error": error},
        status_code=401 if error else 200,
    )
    page.headers["Cache-Control"] = "no-store"
    _set_login_csrf_cookie(page, request, token)
    return page


@router.get("/login")
async def login_page(request: Request) -> Response:
    """Hiển thị form đăng nhập không lưu cache và cấp token chống login-CSRF."""
    return _login_page(request)


@router.post("/login")
async def submit_login(request: Request) -> Response:
    """Xử lý form HTML qua cùng service MFA/throttle với endpoint đăng nhập API."""
    try:
        values = await _parse_form(request)
    except HTTPException:
        raise
    cookie_token = request.cookies.get(_login_csrf_cookie_name(request), "")
    form_token = values.pop("csrf_token", "")
    if not cookie_token or not form_token or not hmac.compare_digest(cookie_token, form_token):
        raise HTTPException(status_code=403, detail="Biểu mẫu hết hạn hoặc không hợp lệ.")
    try:
        credentials = LoginRequest.model_validate(values)
    except ValidationError:
        return _login_page(request, error="Thông tin đăng nhập không hợp lệ.")
    limiter = get_rate_limiter(request)
    service = _auth_service(request)
    try:
        result = await authenticate_login(
            workspace_slug=credentials.workspace_slug,
            email=credentials.email,
            password=credentials.password,
            code=credentials.code,
            address=client_address(request),
            service=service,
            limiter=limiter,
        )
    except HTTPException as error:
        if error.status_code >= 500:
            raise
        if error.status_code == 429:
            message = "Thử lại sau khi hết thời gian giới hạn."
        else:
            message = "Thông tin đăng nhập không hợp lệ."
        return _login_page(request, error=message)
    settings = request.app.state.settings
    response = Response(
        status_code=303,
        headers={"Location": "/admin", "Cache-Control": "no-store"},
    )
    set_session_cookie(
        response,
        settings,
        result.cookie_value,
        max_age=int(SESSION_LIFETIME.total_seconds()),
    )
    _clear_login_csrf_cookie(response, request)
    return response


async def _parse_form(request: Request) -> dict[str, str]:
    """Đọc form urlencoded giới hạn kích thước, từ chối field trùng để tránh nhập nhằng."""
    if (
        request.headers.get("content-type", "").split(";", 1)[0]
        != "application/x-www-form-urlencoded"
    ):
        raise HTTPException(status_code=415, detail="Chỉ nhận form urlencoded.")
    raw = await request.body()
    if len(raw) > 16_384:
        raise HTTPException(status_code=413, detail="Form quá lớn.")
    try:
        values = parse_qs(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
    except (UnicodeDecodeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Form không hợp lệ.") from error
    if any(len(items) != 1 for items in values.values()):
        raise HTTPException(status_code=422, detail="Form có trường lặp.")
    return {key: items[0] for key, items in values.items()}


async def _verify_form_csrf(request: Request, values: dict[str, str]) -> None:
    try:
        await _auth_service(request).verify_csrf(
            read_session_cookie(request), values.get("csrf_token")
        )
    except AuthenticationFailure as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error


def _runtime_dict(row: Any) -> dict[str, Any]:
    return row.as_dict() if hasattr(row, "as_dict") else dict(row)


def _bot_view(bot: dict[str, Any], runtime: dict[str, Any] | None = None) -> AdminBotView:
    account = bot.get("account") or {}
    runtime = runtime or {}
    return AdminBotView(
        id=str(bot["id"]),
        name=str(bot["name"]),
        status=str(bot["status"]),
        account_mode=str(account.get("mode", "—")),
        account_status=str(account.get("status", "—")),
        readiness=str(runtime.get("readiness") or "UNKNOWN"),
        entry_state=str(runtime.get("entry_state") or "UNKNOWN"),
        entry_allowed=bool(runtime.get("entry_allowed", False)),
        control_revision=int(runtime.get("control_revision", 0)),
    )


def _control_error(error: BotRuntimeControlError) -> HTTPException:
    status_by_code = {
        "BOT_NOT_FOUND": 404,
        "CONTROL_PERMISSION_DENIED": 403,
        "RECENT_MFA_REQUIRED": 403,
        "CONTROL_REASON_REQUIRED": 422,
        "CONTROL_REVISION_CONFLICT": 409,
        "CONTROL_REVISION_REQUIRED": 409,
        "BOT_NOT_FLAT": 409,
        "BOT_ENABLE_NOT_READY": 409,
        "BOT_ENABLE_ACCOUNT_STATE_INVALID": 409,
        "BOT_ENABLE_WORKSPACE_KILLED": 409,
        "BOT_ENABLE_RECONCILIATION_REQUIRED": 409,
        "BOT_ENABLE_OPEN_INCIDENT": 409,
        "BOT_ENABLE_CONFIG_REQUIRED": 409,
        "RUNTIME_CONTROL_UNAVAILABLE": 503,
    }
    return HTTPException(
        status_code=status_by_code.get(error.reason_code, 409),
        detail="Thao tác vận hành bị từ chối.",
    )


@router.get("")
async def dashboard(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Hiển thị trang tổng quan quản trị cho thành viên có phiên hợp lệ."""
    response.headers["Cache-Control"] = "no-store"
    principal = identity.principal
    try:
        bots = await _admin_service(request).list_bots(principal)
        operations = _operations(request, identity)
        runtime = [_runtime_dict(item) for item in await operations.list_bots()]
        workspace = _runtime_dict(await operations.workspace_status())
        csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    except BotAdminError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    runtime_by_id = {str(item["id"]): item for item in runtime}
    bot_views = [_bot_view(bot, runtime_by_id.get(str(bot["id"]))) for bot in bots]
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request=request,
        name="admin/dashboard.html",
        context={
            "identity": identity,
            "bots": bot_views,
            "bot_count": len(bot_views),
            "ready_count": sum(bot.readiness == "READY" for bot in bot_views),
            "workspace": workspace,
            "csrf_token": csrf_token,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/dashboard/realtime")
async def dashboard_realtime(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, Any]:
    """Cung cấp dữ liệu cập nhật thời gian thực định kỳ (5s) cho Admin Dashboard."""
    response.headers["Cache-Control"] = "no-store"
    now_utc = datetime.now(UTC)
    now_vn = now_utc.astimezone(VN_TZ)

    principal = identity.principal
    try:
        bots = await _admin_service(request).list_bots(principal)
        operations = _operations(request, identity)
        runtime = [_runtime_dict(item) for item in await operations.list_bots()]
        workspace = _runtime_dict(await operations.workspace_status())
    except BotAdminError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error

    runtime_by_id = {str(item["id"]): item for item in runtime}
    bot_views = [_bot_view(bot, runtime_by_id.get(str(bot["id"]))) for bot in bots]

    # Thu thập tóm tắt hiệu suất nếu service báo cáo khả dụng
    metrics: dict[str, Any] = {}
    reporting_service = getattr(request.app.state, "bot_reporting_service", None)
    if reporting_service is not None:
        try:
            query = BotReportQuery(
                workspace_id=principal.workspace_id,
                bot_id=None,
                start=now_utc - timedelta(days=30),
                end=now_utc,
                mode=REPORT_MODE,
                report_version=REPORT_VERSION,
                scope=ReportScope.WORKSPACE,
            )
            report = await reporting_service.get_workspace_report(query, principal)
            if hasattr(report, "totals") and isinstance(report.totals, dict):
                total_closed = 0
                total_won = 0
                net_pnl = Decimal("0")
                for item in report.totals.values():
                    total_closed += getattr(item, "closed_trade_count", 0)
                    total_won += getattr(item, "winning_trade_count", 0)
                    net_pnl += getattr(item, "net_pnl", Decimal("0"))
                win_rate = (
                    round(float(total_won / total_closed * 100), 2) if total_closed > 0 else 0.0
                )
                metrics = {
                    "total_closed_trades": total_closed,
                    "net_pnl": str(net_pnl),
                    "win_rate": win_rate,
                }
            elif hasattr(report, "as_dict"):
                report_dict = report.as_dict()
                if "totals" in report_dict and isinstance(report_dict["totals"], dict):
                    total_closed = 0
                    total_won = 0
                    net_pnl = Decimal("0")
                    for item in report_dict["totals"].values():
                        total_closed += int(item.get("closed_trade_count", 0))
                        total_won += int(item.get("winning_trade_count", 0))
                        net_pnl += Decimal(str(item.get("net_pnl", "0")))
                    win_rate = (
                        round(float(total_won / total_closed * 100), 2) if total_closed > 0 else 0.0
                    )
                    metrics = {
                        "total_closed_trades": total_closed,
                        "net_pnl": str(net_pnl),
                        "win_rate": win_rate,
                    }
        except Exception:
            metrics = {}

    return {
        "server_time": {
            "iso": now_utc.isoformat(),
            "vn": now_vn.strftime("%d/%m/%Y %H:%M:%S"),
            "vn_time": now_vn.strftime("%H:%M:%S"),
        },
        "workspace": workspace,
        "bot_count": len(bot_views),
        "ready_count": sum(bot.readiness == "READY" for bot in bot_views),
        "bots": [
            {
                "id": b.id,
                "name": b.name,
                "status": b.status,
                "account_mode": b.account_mode,
                "readiness": b.readiness,
                "entry_allowed": b.entry_allowed,
                "control_revision": b.control_revision,
            }
            for b in bot_views
        ],
        "metrics": metrics,
    }


@router.get("/bots")
async def bot_list(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Danh sách bot đã được BotConfigAdminService lọc theo workspace/grant."""
    response.headers["Cache-Control"] = "no-store"
    try:
        bots = await _admin_service(request).list_bots(identity.principal)
        runtime = [_runtime_dict(item) for item in await _operations(request, identity).list_bots()]
    except BotAdminError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    runtime_by_id = {str(item["id"]): item for item in runtime}
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request=request,
        name="admin/bots.html",
        context={
            "identity": identity,
            "bots": [_bot_view(bot, runtime_by_id.get(str(bot["id"]))) for bot in bots],
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/bots/new")
async def create_bot_page(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Form tạo account PAPER; quyền owner/MFA vẫn được kiểm lại bởi application service."""
    if Role.OWNER not in identity.principal.roles:
        raise HTTPException(status_code=403, detail="Chỉ owner được tạo bot.")
    try:
        csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    except AuthenticationFailure as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request=request,
        name="admin/bot_create.html",
        context={"identity": identity, "csrf_token": csrf_token},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/bots/new")
async def create_bot_form(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Xử lý form tạo bot bằng service hiện có; CSRF được xác minh trước mutation."""
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    try:
        body = CreateBotRequest.model_validate(
            {"name": values.get("name"), "max_notional": values.get("max_notional")}
        )
    except Exception as error:
        raise HTTPException(status_code=422, detail="Thông tin tạo bot không hợp lệ.") from error
    try:
        bot, _created = await _admin_service(request).create_bot(
            identity.principal,
            name=body.name,
            max_notional=Decimal(body.max_notional),
        )
    except BotAdminError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    return Response(status_code=303, headers={"Location": f"/admin/bots/{bot['id']}"})


@router.get("/bots/{bot_id}")
async def bot_detail(
    bot_id: UUID,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    draft_validation: str | None = None,
) -> Response:
    """Chi tiết bot; service dùng cùng lỗi 404 cho bot không tồn tại/ngoài grant."""
    response.headers["Cache-Control"] = "no-store"
    try:
        bot = await _admin_service(request).get_bot(identity.principal, bot_id=bot_id)
        runtime = _runtime_dict(await _operations(request, identity).bot_status(bot_id))
        csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    except BotAdminError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    except BotRuntimeControlError as error:
        if error.reason_code == "BOT_NOT_FOUND":
            raise HTTPException(status_code=404, detail="Không tìm thấy bot.") from error
        raise HTTPException(status_code=503, detail="Không thể tải trạng thái bot.") from error
    except AuthenticationFailure as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    templates = cast(Jinja2Templates, request.app.state.templates)
    versions = bot.get("versions", [])
    published = next((item for item in versions if item.get("status") == "PUBLISHED"), None)
    draft = next((item for item in versions if item.get("status") == "DRAFT"), None)
    return templates.TemplateResponse(
        request=request,
        name="admin/bot_detail.html",
        context={
            "identity": identity,
            "bot": _bot_view(bot, runtime),
            "csrf_token": csrf_token,
            "config_parent_version_id": published.get("id", "") if published else "",
            "config_draft": draft,
            "config_draft_json": json.dumps(draft.get("overlay", {}), ensure_ascii=False, indent=2)
            if draft
            else '{\n  "schema_version": 1\n}',
            "draft_validation": draft_validation,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/bots/{bot_id}/config-drafts")
async def create_config_draft_form(
    bot_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    try:
        overlay = BotConfigOverlay.model_validate(json.loads(values.get("overlay_json", "")))
        parent_id_value = values.get("expected_parent_version_id", "").strip()
        parent_id = UUID(parent_id_value) if parent_id_value else None
    except (ValueError, TypeError) as error:
        raise HTTPException(
            status_code=422, detail="Overlay hoặc version cha không hợp lệ."
        ) from error
    try:
        await _admin_service(request).create_draft(
            identity.principal,
            bot_id=bot_id,
            overlay=overlay,
            expected_parent_version_id=parent_id,
        )
    except BotAdminError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    return Response(status_code=303, headers={"Location": f"/admin/bots/{bot_id}"})


@router.post("/bots/{bot_id}/config-drafts/{draft_id}/validate")
async def validate_config_draft_form(
    bot_id: UUID,
    draft_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    try:
        result = await _admin_service(request).validate_draft(
            identity.principal, bot_id=bot_id, draft_id=draft_id
        )
    except BotAdminError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    validation = "valid" if result.get("valid") else "invalid"
    return Response(
        status_code=303,
        headers={"Location": f"/admin/bots/{bot_id}?draft_validation={validation}"},
    )


@router.post("/bots/{bot_id}/config-drafts/{draft_id}/publish")
async def publish_config_draft_form(
    bot_id: UUID,
    draft_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    try:
        expected_revision = int(values.get("expected_revision", ""))
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Cần revision bản nháp.") from error
    if expected_revision < 1:
        raise HTTPException(status_code=422, detail="Revision bản nháp không hợp lệ.")
    try:
        await _admin_service(request).publish(
            identity.principal,
            bot_id=bot_id,
            draft_id=draft_id,
            expected_revision=expected_revision,
        )
    except BotAdminError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    return Response(status_code=303, headers={"Location": f"/admin/bots/{bot_id}"})


@router.post("/bots/{bot_id}/pause")
async def pause_bot_form(
    bot_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    reason = values.get("reason", "").strip()
    if not 1 <= len(reason) <= 500:
        raise HTTPException(status_code=422, detail="Lý do cần từ 1 đến 500 ký tự.")
    try:
        await _operations(request, identity).pause_bot(bot_id, reason)
    except BotRuntimeControlError as error:
        raise _control_error(error) from error
    return Response(status_code=303, headers={"Location": f"/admin/bots/{bot_id}"})


@router.post("/bots/{bot_id}/resume")
async def resume_bot_form(
    bot_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    reason = values.get("reason", "").strip()
    try:
        expected_revision = int(values.get("expected_revision", ""))
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Cần revision trạng thái hiện hành.") from error
    if not 1 <= len(reason) <= 500 or expected_revision < 1:
        raise HTTPException(status_code=422, detail="Lý do hoặc revision không hợp lệ.")
    try:
        await _operations(request, identity).resume_bot(bot_id, reason, expected_revision)
    except BotRuntimeControlError as error:
        raise _control_error(error) from error
    return Response(status_code=303, headers={"Location": f"/admin/bots/{bot_id}"})


@router.post("/bots/{bot_id}/disable")
async def disable_bot_form(
    bot_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    reason = values.get("reason", "").strip()
    if not 1 <= len(reason) <= 500:
        raise HTTPException(status_code=422, detail="Lý do cần từ 1 đến 500 ký tự.")
    try:
        await _operations(request, identity).disable_bot(bot_id, reason)
    except (BotRuntimeControlError, BotNotFlatError) as error:
        if isinstance(error, BotRuntimeControlError):
            raise _control_error(error) from error
        raise HTTPException(status_code=409, detail="Bot chưa phẳng để disable.") from error
    return Response(status_code=303, headers={"Location": f"/admin/bots/{bot_id}"})


@router.post("/bots/{bot_id}/enable")
async def enable_bot_form(
    bot_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    reason = values.get("reason", "").strip()
    if not 1 <= len(reason) <= 500:
        raise HTTPException(status_code=422, detail="Lý do cần từ 1 đến 500 ký tự.")
    try:
        await _operations(request, identity).enable_bot(bot_id, reason)
    except BotRuntimeControlError as error:
        raise _control_error(error) from error
    return Response(status_code=303, headers={"Location": f"/admin/bots/{bot_id}"})


@router.post("/workspace/kill")
async def kill_workspace_form(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    reason = values.get("reason", "").strip()
    if not 1 <= len(reason) <= 500:
        raise HTTPException(status_code=422, detail="Lý do cần từ 1 đến 500 ký tự.")
    try:
        await _operations(request, identity).kill_workspace(reason)
    except BotRuntimeControlError as error:
        raise _control_error(error) from error
    return Response(status_code=303, headers={"Location": "/admin"})


@router.post("/workspace/release")
async def release_workspace_form(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    reason = values.get("reason", "").strip()
    try:
        expected_revision = int(values.get("expected_revision", ""))
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Cần revision workspace hiện hành.") from error
    if not 1 <= len(reason) <= 500 or expected_revision < 1:
        raise HTTPException(status_code=422, detail="Lý do hoặc revision không hợp lệ.")
    try:
        await _operations(request, identity).release_workspace(reason, expected_revision)
    except BotRuntimeControlError as error:
        raise _control_error(error) from error
    return Response(status_code=303, headers={"Location": "/admin"})


@router.get("/flatten")
async def flatten_page(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Hiển thị form preview PAPER; service sẽ xác minh owner và MFA gần đây."""
    if Role.OWNER not in identity.principal.roles:
        raise HTTPException(status_code=403, detail="Chỉ owner được flatten workspace.")
    try:
        csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    except AuthenticationFailure as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request=request,
        name="admin/flatten.html",
        context={"identity": identity, "csrf_token": csrf_token},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/flatten/preview")
async def flatten_preview_form(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    reason = values.get("reason", "").strip()
    try:
        preview = (
            await _flatten_service(request).for_principal(identity.principal).create_preview(reason)
        )
        csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    except FlattenAllError as error:
        status = (
            403
            if error.reason_code in {"CONTROL_PERMISSION_DENIED", "RECENT_MFA_REQUIRED"}
            else 503
        )
        raise HTTPException(
            status_code=status, detail="Không thể tạo preview flatten-all."
        ) from error
    except AuthenticationFailure as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request=request,
        name="admin/flatten_preview.html",
        context={
            "identity": identity,
            "preview": preview,
            "reason": reason,
            "idempotency_key": uuid4().hex,
            "csrf_token": csrf_token,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/flatten/confirm")
async def flatten_confirm_form(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    try:
        result = (
            await _flatten_service(request)
            .for_principal(identity.principal)
            .confirm(
                preview_id=UUID(values.get("preview_id", "")),
                preview_hash=values.get("preview_hash", ""),
                idempotency_key=values.get("idempotency_key", ""),
                reason=values.get("reason", ""),
            )
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Preview flatten-all không hợp lệ.") from error
    except FlattenAllError as error:
        status = {
            "CONTROL_PERMISSION_DENIED": 403,
            "RECENT_MFA_REQUIRED": 403,
            "FLATTEN_PREVIEW_STALE": 409,
            "FLATTEN_PREVIEW_EXPIRED": 409,
            "FLATTEN_IDEMPOTENCY_KEY_REUSED": 409,
        }.get(error.reason_code, 422)
        raise HTTPException(
            status_code=status, detail="Xác nhận flatten-all bị từ chối."
        ) from error
    return Response(
        status_code=303, headers={"Location": f"/admin/flatten/operations/{result.operation_id}"}
    )


@router.get("/flatten/operations/{operation_id}")
async def flatten_operation_page(
    operation_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    if Role.OWNER not in identity.principal.roles:
        raise HTTPException(status_code=403, detail="Chỉ owner được xem operation flatten.")
    try:
        operation = (
            await _flatten_service(request)
            .for_principal(identity.principal)
            .get_operation(operation_id)
        )
    except FlattenAllError as error:
        status = 404 if error.reason_code == "FLATTEN_OPERATION_NOT_FOUND" else 503
        raise HTTPException(
            status_code=status, detail="Không tìm thấy operation flatten."
        ) from error
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request=request,
        name="admin/flatten_operation.html",
        context={"identity": identity, "operation": operation},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/telegram")
async def telegram_bindings_page(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Liệt kê binding đã xác minh, không đọc hoặc hiển thị Telegram user ID."""
    try:
        bindings = await _telegram_binding_service(request).list_bindings(identity.principal)
        csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    except ValueError as error:
        raise HTTPException(status_code=403, detail="Không đủ quyền quản lý Telegram.") from error
    except AuthenticationFailure as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request=request,
        name="admin/telegram_bindings.html",
        context={"identity": identity, "bindings": bindings, "csrf_token": csrf_token},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/telegram/link")
async def telegram_link_form(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    try:
        link = await _telegram_binding_service(request).create_link(
            identity.principal, reason=values.get("reason", "")
        )
        csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    except ValueError as error:
        status = 403 if "owner" in str(error).lower() or "mfa" in str(error).lower() else 422
        raise HTTPException(
            status_code=status, detail="Không thể tạo mã liên kết Telegram."
        ) from error
    except AuthenticationFailure as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request=request,
        name="admin/telegram_link_created.html",
        context={
            "identity": identity,
            "link_url": link.url,
            "expires_at": link.expires_at,
            "csrf_token": csrf_token,
        },
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.post("/telegram/revoke/{membership_id}")
async def telegram_revoke_form(
    membership_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    try:
        await _telegram_binding_service(request).revoke(
            identity.principal, membership_id, reason=values.get("reason", "")
        )
    except ValueError as error:
        status = 403 if "owner" in str(error).lower() or "mfa" in str(error).lower() else 422
        raise HTTPException(
            status_code=status, detail="Không thể thu hồi liên kết Telegram."
        ) from error
    return Response(status_code=303, headers={"Location": "/admin/telegram"})


@router.get("/reports")
async def reports(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    start: datetime | None = None,
    end: datetime | None = None,
    scope: str | None = None,
    bot_id: str | None = None,
) -> Response:
    """Báo cáo bot mặc định; chỉ tổng hợp workspace khi người dùng chọn scope rõ ràng."""
    response.headers["Cache-Control"] = "no-store"
    report: dict[str, Any] | None = None
    parsed_bot_id: UUID | None = None
    if bot_id and bot_id.strip():
        try:
            parsed_bot_id = UUID(bot_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="bot_id phải là UUID hợp lệ.") from error
    if any(value is not None for value in (start, end, scope)) or parsed_bot_id is not None:
        if start is None or end is None or scope is None:
            raise HTTPException(
                status_code=422, detail="Cần chọn scope, thời điểm bắt đầu và kết thúc."
            )
        try:
            report_scope = ReportScope(scope)
        except ValueError as error:
            raise HTTPException(
                status_code=422, detail="scope chỉ nhận bot hoặc workspace."
            ) from error
        query = BotReportQuery(
            workspace_id=identity.principal.workspace_id,
            bot_id=parsed_bot_id,
            start=start,
            end=end,
            scope=report_scope,
        )
        service = _reporting_service(request)
        try:
            if report_scope is ReportScope.WORKSPACE:
                report = (await service.get_workspace_report(query, identity.principal)).as_dict()
            else:
                report = (await service.get_bot_report(query, identity.principal)).as_dict()
        except BotReportError as error:
            raise HTTPException(
                status_code=error.status_code,
                detail={"code": error.code, "message": error.message},
            ) from error
    try:
        bots = await _admin_service(request).list_bots(identity.principal)
    except BotAdminError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request=request,
        name="admin/reports.html",
        context={
            "identity": identity,
            "bots": [_bot_view(bot) for bot in bots],
            "report": report,
            "start": start.isoformat() if start is not None else "",
            "end": end.isoformat() if end is not None else "",
            "scope": scope or "bot",
            "bot_id": str(parsed_bot_id or ""),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/strategy-cards")
async def strategy_cards_page(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Hiển thị danh sách Strategy Cards, các tầng L0-L6 và kết quả kiểm định."""
    response.headers["Cache-Control"] = "no-store"
    service = _strategy_card_service(request)
    cards = await service.list_strategy_cards(identity.principal)
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request=request,
        name="admin/strategy_cards.html",
        context={
            "identity": identity,
            "cards": cards,
            "total_cards": len(cards),
            "active_cards_count": sum(1 for c in cards if c.is_active),
            "verified_cards_count": sum(1 for c in cards if c.is_verified),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/strategy-cards")
async def strategy_cards_api(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, Any]:
    """Trả về JSON danh sách Strategy Cards có sẵn trong workspace."""
    response.headers["Cache-Control"] = "no-store"
    service = _strategy_card_service(request)
    cards = await service.list_strategy_cards(identity.principal)
    return {
        "status": "success",
        "total": len(cards),
        "cards": [c.to_dict() for c in cards],
    }


@router.get("/strategy-cards/{card_id}/microscope")
async def strategy_card_microscope_page(
    card_id: str,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Hiển thị không gian làm việc Soi kính hiển vi (Microscope Replay Workspace)."""
    response.headers["Cache-Control"] = "no-store"
    card_service = _strategy_card_service(request)
    microscope_service = _microscope_service(request)

    card = await card_service.get_domain_card(identity.principal, card_id)
    if card is None:
        raise HTTPException(
            status_code=404,
            detail=f"Không tìm thấy Strategy Card với mã định danh '{card_id}'",
        )

    presets = microscope_service.get_presets()
    initial_preset = presets[0] if presets else None
    initial_trace = None
    if initial_preset:
        initial_trace = microscope_service.inspect_bar(
            card=card,
            ohlcv=initial_preset.ohlcv,
            context=initial_preset.context,
        )

    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request=request,
        name="admin/microscope_replay.html",
        context={
            "identity": identity,
            "card": card,
            "card_hash": compute_canonical_hash(card),
            "presets": presets,
            "presets_data": [p.to_dict() for p in presets],
            "initial_preset": initial_preset,
            "initial_trace": initial_trace,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/strategy-cards/{card_id}/inspect-bar")
async def inspect_bar_api(
    card_id: str,
    payload: InspectBarRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, Any]:
    """API thực thi soi vết nến tức thời qua 7 tầng L0->L6 trả về MicroscopeTrace."""
    response.headers["Cache-Control"] = "no-store"
    card_service = _strategy_card_service(request)
    microscope_service = _microscope_service(request)

    card = await card_service.get_domain_card(identity.principal, card_id)
    if card is None:
        raise HTTPException(
            status_code=404,
            detail=f"Không tìm thấy Strategy Card với mã định danh '{card_id}'",
        )

    trace = microscope_service.inspect_bar(
        card=card,
        ohlcv=payload.ohlcv,
        context=payload.context,
    )

    return {
        "status": "success",
        "trace": trace.to_dict(),
    }
