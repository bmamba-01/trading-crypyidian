"""Các trang HTML quản trị; dữ liệu và quyền vẫn do service/backend quyết định."""

import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, cast
from urllib.parse import parse_qs
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from trading_bot.admin.bot_schemas import CreateBotRequest
from trading_bot.admin.bot_service import BotAdminError, BotConfigAdminService
from trading_bot.admin.presentation import (
    MARKET_DOWN_COLOR,
    MARKET_FLAT_COLOR,
    MARKET_UP_COLOR,
    METRICS,
    PNL_METRIC_KEYS,
    label_vi,
    local_time,
    number_vi,
    pnl_tone,
)
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
from trading_bot.application.telegram_signal_subscription import (
    TelegramSignalSubscriptionError,
)
from trading_bot.application.telegram_signals import TelegramSignalFilters
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


def _templates(request: Request) -> Jinja2Templates:
    """Đăng ký định dạng trình bày dùng chung cho mọi trang quản trị."""
    templates = cast(Jinja2Templates, request.app.state.templates)
    templates.env.filters.update(
        label_vi=label_vi,
        local_time=local_time,
        number_vi=number_vi,
        pnl_tone=pnl_tone,
    )
    templates.env.globals["report_metrics"] = METRICS
    templates.env.globals["pnl_metric_keys"] = PNL_METRIC_KEYS
    return templates


class AdminHTMLRoute(APIRoute):
    """Lỗi của trang HTML giữ mã HTTP và không phản chiếu dữ liệu nội bộ/biểu mẫu."""

    def get_route_handler(self) -> Any:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await original(request)
            except (StarletteHTTPException, RequestValidationError, AuthenticationFailure) as error:
                status = (
                    error.status_code
                    if isinstance(error, (StarletteHTTPException, AuthenticationFailure))
                    else 422
                )
                messages = {
                    401: "Phiên đăng nhập không hợp lệ hoặc đã hết hạn.",
                    403: "Xác thực hoặc quyền truy cập chưa đáp ứng yêu cầu.",
                    404: "Không tìm thấy nội dung trong phạm vi được cấp quyền.",
                    409: "Trạng thái đã thay đổi hoặc điều kiện vận hành chưa đáp ứng.",
                    422: "Thông tin gửi lên không hợp lệ hoặc chưa đầy đủ.",
                    429: "Bạn đã gửi quá nhiều yêu cầu. Hãy chờ rồi thử lại.",
                    503: "Dịch vụ tạm thời chưa sẵn sàng. Hãy thử lại sau.",
                }
                headers = (
                    dict(error.headers or {}) if isinstance(error, StarletteHTTPException) else {}
                )
                headers["Cache-Control"] = "no-store"
                if self.response_class is JSONResponse:
                    return JSONResponse(
                        {"detail": messages.get(status, "Không thể xử lý yêu cầu này.")},
                        status_code=status,
                        headers=headers,
                    )
                return _templates(request).TemplateResponse(
                    request=request,
                    name="admin/error.html",
                    context={
                        "identity": None,
                        "error_message": messages.get(status, "Không thể xử lý yêu cầu này."),
                        "error_status": status,
                    },
                    status_code=status,
                    headers=headers,
                )

        return handle


router = APIRouter(prefix="/admin", tags=["admin web"], route_class=AdminHTMLRoute)
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
    templates = _templates(request)
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


async def _parse_form(request: Request, *, max_bytes: int = 16_384) -> dict[str, str]:
    """Đọc form urlencoded giới hạn kích thước, từ chối field trùng để tránh nhập nhằng."""
    if (
        request.headers.get("content-type", "").split(";", 1)[0]
        != "application/x-www-form-urlencoded"
    ):
        raise HTTPException(status_code=415, detail="Chỉ nhận form urlencoded.")
    raw = await request.body()
    if len(raw) > max_bytes:
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
    report_end = datetime.now(UTC)
    report_start = report_end - timedelta(days=30)
    dashboard_reports: list[dict[str, Any]] = []
    metrics_loaded = False
    metrics_message = "Hãy thử lại sau hoặc xem báo cáo từng bot."
    active_bots = {bot.id: bot for bot in bot_views if bot.status in {"ENABLED", "ACTIVE"}}
    try:
        reports = await _reporting_service(request).get_dashboard_reports(
            BotReportQuery(
                workspace_id=principal.workspace_id,
                bot_id=None,
                start=report_start,
                end=report_end,
            ),
            principal,
            bot_ids=frozenset(UUID(bot_id) for bot_id in active_bots),
        )
        dashboard_reports = []
        for report in reports:
            if str(report.bot_id) not in active_bots:
                continue
            payload = report.as_dict()
            dashboard_reports.append(
                {
                    "name": active_bots[str(report.bot_id)].name,
                    "bot_id": str(report.bot_id),
                    "totals": payload["totals"],
                    "trades": payload["trades"],
                }
            )
        metrics_loaded = True
    except BotReportError, HTTPException:
        pass
    templates = _templates(request)
    return templates.TemplateResponse(
        request=request,
        name="admin/dashboard.html",
        context={
            "identity": identity,
            "bots": bot_views,
            "bot_count": len(bot_views),
            "ready_count": sum(bot.readiness == "READY" for bot in bot_views),
            "dashboard_reports": dashboard_reports,
            "dashboard_chart": _dashboard_performance_chart(dashboard_reports),
            "metrics_loaded": metrics_loaded,
            "metrics_message": metrics_message,
            "report_start": report_start,
            "report_end": report_end,
            "workspace": workspace,
            "csrf_token": csrf_token,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/dashboard/bots/{bot_id}/snapshot", response_class=JSONResponse)
async def dashboard_bot_snapshot(
    bot_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Đọc trạng thái và ledger PAPER của đúng bot, không cung cấp dữ liệu thị trường."""
    try:
        runtime = _runtime_dict(await _operations(request, identity).bot_status(bot_id))
        report_end = datetime.now(UTC)
        report_start = report_end - timedelta(days=30)
        report = (
            await _reporting_service(request).get_bot_report(
                BotReportQuery(
                    workspace_id=identity.principal.workspace_id,
                    bot_id=bot_id,
                    start=report_start,
                    end=report_end,
                    scope=ReportScope.BOT,
                ),
                identity.principal,
            )
        ).as_dict()
    except BotRuntimeControlError as error:
        status = 404 if error.reason_code in {"BOT_NOT_FOUND", "CONTROL_PERMISSION_DENIED"} else 503
        raise HTTPException(status_code=status, detail="Không thể tải ảnh chụp bot.") from error
    except BotReportError as error:
        status = 404 if error.status_code in {403, 404} else 503
        raise HTTPException(status_code=status, detail="Không thể tải ảnh chụp bot.") from error
    except SQLAlchemyError as error:
        raise HTTPException(status_code=503, detail="Không thể tải ảnh chụp bot.") from error

    # Chỉ chuyển các giá trị trình bày; không chuyển account, lineage hoặc ORM ra HTTP.
    runtime_fields = (
        "entry_state",
        "control_revision",
        "readiness",
        "readiness_reason_code",
        "workspace_kill_state",
        "workspace_control_revision",
        "entry_allowed",
    )
    trade_fields = ("trade_id", "symbol", "side", "quote_asset", "opened_at")
    closed_fields = trade_fields + (
        "closed_at",
        "gross_pnl",
        "fee_total",
        "funding_total",
        "slippage_total",
        "total_cost",
        "net_pnl",
    )
    metric_fields = {key for key, _label, _currency in METRICS} | {
        "quote_asset",
        "winning_trade_count",
        "losing_trade_count",
        "breakeven_trade_count",
        "total_cost",
    }
    recent_trades = sorted(
        report["trades"],
        key=lambda trade: (trade["closed_at"], trade["trade_id"]),
        reverse=True,
    )[:10]
    return JSONResponse(
        {
            "observed_at": datetime.now(UTC).isoformat(),
            "source": "PAPER_LEDGER",
            "bot": {
                "id": str(bot_id),
                "name": runtime.get("name"),
                "status": runtime.get("status"),
            },
            "runtime": {key: runtime.get(key) for key in runtime_fields},
            "window": {"start": report_start.isoformat(), "end": report_end.isoformat()},
            "totals": {
                asset: {key: value for key, value in totals.items() if key in metric_fields}
                for asset, totals in report["totals"].items()
            },
            "open_trades": [
                {key: trade[key] for key in trade_fields if key in trade}
                for trade in report["open_trades"]
            ],
            "recent_closed_trades": [
                {key: trade[key] for key in closed_fields if key in trade}
                for trade in recent_trades
            ],
        },
        headers={"Cache-Control": "no-store"},
    )


def _dashboard_performance_chart(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Tạo equity USDT, marker có giải thích và danh sách trade cùng nguồn ledger."""
    series: list[dict[str, Any]] = []
    trade_events: list[dict[str, Any]] = []
    for report in reports:
        events: list[tuple[datetime, int, dict[str, Any], str]] = []
        for trade in report["trades"]:
            if trade.get("quote_asset") != "USDT":
                continue
            entry_reason = trade.get("entry_reason")
            entry_reason = entry_reason if isinstance(entry_reason, dict) else {}
            strategy_id = entry_reason.get("strategy_id") or "Chưa có dữ liệu"
            entry_detail = (
                entry_reason.get("detail_vi")
                or entry_reason.get("reason_code")
                or "Chưa có dữ liệu"
            )
            exit_reason = trade.get("exit_reason") or ""
            exit_reason_vi = _exit_reason_label_vi(exit_reason)
            trade_events.append(
                {
                    "bot_name": report["name"],
                    "trade_id": trade["trade_id"],
                    "symbol": trade["symbol"],
                    "side": trade["side"],
                    "opened_at": trade["opened_at"],
                    "closed_at": trade["closed_at"],
                    "net_pnl": trade["net_pnl"],
                    "quote_asset": trade["quote_asset"],
                    "strategy_id": strategy_id,
                    "entry_detail_vi": entry_detail,
                    "exit_reason": exit_reason or "Chưa có dữ liệu",
                    "exit_reason_vi": exit_reason_vi,
                }
            )
            events.append((datetime.fromisoformat(trade["opened_at"]), 0, trade, "ENTRY"))
            events.append((datetime.fromisoformat(trade["closed_at"]), 1, trade, "EXIT"))
        running = Decimal("0")
        values_by_time: dict[int, Decimal] = {}
        markers: list[dict[str, Any]] = []
        for occurred_at, _order, trade, event_type in sorted(
            events, key=lambda event: (event[0], event[1], event[2]["trade_id"])
        ):
            timestamp = int(occurred_at.timestamp())
            if event_type == "EXIT":
                running += Decimal(trade["net_pnl"])
            values_by_time[timestamp] = running
            is_entry = event_type == "ENTRY"
            is_long = trade["side"] == "LONG"
            upward = is_long if is_entry else not is_long
            net_pnl = Decimal(trade["net_pnl"])
            exit_label = {
                "STOP_LOSS": "SL",
                "TAKE_PROFIT": "TP",
                "TIME_STOP": "TIME",
                "OPPOSING_SIGNAL": "ĐẢO",
            }.get(trade.get("exit_reason") or "", "ĐÓNG")
            entry_reason = trade.get("entry_reason")
            entry_reason = entry_reason if isinstance(entry_reason, dict) else {}
            strategy_id = entry_reason.get("strategy_id") or "Chưa có dữ liệu"
            entry_detail = (
                entry_reason.get("detail_vi")
                or entry_reason.get("reason_code")
                or "Chưa có dữ liệu"
            )
            exit_reason_vi = _exit_reason_label_vi(trade.get("exit_reason"))
            markers.append(
                {
                    "time": timestamp,
                    "position": "belowBar" if upward else "aboveBar",
                    "shape": "arrowUp" if upward else "arrowDown",
                    "color": (
                        MARKET_UP_COLOR
                        if (is_entry and is_long) or (not is_entry and net_pnl > 0)
                        else MARKET_DOWN_COLOR
                        if (is_entry and not is_long) or (not is_entry and net_pnl < 0)
                        else MARKET_FLAT_COLOR
                    ),
                    "text": f"Vào {trade['symbol']}"
                    if is_entry
                    else f"{exit_label} {net_pnl:+} USDT",
                    "title": (
                        f"Vào {trade['symbol']} theo {trade['side']}; "
                        f"thời điểm {occurred_at.isoformat()}; chiến thuật {strategy_id}; "
                        f"vì sao bắt tín hiệu: {entry_detail}"
                        if is_entry
                        else (
                            f"Đóng {trade['symbol']} lúc {occurred_at.isoformat()}; "
                            f"lý do: {exit_reason_vi}; PnL ròng {net_pnl} USDT; "
                            f"chiến thuật vào lệnh {strategy_id}"
                        )
                    ),
                }
            )
        points = [
            {"time": time, "value": float(value)} for time, value in sorted(values_by_time.items())
        ]
        if points:
            series.append({"name": report["name"], "data": points, "markers": markers})
    trade_events.sort(key=lambda event: (event["closed_at"], event["trade_id"]), reverse=True)
    return {"quote_asset": "USDT", "series": series, "events": trade_events[:50]}


def _exit_reason_label_vi(reason: object) -> str:
    """Dịch mã đóng vị thế đã lưu, giữ nguyên mã chưa được biết."""
    labels = {
        "STOP_LOSS": "Dừng lỗ",
        "TAKE_PROFIT": "Chốt lời",
        "TIME_STOP": "Đóng theo thời gian",
        "OPPOSING_SIGNAL": "Tín hiệu ngược chiều",
        "FLATTEN_OPERATION": "Đóng theo thao tác quản trị",
    }
    if not isinstance(reason, str) or not reason:
        return "Chưa có dữ liệu"
    return labels.get(reason, reason)


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
    templates = _templates(request)
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
    templates = _templates(request)
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
    signal_filter_error: str | None = None,
) -> Response:
    """Chi tiết bot; service dùng cùng lỗi 404 cho bot không tồn tại/ngoài grant."""
    response.headers["Cache-Control"] = "no-store"
    try:
        bot = await _admin_service(request).get_bot(identity.principal, bot_id=bot_id)
        runtime = _runtime_dict(await _operations(request, identity).bot_status(bot_id))
        csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
        signal_service = getattr(request.app.state, "telegram_signal_subscription_service", None)
        signal_state = (
            await signal_service.get_state(identity.principal, bot_id=bot_id)
            if signal_service is not None
            else None
        )
    except BotAdminError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    except BotRuntimeControlError as error:
        if error.reason_code == "BOT_NOT_FOUND":
            raise HTTPException(status_code=404, detail="Không tìm thấy bot.") from error
        raise HTTPException(status_code=503, detail="Không thể tải trạng thái bot.") from error
    except AuthenticationFailure as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    except TelegramSignalSubscriptionError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    templates = _templates(request)
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
            "signal_filter_error": signal_filter_error,
            "telegram_signal_state": signal_state,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/bots/{bot_id}/telegram-signals")
async def set_bot_telegram_signals_form(
    bot_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Bật/tắt cảnh báo tín hiệu Telegram; thao tác này không gửi hoặc ghi lệnh."""
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    raw_enabled = values.get("enabled")
    if raw_enabled not in {"true", "false"}:
        raise HTTPException(status_code=422, detail="Trạng thái đăng ký tín hiệu không hợp lệ.")
    service = getattr(request.app.state, "telegram_signal_subscription_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Đăng ký tín hiệu Telegram chưa sẵn sàng.")
    try:
        await service.set_enabled(
            identity.principal, bot_id=bot_id, enabled=(raw_enabled == "true")
        )
    except TelegramSignalSubscriptionError as error:
        message = str(error)
        status = 404 if "Không tìm thấy bot" in message else 409
        raise HTTPException(status_code=status, detail=message) from error
    return Response(
        status_code=303,
        headers={"Location": f"/admin/bots/{bot_id}", "Cache-Control": "no-store"},
    )


@router.post("/bots/{bot_id}/telegram-signal-filters")
async def set_bot_telegram_signal_filters_form(
    bot_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Lưu allowlist signal của chính thành viên; không kích hoạt giao dịch."""
    values = await _parse_form(request, max_bytes=65_536)
    await _verify_form_csrf(request, values)
    try:
        filters = TelegramSignalFilters(
            symbols=_parse_filter_values(values.get("symbols", "")),
            timeframes=_parse_filter_values(values.get("timeframes", "")),
            directions=_parse_filter_values(values.get("directions", "")),
            strategy_ids=_parse_filter_values(values.get("strategy_ids", "")),
            setup_codes=_parse_filter_values(values.get("setup_codes", "")),
        )
    except ValueError:
        return Response(
            status_code=303,
            headers={
                "Location": f"/admin/bots/{bot_id}?signal_filter_error=invalid",
                "Cache-Control": "no-store",
            },
        )

    service = getattr(request.app.state, "telegram_signal_subscription_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Đăng ký tín hiệu Telegram chưa sẵn sàng.")
    try:
        await service.set_filters(identity.principal, bot_id=bot_id, filters=filters)
    except TelegramSignalSubscriptionError as error:
        message = str(error)
        status = 404 if "Không tìm thấy bot" in message else 409
        raise HTTPException(status_code=status, detail=message) from error
    return Response(
        status_code=303,
        headers={"Location": f"/admin/bots/{bot_id}", "Cache-Control": "no-store"},
    )


def _parse_filter_values(raw: str) -> tuple[str, ...]:
    """Nhận danh sách tối đa 100 giá trị, ngăn cách bằng dấu phẩy hoặc dòng mới."""
    values = tuple(value.strip() for value in re.split(r"[,\r\n]+", raw) if value.strip())
    if len(values) > 100:
        raise ValueError("Một allowlist chỉ được có tối đa 100 giá trị.")
    return values


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
    templates = _templates(request)
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
    templates = _templates(request)
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
    templates = _templates(request)
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
    templates = _templates(request)
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
    templates = _templates(request)
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
    chart_trade_id: str | None = None,
    chart_timeframe: str = "1h",
) -> Response:
    """Báo cáo bot mặc định; chỉ tổng hợp workspace khi người dùng chọn scope rõ ràng."""
    response.headers["Cache-Control"] = "no-store"
    report: dict[str, Any] | None = None
    chart_data: dict[str, Any] | None = None
    parsed_bot_id: UUID | None = None
    parsed_chart_trade_id: UUID | None = None
    if bot_id and bot_id.strip():
        try:
            parsed_bot_id = UUID(bot_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="bot_id phải là UUID hợp lệ.") from error
    if chart_trade_id:
        try:
            parsed_chart_trade_id = UUID(chart_trade_id)
        except ValueError as error:
            raise HTTPException(
                status_code=422, detail="chart_trade_id phải là UUID hợp lệ."
            ) from error
    if (
        any(value is not None for value in (start, end, scope))
        or parsed_bot_id is not None
        or parsed_chart_trade_id is not None
    ):
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
        if parsed_chart_trade_id is not None:
            if report_scope is not ReportScope.BOT or parsed_bot_id is None:
                raise HTTPException(
                    status_code=422,
                    detail="Biểu đồ trade cần phạm vi một bot được chọn rõ ràng.",
                )
            try:
                chart_data = await service.get_trade_chart(
                    query,
                    identity.principal,
                    trade_id=parsed_chart_trade_id,
                    timeframe=chart_timeframe,
                )
            except BotReportError as error:
                raise HTTPException(
                    status_code=error.status_code,
                    detail={"code": error.code, "message": error.message},
                ) from error
    try:
        bots = await _admin_service(request).list_bots(identity.principal)
    except BotAdminError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    templates = _templates(request)
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
            "chart_data": chart_data,
            "chart_trade_id": str(parsed_chart_trade_id or ""),
            "chart_timeframe": chart_timeframe,
        },
        headers={"Cache-Control": "no-store"},
    )
