"""Các trang HTML quản trị; dữ liệu và quyền vẫn do service/backend quyết định."""

import csv
import hashlib
import hmac
import io
import json
import logging
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, cast
from urllib.parse import parse_qs, urlencode
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from trading_bot.admin.bot_schemas import CreateBotRequest
from trading_bot.admin.bot_service import BotAdminError, BotConfigAdminService
from trading_bot.admin.form_choices import (
    BOT_MAX_NOTIONAL_PRESETS,
    RISK_FIELD_SPECS,
    Choice,
    coin_choices,
    direction_choices,
    preset_choices,
    release_choices,
    risk_fields,
    setup_code_choices,
    strategy_id_choices,
    timeframe_choices,
)
from trading_bot.admin.presentation import (
    LABELS,
    MARKET_DOWN_COLOR,
    MARKET_FLAT_COLOR,
    MARKET_UP_COLOR,
    METRICS,
    PNL_METRIC_KEYS,
    label_vi,
    local_minute,
    local_time,
    number2_vi,
    number_vi,
    pnl_tone,
)
from trading_bot.admin.session_redirect import (
    ADMIN_HOME_PATH,
    NEXT_PARAM,
    login_redirect_for,
    safe_next_path,
)
from trading_bot.admin.strategy_card_service import StrategyCardAdminService
from trading_bot.admin.strategy_release_service import (
    StrategyReleaseAdminError,
    StrategyReleaseAdminService,
)
from trading_bot.application.analyst_review import AnalystReviewError, AnalystReviewService
from trading_bot.application.bot_lifecycle_guard import BotNotFlatError
from trading_bot.application.bot_operations import BotOperationsService, PrincipalBotOperations
from trading_bot.application.bot_reporting import (
    BotReportError,
    BotReportingService,
    BotReportQuery,
    ReportScope,
)
from trading_bot.application.bot_runtime_control import BotRuntimeControlError
from trading_bot.application.economic_calendar import (
    EconomicCalendarError,
    EconomicCalendarService,
)
from trading_bot.application.flatten_all import FlattenAllError, FlattenAllService
from trading_bot.application.learning_post_mortem import LearningPostMortemService
from trading_bot.application.market_chart import (
    MARKET_CHART_DEFAULT_LIMIT,
    MARKET_CHART_INTERVAL_SECONDS,
    MARKET_CHART_MAX_LIMIT,
    build_market_chart_payload,
    chart_fingerprint,
)
from trading_bot.application.market_levels import price_levels
from trading_bot.application.market_narrative import decision_narrative, market_narrative
from trading_bot.application.market_scanner import MarketScannerError, MarketScannerService
from trading_bot.application.operational_telemetry import OperationalHealthService
from trading_bot.application.signal_outcome_review import (
    SignalOutcomeReviewError,
    SignalOutcomeReviewService,
)
from trading_bot.application.strategy_pipeline.microscope import MicroscopeReplayService
from trading_bot.application.telegram_binding import TelegramTopicSetupError
from trading_bot.application.telegram_signal_subscription import (
    TelegramSignalSubscriptionError,
)
from trading_bot.application.telegram_signals import TelegramSignalFilters
from trading_bot.auth.action_challenge import (
    ACTION_BREAKER_RESUME,
    ActionChallengeError,
    breaker_resume_payload,
    extract_step_up_proof,
    http_step_up_scope,
)
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.cookies import read_session_cookie, set_session_cookie
from trading_bot.auth.policy import MfaMethod, Permission, Principal, Role, authorize
from trading_bot.auth.route_policy import (
    BotScopePolicy,
    CsrfMode,
    EnforcementMode,
    bot_scoped_route,
    owner_route,
    permission_route,
    public_route,
    roles_route,
)
from trading_bot.auth.router import (
    authenticate_login,
    client_address,
    current_identity,
    enforce_endpoint_step_up,
    get_rate_limiter,
    require_csrf,
)
from trading_bot.auth.schemas import LoginRequest
from trading_bot.auth.security import TEST_TOTP_CODE
from trading_bot.auth.service import (
    RECENT_MFA_LIFETIME,
    SESSION_LIFETIME,
    AuthenticationFailure,
    AuthIdentity,
    AuthService,
)
from trading_bot.config.bot_settings import BotConfigOverlay
from trading_bot.config.workspace_settings import decimal_text
from trading_bot.domain.market.economic_calendar import IMPACT_LABELS_VI, EconomicEventInput
from trading_bot.domain.market.scanner import SCAN_INTERVALS, ScanCriteria
from trading_bot.domain.portfolio.post_mortem import evaluate_trade_post_mortem
from trading_bot.domain.portfolio.reporting import (
    REPORT_MODE,
    REPORT_VERSION,
)
from trading_bot.domain.portfolio.setup_statistics import trade_statistics
from trading_bot.domain.risk.circuit_breaker import (
    CircuitBreakerEngine,
    CircuitBreakerResumeDenied,
    OperationalTelemetry,
)
from trading_bot.domain.risk.risk_windows import (
    RISK_LOSS_WINDOW_POLICY,
    calculate_loss_windows,
)
from trading_bot.domain.strategy.setup_progress import context_conditions
from trading_bot.domain.strategy_card.schema import compute_canonical_hash
from trading_bot.observability.health import build_system_health_snapshot

try:
    VN_TZ: tzinfo = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:  # pragma: no cover
    VN_TZ = timezone(timedelta(hours=7))

LOGIN_CSRF_COOKIE = "crypidian_login_csrf"
LOGIN_CSRF_HOST_COOKIE = "__Host-crypidian_login_csrf"
# Đủ thời gian để mở ứng dụng xác thực hoặc trình quản lý mật khẩu; hết hạn thì trang đăng
# nhập được hiển thị lại với token mới thay vì trang lỗi.
LOGIN_CSRF_MAX_AGE_SECONDS = 900
LOGIN_FORM_EXPIRED_MESSAGE = (
    "Biểu mẫu đăng nhập đã hết hạn hoặc được mở ở tab khác. Vui lòng nhập lại mật khẩu và mã "
    "xác thực rồi gửi lại."
)


def _templates(request: Request) -> Jinja2Templates:
    """Đăng ký định dạng trình bày dùng chung cho mọi trang quản trị."""
    templates = cast(Jinja2Templates, request.app.state.templates)
    templates.env.filters.update(
        label_vi=label_vi,
        local_time=local_time,
        number_vi=number_vi,
        number2_vi=number2_vi,
        local_minute=local_minute,
        pnl_tone=pnl_tone,
    )
    templates.env.globals["report_metrics"] = METRICS
    templates.env.globals["pnl_metric_keys"] = PNL_METRIC_KEYS
    templates.env.globals["ui_labels_vi"] = LABELS
    # Chú giải chỉ số (UI-1): import cục bộ để thay đổi chỉ nằm trong hàm đăng ký này.
    from trading_bot.admin.presentation import METRIC_HELP_GLOBALS

    templates.env.globals.update(METRIC_HELP_GLOBALS)
    return templates


# Mã lý do an toàn được phép hiển thị trên trang lỗi (không chứa dữ liệu nội bộ), kèm gợi ý.
SAFE_ERROR_REASONS: dict[str, str] = {
    "FLATTEN_PREVIEW_STALE": (
        "Vị thế hoặc lệnh đã thay đổi sau khi tạo bản xem trước. Tạo bản xem trước mới rồi "
        "xác nhận lại."
    ),
    "FLATTEN_PREVIEW_EXPIRED": (
        "Bản xem trước đã quá 90 giây. Tạo bản xem trước mới rồi xác nhận lại."
    ),
    "FLATTEN_IDEMPOTENCY_KEY_REUSED": (
        "Bản xem trước này đã được dùng cho một lần xác nhận. Mở trang thao tác vừa tạo hoặc "
        "tạo bản xem trước mới."
    ),
    "RECENT_MFA_REQUIRED": "Cần xác thực MFA gần đây. Đăng nhập lại rồi thử lại.",
    "CONTROL_PERMISSION_DENIED": "Tài khoản không có quyền thực hiện thao tác này.",
    "CATALOG_HASH_MISMATCH": (
        "Mã băm (SHA-256) không khớp với danh mục chiến lược hiện hành. Hãy tải lại trang "
        "và chỉ công bố bản phát hành chính thức từ catalog."
    ),
    "CONTROL_REASON_REQUIRED": (
        "Lý do thao tác cần từ 3 đến 500 ký tự tiếng Việt rõ ràng để ghi vào nhật ký kiểm toán."
    ),
    "UNKNOWN_STRATEGY_RELEASE": (
        "Mã phát hành chiến lược không tồn tại trong danh mục sản phẩm hiện hành."
    ),
    "RELEASE_NOT_PUBLISHED": (
        "Bản phát hành chưa ở trạng thái Đã công bố trong không gian làm việc này."
    ),
}


class AdminHTTPException(StarletteHTTPException):
    """Lỗi trang HTML kèm mã lý do an toàn để trang lỗi gợi ý bước tiếp theo đúng."""

    def __init__(self, status_code: int, detail: str, *, reason_code: str) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.reason_code = reason_code if reason_code in SAFE_ERROR_REASONS else None


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
                # Phiên hết hạn khi người dùng mở trang/gửi form HTML: về trang đăng nhập.
                redirect = login_redirect_for(request, status)
                if redirect is not None:
                    return redirect
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
                is_api = request.url.path.startswith("/admin/api/")
                if self.response_class is JSONResponse or is_api:
                    payload: dict[str, Any] = {
                        "detail": messages.get(status, "Không thể xử lý yêu cầu này.")
                    }
                    reason_code = getattr(error, "reason_code", None)
                    if reason_code in SAFE_ERROR_REASONS:
                        payload["status"] = "error"
                        payload["code"] = reason_code
                    return JSONResponse(
                        payload,
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
                        "error_reason_code": getattr(error, "reason_code", None),
                        "error_reason_hint": SAFE_ERROR_REASONS.get(
                            getattr(error, "reason_code", None) or ""
                        ),
                    },
                    status_code=status,
                    headers=headers,
                )

        return handle


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin web"], route_class=AdminHTMLRoute)


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
    needs_manual_enable: bool = False
    # None khi bot không khai báo mã: không tự điền BTCUSDT (dữ liệu bịa).
    symbol: str | None = None


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


def _learning_post_mortem_service(request: Request) -> LearningPostMortemService:
    service = cast(
        LearningPostMortemService | None,
        getattr(request.app.state, "learning_post_mortem_service", None),
    )
    if service is None:
        service = LearningPostMortemService()
        request.app.state.learning_post_mortem_service = service
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


def _strategy_release_service(request: Request) -> StrategyReleaseAdminService:
    service = cast(
        StrategyReleaseAdminService | None,
        getattr(request.app.state, "strategy_release_service", None),
    )
    if service is None:
        service = StrategyReleaseAdminService()
        request.app.state.strategy_release_service = service
    return service


class StrategyReleaseActionRequest(BaseModel):
    """Yêu cầu công bố hoặc thu hồi một `strategy_release` từ Admin API."""

    release_id: str
    reason: str
    content_hash: str | None = None


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
        max_age=LOGIN_CSRF_MAX_AGE_SECONDS,
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


# Tiêu đề và gợi ý cụ thể theo loại lỗi. Nội dung lỗi xác thực cố ý không nói trường nào sai
# để không lộ workspace/email có tồn tại hay không (chống dò tài khoản).
LOGIN_ERROR_DETAILS: dict[str, tuple[str, str]] = {
    "invalid": (
        "Đăng nhập không thành công",
        "Kiểm tra mã không gian làm việc, email, mật khẩu và mã xác thực. Mỗi mã 6 chữ số chỉ "
        "dùng được một lần; nếu vừa dùng, chờ mã mới (tối đa 30 giây).",
    ),
    "rate_limited": (
        "Tạm khóa đăng nhập",
        "Đã thử sai quá nhiều lần. Chờ hết thời gian giới hạn rồi thử lại.",
    ),
    "expired": (
        "Biểu mẫu đã hết hạn",
        "Trang đã được làm mới; nhập lại mật khẩu và mã xác thực.",
    ),
}


def _login_page(
    request: Request,
    *,
    error: str | None = None,
    status_code: int | None = None,
    error_kind: str = "invalid",
    next_path: str | None = None,
) -> Response:
    token = secrets.token_urlsafe(32)
    templates = _templates(request)
    page = templates.TemplateResponse(
        request=request,
        name="admin/login.html",
        context={
            "identity": None,
            "csrf_token": token,
            # Chỉ đường dẫn nội bộ `/admin...` đã kiểm tra mới được đưa vào form.
            "next_path": safe_next_path(next_path),
            "error": error,
            "error_title": LOGIN_ERROR_DETAILS.get(error_kind, LOGIN_ERROR_DETAILS["invalid"])[0],
            "error_hint": LOGIN_ERROR_DETAILS.get(error_kind, LOGIN_ERROR_DETAILS["invalid"])[1],
            "test_totp_code": (
                TEST_TOTP_CODE if request.app.state.settings.auth_test_totp_enabled else None
            ),
        },
        status_code=status_code or (401 if error else 200),
    )
    page.headers["Cache-Control"] = "no-store"
    _set_login_csrf_cookie(page, request, token)
    return page


@router.get(
    "/login",
    dependencies=[
        public_route(reason="Trang HTML đăng nhập Admin Web; cấp login CSRF cookie trước phiên.")
    ],
)
async def login_page(request: Request) -> Response:
    """Hiển thị form đăng nhập không lưu cache và cấp token chống login-CSRF.

    `next` (nếu an toàn) được giữ trong form để quay lại trang cũ sau khi đăng nhập.
    """
    return _login_page(request, next_path=request.query_params.get(NEXT_PARAM))


@router.post(
    "/login",
    dependencies=[
        public_route(
            reason=(
                "Xử lý form đăng nhập Admin Web với login CSRF cookie, "
                "mật khẩu, TOTP và rate limit."
            ),
            csrf=CsrfMode.LOGIN_COOKIE,
        )
    ],
)
async def submit_login(request: Request) -> Response:
    """Xử lý form HTML qua cùng service MFA/throttle với endpoint đăng nhập API."""
    try:
        values = await _parse_form(request)
    except HTTPException:
        raise
    cookie_token = request.cookies.get(_login_csrf_cookie_name(request), "")
    form_token = values.pop("csrf_token", "")
    next_path = safe_next_path(values.pop(NEXT_PARAM, ""))
    if not cookie_token or not form_token or not hmac.compare_digest(cookie_token, form_token):
        # Không xác thực gì: chỉ trả lại form với token mới để người dùng gửi lại.
        return _login_page(
            request,
            error=LOGIN_FORM_EXPIRED_MESSAGE,
            status_code=403,
            error_kind="expired",
            next_path=next_path,
        )
    try:
        credentials = LoginRequest.model_validate(values)
    except ValidationError:
        return _login_page(request, error="Thông tin đăng nhập không hợp lệ.", next_path=next_path)
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
            return _login_page(
                request,
                error="Thử lại sau khi hết thời gian giới hạn.",
                status_code=429,
                error_kind="rate_limited",
                next_path=next_path,
            )
        return _login_page(request, error="Thông tin đăng nhập không hợp lệ.", next_path=next_path)
    settings = request.app.state.settings
    response = Response(
        status_code=303,
        headers={"Location": next_path or ADMIN_HOME_PATH, "Cache-Control": "no-store"},
    )
    set_session_cookie(
        response,
        settings,
        result.cookie_value,
        max_age=int(SESSION_LIFETIME.total_seconds()),
    )
    _clear_login_csrf_cookie(response, request)
    return response


async def _parse_form(
    request: Request,
    *,
    max_bytes: int = 16_384,
    multi_value_fields: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Đọc form urlencoded giới hạn kích thước, từ chối field trùng để tránh nhập nhằng.

    Chỉ các trường trong `multi_value_fields` (multi-select) được lặp; giá trị được nối bằng
    xuống dòng để bộ phân tích danh sách hiện có xử lý như trước.
    """
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
    if any(len(items) != 1 for key, items in values.items() if key not in multi_value_fields):
        raise HTTPException(status_code=422, detail="Form có trường lặp.")
    return {
        key: "\n".join(items) if key in multi_value_fields else items[0]
        for key, items in values.items()
    }


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
        needs_manual_enable=bool(runtime.get("needs_manual_enable", False)),
        symbol=str(bot["symbol"]) if bot.get("symbol") else None,
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


async def _build_operations_health_data(
    *,
    workspace_id: str,
    runtime: list[dict[str, Any]],
    workspace: dict[str, Any],
    correlation_id: str,
    now_utc: datetime,
    request: Request | None = None,
    actor_membership_id: UUID | None = None,
) -> dict[str, Any]:
    """Tổng hợp ảnh chụp sức khỏe vận hành thật từ runtime và workspace hiện hành."""
    health_service = (
        getattr(request.app.state, "operational_health_service", None)
        if request is not None
        else None
    )
    if health_service is None and request is not None:
        session_factory = getattr(request.app.state, "session_factory", None)
        if session_factory is not None:
            health_service = OperationalHealthService(session_factory)
    if health_service is not None:
        result = cast(
            "dict[str, Any]",
            await health_service.load_health_snapshot(
                workspace_id=UUID(workspace_id),
                actor_membership_id=actor_membership_id,
                runtime=runtime,
                workspace=workspace,
                correlation_id=correlation_id,
                now_utc=now_utc,
            ),
        )
        result["workers"] = await _collect_worker_readiness(
            request, workspace_id=UUID(workspace_id)
        )
        return result

    total_api_errors = sum(int(item.get("error_count", 0) or 0) for item in runtime)
    loss_snapshot = calculate_loss_windows(
        events=[],
        as_of=now_utc,
        policy=RISK_LOSS_WINDOW_POLICY,
    )
    telemetry = OperationalTelemetry(
        evaluated_at=now_utc,
        feed_age_seconds=None,
        daily_loss=None,
        drawdown_pct=None,
        consecutive_api_errors=total_api_errors,
        outbox_lag_seconds=None,
        clock_skew_ms=None,
        unprotected_exposure_count=None,
        reconciliation_discrepancy_count=None,
        unknown_order_count=None,
    )
    breaker_snapshot = CircuitBreakerEngine().evaluate(telemetry)
    health_snapshot = build_system_health_snapshot(
        service_name="crypidian-admin",
        environment="PAPER",
        correlation_id=correlation_id,
        breaker=breaker_snapshot,
        loss_windows=loss_snapshot,
        metadata={
            "workspace_id": workspace_id,
            "kill_state": str(workspace.get("kill_state", "ACTIVE")),
            "runtime_bot_count": len(runtime),
            "telemetry": telemetry.to_evidence(),
            "telemetry_available": False,
            "telemetry_source": "unavailable_no_db",
        },
        evaluated_at=now_utc,
    )
    payload = health_snapshot.to_dict()
    payload["workers"] = await _collect_worker_readiness(request, workspace_id=UUID(workspace_id))
    return payload


async def _collect_worker_readiness(
    request: Request | None, *, workspace_id: UUID | None = None
) -> list[dict[str, Any]]:
    """Thu thập readiness reason-coded của các worker cho admin-web (CRY-375)."""
    from trading_bot.observability.readiness import WorkerReadinessReport, WorkerReadinessState

    if request is None:
        return []
    reports: list[dict[str, Any]] = []
    provider = getattr(request.app.state, "worker_readiness_provider", None)
    if callable(provider):
        provided = provider()
        if hasattr(provided, "__await__"):
            provided = await provided
        reports = [
            item.to_dict() if isinstance(item, WorkerReadinessReport) else dict(item)
            for item in provided
        ]
    else:
        for attr in (
            "market_signal_worker",
            "telegram_signal_worker",
            "telegram_trade_result_worker",
            "backtest_worker",
        ):
            worker = getattr(request.app.state, attr, None)
            if worker is not None and callable(getattr(worker, "readiness_snapshot", None)):
                snap = worker.readiness_snapshot()
                reports.append(
                    snap.to_dict() if isinstance(snap, WorkerReadinessReport) else dict(snap)
                )

    if workspace_id is not None:
        reader = getattr(request.app.state, "worker_heartbeat_reader", None)
        if reader is None:
            session_factory = getattr(request.app.state, "session_factory", None)
            if session_factory is not None:
                from trading_bot.application.ingestion_worker_heartbeat import (
                    WorkerHeartbeatReadService,
                )

                reader = WorkerHeartbeatReadService(session_factory)
        if reader is not None:
            persisted = await reader.list_readiness(workspace_id=workspace_id)
            by_component = {str(item.get("component")): item for item in reports}
            for item in persisted:
                by_component[item.component] = item.to_dict()
            reports = list(by_component.values())

    if not reports:
        # Khi admin-web chạy tách container, kiểm tra DB probe để báo rõ trạng thái kết nối.
        probe = getattr(request.app.state, "database_probe", None)
        db_ok = bool(await probe()) if callable(probe) else True
        if not db_ok:
            reports.append(
                WorkerReadinessReport(
                    component="worker_telemetry_store",
                    state=WorkerReadinessState.NOT_READY,
                    reason_code="DATABASE_UNAVAILABLE",
                    detail_vi="Admin-web không truy cập được cơ sở dữ liệu trạng thái worker.",
                ).to_dict()
            )
    return reports


@router.get(
    "",
    dependencies=[
        permission_route(
            Permission.WORKSPACE_READ,
            bot_scope=BotScopePolicy(BotPermission.READ, param_name=None, param_location="service"),
        )
    ],
)
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
        workspace = (
            _runtime_dict(await operations.workspace_status())
            if hasattr(operations, "workspace_status")
            else {"kill_state": "ACTIVE", "revision": 1}
        )
        csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    except BotAdminError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    runtime_by_id = {str(item["id"]): item for item in runtime}
    bot_views = [_bot_view(bot, runtime_by_id.get(str(bot["id"]))) for bot in bots]
    # Thống kê 30 ngày và biểu đồ hiệu suất đọc từ ledger PAPER; chỉ gồm bot đang bật mà
    # principal có quyền xem báo cáo. Lỗi quyền/báo cáo không làm hỏng phần điều khiển.
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
        dashboard_reports = []
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
            "dashboard_live_help": _dashboard_live_help_payload(),
            "strategy_explainers": _strategy_explainers(dashboard_reports),
            "metrics_loaded": metrics_loaded,
            "metrics_message": metrics_message,
            "report_start": report_start,
            "report_end": report_end,
            "workspace": workspace,
            "csrf_token": csrf_token,
            "operations_health": await _build_operations_health_data(
                workspace_id=str(principal.workspace_id),
                runtime=runtime,
                workspace=workspace,
                correlation_id=f"dash-{uuid4().hex[:10]}",
                now_utc=report_end,
                request=request,
                actor_membership_id=principal.membership_id,
            ),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/api/dashboard/realtime",
    dependencies=[
        permission_route(
            Permission.WORKSPACE_READ,
            bot_scope=BotScopePolicy(BotPermission.READ, param_name=None, param_location="service"),
        )
    ],
)
async def dashboard_realtime(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    fp: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Cung cấp dữ liệu cập nhật thời gian thực định kỳ (5s) cho Admin Dashboard.

    Hỗ trợ delta fetching thông qua tham số `fp` (fingerprint) và `since`. Khi không
    có thay đổi trạng thái, server trả về phản hồi siêu nhẹ `status: unchanged`.
    """
    response.headers["Cache-Control"] = "no-store"
    now_utc = datetime.now(UTC)
    now_vn = now_utc.astimezone(VN_TZ)

    principal = identity.principal
    try:
        bots = await _admin_service(request).list_bots(principal)
        operations = _operations(request, identity)
        runtime = [_runtime_dict(item) for item in await operations.list_bots()]
        workspace = (
            _runtime_dict(await operations.workspace_status())
            if hasattr(operations, "workspace_status")
            else {"kill_state": "ACTIVE", "revision": 1}
        )
    except BotAdminError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error

    runtime_by_id = {str(item["id"]): item for item in runtime}
    bot_views = [_bot_view(bot, runtime_by_id.get(str(bot["id"]))) for bot in bots]

    # Tính toán fingerprint để kiểm tra delta fetching
    current_fp_items = [
        str(workspace.get("revision", 1)),
        str(len(bots)),
    ]
    for b in bot_views:
        current_fp_items.append(f"{b.id}:{b.control_revision}:{b.status}:{b.readiness}")
    current_fingerprint = hashlib.sha256(":".join(current_fp_items).encode()).hexdigest()[:16]

    if fp is not None and fp == current_fingerprint:
        return {
            "status": "unchanged",
            "fingerprint": current_fingerprint,
            "server_time": {
                "iso": now_utc.isoformat(),
                "vn": now_vn.strftime("%d/%m/%Y %H:%M:%S"),
                "vn_time": now_vn.strftime("%H:%M:%S"),
            },
            "bot_count": len(bot_views),
            "ready_count": sum(bot.readiness == "READY" for bot in bot_views),
        }

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
        "status": "updated",
        "fingerprint": current_fingerprint,
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
        "operations_health": await _build_operations_health_data(
            workspace_id=str(principal.workspace_id),
            runtime=runtime,
            workspace=workspace,
            correlation_id=f"rt-{uuid4().hex[:10]}",
            now_utc=now_utc,
            request=request,
            actor_membership_id=principal.membership_id,
        ),
    }


@router.get(
    "/dashboard/bots/{bot_id}/snapshot",
    response_class=JSONResponse,
    dependencies=[bot_scoped_route(BotPermission.REPORT_READ)],
)
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
    # Vốn ban đầu là phần bổ sung: lỗi đọc không được làm hỏng cả ảnh chụp.
    account_seed: dict[str, Any] | None = None
    seed_reader = getattr(_reporting_service(request), "get_account_seed", None)
    if callable(seed_reader):
        try:
            account_seed = await seed_reader(
                BotReportQuery(
                    workspace_id=identity.principal.workspace_id,
                    bot_id=bot_id,
                    start=report_start,
                    end=report_end,
                    scope=ReportScope.BOT,
                ),
                identity.principal,
            )
        except BotReportError, SQLAlchemyError:
            account_seed = None
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
            "account": (
                {
                    "quote_asset": account_seed["quote_asset"],
                    "initial_equity": account_seed["initial_equity"],
                }
                if account_seed
                else None
            ),
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


def _strategy_explainers(reports: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Giải thích cách mỗi bot ra quyết định, chỉ từ release đã ghi trong lệnh thật.

    Nguồn: `entry_reason` của các lệnh trong báo cáo 30 ngày (release, mã lý do, giải thích,
    giá tham chiếu, thời điểm tín hiệu) và metadata của release trong catalog sản phẩm (tên,
    tóm tắt quy tắc, khung thời gian, chỉ báo). Bot chưa có lệnh thì không suy đoán chiến lược.
    """
    from trading_bot.application.strategy_catalog import strategy_registry
    from trading_bot.application.strategy_market_requirements import (
        entry_timeframe_for_release,
        feature_requirements_for_release,
    )

    registry = strategy_registry()
    explainers: dict[str, dict[str, Any]] = {}
    for report in reports:
        trades = sorted(
            report.get("trades") or [],
            key=lambda trade: str(trade.get("opened_at") or ""),
            reverse=True,
        )
        releases: dict[str, dict[str, Any]] = {}
        signals: list[dict[str, Any]] = []
        symbols: set[str] = set()
        for trade in trades:
            reason = trade.get("entry_reason")
            reason = reason if isinstance(reason, dict) else {}
            release_id = str(reason.get("strategy_release_id") or "")
            symbols.add(str(trade.get("symbol") or ""))
            if release_id and release_id not in releases:
                entry: dict[str, Any] = {"release_id": release_id}
                try:
                    release = registry.get_release(release_id)
                except LookupError:
                    entry["unknown"] = True
                else:
                    entry.update(
                        strategy_id=release.strategy_id,
                        name_vi=release.name_vi,
                        rule_summary_vi=release.rule_summary_vi,
                        status=release.status,
                        recipe_version=release.recipe_version,
                        content_hash=release.content_hash[:12],
                    )
                entry["timeframe"] = entry_timeframe_for_release(release_id)
                try:
                    features = feature_requirements_for_release(release_id)
                except ValueError:
                    features = {}
                entry["features"] = [
                    f"{timeframe}: {', '.join(names)}" for timeframe, names in features.items()
                ]
                releases[release_id] = entry
            if len(signals) < 5 and reason:
                signals.append(
                    {
                        "symbol": trade.get("symbol"),
                        "side": trade.get("side"),
                        "signal_at": reason.get("signal_at") or trade.get("opened_at"),
                        "reason_code": reason.get("reason_code"),
                        "detail_vi": reason.get("detail_vi"),
                        "reference_price": reason.get("reference_price"),
                        "exit_reason": trade.get("exit_reason"),
                        "net_pnl": trade.get("net_pnl"),
                        "quote_asset": trade.get("quote_asset"),
                    }
                )
        explainers[str(report["bot_id"])] = {
            "name": report.get("name"),
            "releases": list(releases.values()),
            "symbols": sorted(symbol for symbol in symbols if symbol),
            "signals": signals,
        }
    return explainers


def _dashboard_live_help_payload() -> dict[str, dict[str, str]]:
    """Xuất chú giải từ `metric_help.HELP` cho vùng tổng `dashboard-live.js` (CRY-200 / UI-1)."""
    from trading_bot.admin.metric_help import HELP

    keys = [key for key, _title, _monetary in METRICS] + ["total_equity"]
    payload: dict[str, dict[str, str]] = {}
    for key in keys:
        entry = HELP.get(key)
        if entry is None:
            continue
        payload[key] = {
            "title_vi": entry.title_vi,
            "definition_vi": entry.definition_vi,
            "formula_vi": entry.formula_vi,
            "reading_vi": entry.reading_vi,
            "limits_vi": entry.limits_vi,
        }
    return payload


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
                "CHANNEL_EXIT": "KÊNH",
                "FEATURE_EXIT": "CHỈ BÁO",
                "DELIST_EXIT": "GỠ",
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
                    else f"{exit_label} {'+' if net_pnl > 0 else ''}{number2_vi(net_pnl)} USDT",
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
        "CHANNEL_EXIT": "Thoát theo kênh giá",
        "FEATURE_EXIT": "Thoát theo chỉ báo",
        "FLATTEN_OPERATION": "Đóng theo thao tác quản trị",
        "DELIST_EXIT": "Đóng trước khi Binance gỡ hợp đồng",
    }
    if not isinstance(reason, str) or not reason:
        return "Chưa có dữ liệu"
    return labels.get(reason, reason)


@router.get(
    "/bots",
    dependencies=[
        permission_route(
            Permission.WORKSPACE_READ,
            bot_scope=BotScopePolicy(BotPermission.READ, param_name=None, param_location="service"),
        )
    ],
)
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


@router.get(
    "/bots/new",
    dependencies=[owner_route(denied_detail="Chỉ owner được tạo bot.")],
)
async def create_bot_page(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Form tạo account PAPER; quyền owner/MFA vẫn được kiểm lại bởi application service."""
    try:
        csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    except AuthenticationFailure as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    templates = _templates(request)
    return templates.TemplateResponse(
        request=request,
        name="admin/bot_create.html",
        context={
            "identity": identity,
            "csrf_token": csrf_token,
            "max_notional_choices": _max_notional_choices("10000"),
        },
        headers={"Cache-Control": "no-store"},
    )


def _max_notional_choices(selected: str) -> object:
    """Mốc giới hạn danh nghĩa cho form tạo bot; giá trị lạ đã gửi vẫn được giữ để sửa."""
    return preset_choices(BOT_MAX_NOTIONAL_PRESETS, selected, formatter="number")


@router.post(
    "/bots/new",
    dependencies=[owner_route(csrf=CsrfMode.FORM, denied_detail="Chỉ owner được tạo bot.")],
)
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
    except ValidationError as error:
        fields = {str(item["loc"][0]) for item in error.errors() if item.get("loc")}
        messages = [CREATE_BOT_FIELD_ERRORS[f] for f in ("name", "max_notional") if f in fields]
        return _create_bot_form_response(
            request, identity, values, messages or ["Thông tin tạo bot không hợp lệ."]
        )
    try:
        with http_step_up_scope(request, form_values=values):
            bot, _created = await _admin_service(request).create_bot(
                identity.principal,
                name=body.name,
                max_notional=Decimal(body.max_notional),
            )
    except ActionChallengeError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    except BotAdminError as error:
        if error.status_code in {409, 422}:
            # Lỗi nghiệp vụ người dùng sửa được: hiện lại form kèm giá trị đã nhập.
            return _create_bot_form_response(
                request, identity, values, [error.message], status_code=error.status_code
            )
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    return Response(status_code=303, headers={"Location": f"/admin/bots/{bot['id']}"})


CREATE_BOT_FIELD_ERRORS = {
    "name": (
        "Tên bot bắt đầu bằng chữ hoặc số, chỉ gồm chữ không dấu, số, khoảng trắng, "
        "dấu chấm, gạch dưới, gạch ngang (tối đa 120 ký tự)."
    ),
    "max_notional": "Giới hạn giá trị danh nghĩa phải lớn hơn 0 và là số thập phân hợp lệ.",
}


def _create_bot_form_response(
    request: Request,
    identity: AuthIdentity,
    values: dict[str, str],
    errors: list[str],
    *,
    status_code: int = 422,
) -> Response:
    """Hiện lại form tạo bot với lỗi và giá trị đã nhập (Jinja tự escape); giữ CSRF đã gửi."""
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/bot_create.html",
        status_code=status_code,
        context={
            "identity": identity,
            "csrf_token": values.get("csrf_token", ""),
            "form_errors": errors,
            "form_values": {
                "name": values.get("name", ""),
                "max_notional": values.get("max_notional", ""),
            },
            "max_notional_choices": _max_notional_choices(values.get("max_notional", "")),
        },
        headers={"Cache-Control": "no-store"},
    )


BOT_DETAIL_CHART_TIMEFRAME = "1h"
# Vai trò được gắn nhãn ca giao dịch cho rà soát huấn luyện lại.
RETRAINING_LABEL_ROLES = frozenset({Role.OWNER, Role.STRATEGY_ADMIN, Role.ANALYST_REVIEWER})
RETRAINING_LABEL_TRADE_FIELDS = (
    "symbol",
    "side",
    "quote_asset",
    "opened_at",
    "closed_at",
    "gross_pnl",
    "fee_total",
    "funding_total",
    "slippage_total",
    "net_pnl",
    "exit_reason",
    "holding_duration_seconds",
    "entry_reason",
    "fills",
    "lineage",
)
BOT_DETAIL_RECENT_TRADE_LIMIT = 10


def _fill_summary(fills: object, role: str) -> tuple[str | None, str | None]:
    """Trả (khối lượng, giá bình quân gia quyền) của các fill theo vai trò, lấy từ ledger.

    Không có fill hợp lệ thì trả `None` để giao diện ghi "chưa có dữ liệu" thay vì số 0.
    """
    quantity = Decimal("0")
    notional = Decimal("0")
    if isinstance(fills, list):
        for fill in fills:
            if not isinstance(fill, dict) or fill.get("role") != role:
                continue
            try:
                fill_quantity = Decimal(str(fill.get("quantity")))
                fill_price = Decimal(str(fill.get("price")))
            except InvalidOperation:
                continue
            if not fill_quantity.is_finite() or not fill_price.is_finite() or fill_quantity <= 0:
                continue
            quantity += fill_quantity
            notional += fill_quantity * fill_price
    if quantity <= 0:
        return None, None
    return format(quantity.normalize(), "f"), format((notional / quantity).normalize(), "f")


async def _approved_trade_plans(
    service: Any, query: BotReportQuery, principal: Principal, trades: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Kế hoạch đã duyệt theo `trade_id`; là nguồn thật cho SL/TP, đòn bẩy và chi phí dự trù.

    Lỗi quyền/đọc không làm hỏng trang: thiếu kế hoạch thì hậu kiểm ghi quy tắc "chưa kiểm được".
    """
    trade_ids: list[UUID] = []
    for trade in trades:
        try:
            trade_ids.append(UUID(str(trade.get("trade_id"))))
        except ValueError:
            continue
    if not trade_ids:
        return {}
    try:
        plans = await service.get_trade_plan_summaries(query, principal, trade_ids=trade_ids)
    except BotReportError, HTTPException, SQLAlchemyError:
        return {}
    if not isinstance(plans, dict):
        return {}
    return {str(trade_id): plan for trade_id, plan in plans.items() if isinstance(plan, dict)}


def _recent_trade_view(trade: dict[str, Any], plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dựng một dòng lịch sử lệnh cho Bot Detail chỉ từ trường có trong ledger PAPER."""
    post_mortem = evaluate_trade_post_mortem(trade, plan=plan)
    entry_reason = trade.get("entry_reason")
    entry_reason = entry_reason if isinstance(entry_reason, dict) else {}
    quantity, entry_price = _fill_summary(trade.get("fills"), "ENTRY")
    _exit_quantity, exit_price = _fill_summary(trade.get("fills"), "EXIT")
    quote_asset = str(trade.get("quote_asset") or "")
    return {
        "trade_id": str(trade.get("trade_id")),
        "symbol": str(trade.get("symbol") or ""),
        "side": str(trade.get("side") or ""),
        "quote_asset": quote_asset,
        "quantity": quantity,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "net_pnl": trade.get("net_pnl"),
        "closed_at": trade.get("closed_at"),
        "exit_reason": trade.get("exit_reason"),
        "exit_reason_vi": _exit_reason_label_vi(trade.get("exit_reason")),
        "strategy_id": entry_reason.get("strategy_id"),
        "entry_detail_vi": entry_reason.get("detail_vi"),
        "fee_total": trade.get("fee_total"),
        "funding_total": trade.get("funding_total"),
        "slippage_total": trade.get("slippage_total"),
        "quality": post_mortem.classification.value,
        "is_deviant": post_mortem.is_deviant,
        "deviant_reason": post_mortem.deviation_reason,
    }


def _format_drawdown_breaker_view(state: Any) -> dict[str, Any]:
    """Chuyển DrawdownBreakerState sang dict hiển thị kèm hướng dẫn mở lại (CRY-197)."""
    drawdown_pct = state.drawdown_fraction * Decimal("100")
    max_drawdown_pct = state.max_drawdown_fraction * Decimal("100")
    return {
        "tripped": bool(state.tripped),
        "drawdown_pct": f"{drawdown_pct:.2f}%",
        "max_drawdown_pct": f"{max_drawdown_pct:.2f}%",
        "peak_equity": str(state.peak_equity),
        "current_equity": str(state.current_equity),
        "baseline_at": state.baseline_at.isoformat() if state.baseline_at else None,
        "policy_version": str(state.policy_version),
        "resume_guidance_vi": (
            "Cầu dao sụt vốn đã ngắt mạch và đang chặn mở vị thế mới (thoát lệnh bảo vệ vẫn "
            "hoạt động). Để mở lại: kiểm tra nguyên nhân sụt vốn, sau đó bấm «Tạm dừng» rồi "
            "«Tiếp tục» bot (yêu cầu xác thực MFA gần đây và ghi rõ lý do) để đặt mốc đỉnh vốn mới."
        ),
    }


async def _load_bot_drawdown_breaker(
    request: Request,
    principal: Principal,
    bot_id: UUID,
    bot: dict[str, Any],
) -> dict[str, Any] | None:
    """Đọc trạng thái ngắt mạch sụt vốn thật cho trang chi tiết bot (CRY-197)."""
    raw_account = bot.get("account") if isinstance(bot, dict) else None
    raw_account_id = raw_account.get("id") if isinstance(raw_account, dict) else None
    account_id: UUID | None = None
    if raw_account_id:
        try:
            account_id = UUID(str(raw_account_id))
        except ValueError:
            account_id = None

    provider = getattr(request.app.state, "bot_drawdown_breaker_provider", None)
    if provider is not None:
        try:
            state = await provider.load_bot_drawdown_breaker(
                workspace_id=principal.workspace_id,
                bot_id=bot_id,
                account_id=account_id,
            )
            return _format_drawdown_breaker_view(state) if state is not None else None
        except Exception:  # noqa: BLE001 — trang chi tiết bot chỉ đọc, không được hỏng vì breaker
            return None

    session_factory = getattr(request.app.state, "database_session_factory", None)
    if session_factory is None or account_id is None:
        return None

    from types import SimpleNamespace

    from trading_bot.adapters.database.repositories.account_equity import AccountEquityRepository
    from trading_bot.adapters.database.repositories.workspace_settings import (
        WorkspaceSettingsRepository,
    )
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.application.paper_market_context import (
        PostgresPaperDecisionContextProvider,
    )
    from trading_bot.application.workspace_risk import mark_policy_from
    from trading_bot.domain.risk.drawdown_breaker import drawdown_policy_for

    now = datetime.now(UTC)
    try:
        async with UnitOfWork(
            session_factory,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as uow:
            published = await WorkspaceSettingsRepository(uow.session).load_effective_config(
                workspace_id=principal.workspace_id
            )
            if published is None:
                return None
            settings = published.settings
            equity = await AccountEquityRepository(uow.session).load_paper_equity_snapshot(
                workspace_id=principal.workspace_id,
                bot_id=bot_id,
                account_id=account_id,
                as_of=now,
                max_equity_age=timedelta(seconds=settings.risk.max_equity_age_seconds),
                max_mark_age=timedelta(seconds=settings.risk.max_mark_age_seconds),
                mark_policy=mark_policy_from(settings),
            )
            if not equity.known or not equity.seed_capital:
                return None
            ctx_provider = PostgresPaperDecisionContextProvider(
                session_factory, workspace_id=principal.workspace_id
            )
            state = await ctx_provider._drawdown_breaker(
                uow.session,
                SimpleNamespace(account_id=account_id, bot_id=bot_id),
                as_of=now,
                equity=equity.equity_amount,
                seed=equity.seed_capital,
                policy=drawdown_policy_for(settings.risk.max_drawdown_fraction),
            )
            return _format_drawdown_breaker_view(state)
    except Exception:  # noqa: BLE001 — lỗi DB/chưa seed chỉ ẩn khối breaker, không làm hỏng trang
        return None


async def _get_bot_trading_data(
    request: Request,
    principal: Principal,
    bot_id: UUID,
) -> dict[str, Any]:
    """Tổng hợp chỉ số, vị thế mở, lệnh gần đây và nến cho Bot Detail từ ledger PAPER.

    Mọi giá trị đều đọc từ báo cáo bot 30 ngày và kho nến nội bộ. Khi chưa có nguồn hoặc
    principal không có quyền báo cáo, hàm trả trạng thái rỗng; không dựng số liệu mẫu.
    PAPER chưa có mark-to-market nên vị thế mở không có giá tham chiếu hay PnL chưa chốt.
    """
    empty: dict[str, Any] = {
        "data_available": False,
        "metrics": None,
        "positions": [],
        "recent_trades": [],
        "chart_data": None,
    }
    reporting_service = getattr(request.app.state, "bot_reporting_service", None)
    if reporting_service is None:
        return empty
    now_utc = datetime.now(UTC)
    query = BotReportQuery(
        workspace_id=principal.workspace_id,
        bot_id=bot_id,
        start=now_utc - timedelta(days=30),
        end=now_utc,
        scope=ReportScope.BOT,
    )
    try:
        report = (await reporting_service.get_bot_report(query, principal)).as_dict()
    except BotReportError, HTTPException, SQLAlchemyError:
        return empty

    totals_by_asset = report.get("totals") or {}
    quote_asset = "USDT" if "USDT" in totals_by_asset else next(iter(sorted(totals_by_asset)), None)
    totals = totals_by_asset.get(quote_asset, {}) if quote_asset is not None else {}
    open_trades = [trade for trade in report.get("open_trades") or [] if isinstance(trade, dict)]
    metrics: dict[str, Any] = {
        "quote_asset": quote_asset,
        "other_quote_assets": sorted(asset for asset in totals_by_asset if asset != quote_asset),
        "open_positions_count": len(open_trades),
        "closed_trade_count": totals.get("closed_trade_count", 0),
        "win_rate_percent": totals.get("win_rate_percent"),
        "profit_factor": totals.get("profit_factor"),
        "expectancy_per_trade": totals.get("expectancy_per_trade"),
        "net_pnl": totals.get("net_pnl"),
        "max_drawdown": totals.get("max_drawdown"),
    }
    positions = [
        {
            "trade_id": str(trade.get("trade_id")),
            "symbol": str(trade.get("symbol") or ""),
            "side": str(trade.get("side") or ""),
            "quote_asset": str(trade.get("quote_asset") or ""),
            "opened_at": trade.get("opened_at"),
        }
        for trade in open_trades
    ]
    closed_trades = sorted(
        (trade for trade in report.get("trades") or [] if isinstance(trade, dict)),
        key=lambda trade: (str(trade.get("closed_at")), str(trade.get("trade_id"))),
        reverse=True,
    )[:BOT_DETAIL_RECENT_TRADE_LIMIT]
    plans = await _approved_trade_plans(reporting_service, query, principal, closed_trades)
    recent_trades = [
        _recent_trade_view(trade, plans.get(str(trade.get("trade_id")))) for trade in closed_trades
    ]

    chart_data: dict[str, Any] | None = None
    if closed_trades:
        try:
            chart = await reporting_service.get_trade_chart(
                query,
                principal,
                trade_id=UUID(str(closed_trades[0]["trade_id"])),
                timeframe=BOT_DETAIL_CHART_TIMEFRAME,
            )
        except BotReportError, HTTPException, SQLAlchemyError, KeyError, ValueError:
            chart = None
        # Chỉ vẽ khi kho nến nội bộ thật sự có dữ liệu cho giao dịch gần nhất.
        if chart and chart.get("bars"):
            chart_data = chart
    return {
        "data_available": True,
        "metrics": metrics,
        "positions": positions,
        "recent_trades": recent_trades,
        "chart_data": chart_data,
    }


@router.get(
    "/api/bots/{bot_id}/realtime",
    dependencies=[bot_scoped_route(BotPermission.READ)],
)
async def bot_detail_realtime(
    bot_id: UUID,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    fp: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Cung cấp dữ liệu cập nhật thời gian thực định kỳ (5s) cho Bot Detail view.

    Hỗ trợ delta fetching thông qua tham số `fp` (fingerprint) và `since`. Khi không
    có thay đổi trạng thái hoặc vị thế, server trả về phản hồi siêu nhẹ `status: unchanged`.
    """
    response.headers["Cache-Control"] = "no-store"
    now_utc = datetime.now(UTC)
    now_vn = now_utc.astimezone(VN_TZ)

    try:
        bot = await _admin_service(request).get_bot(identity.principal, bot_id=bot_id)
        runtime = _runtime_dict(await _operations(request, identity).bot_status(bot_id))
    except BotAdminError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    except BotRuntimeControlError as error:
        if error.reason_code == "BOT_NOT_FOUND":
            raise HTTPException(status_code=404, detail="Không tìm thấy bot.") from error
        raise HTTPException(status_code=503, detail="Không thể tải trạng thái bot.") from error

    bot_view = _bot_view(bot, runtime)

    # Tính toán fingerprint kiểm tra delta fetching
    current_fp_items = [
        str(bot_view.control_revision),
        str(bot_view.status),
        str(bot_view.readiness),
        str(bot_view.entry_state),
        str(bot_view.entry_allowed),
    ]
    current_fingerprint = hashlib.sha256(":".join(current_fp_items).encode()).hexdigest()[:16]

    if fp is not None and fp == current_fingerprint:
        return {
            "status": "unchanged",
            "fingerprint": current_fingerprint,
            "server_time": {
                "iso": now_utc.isoformat(),
                "vn": now_vn.strftime("%d/%m/%Y %H:%M:%S"),
                "vn_time": now_vn.strftime("%H:%M:%S"),
            },
            "bot_id": str(bot_id),
        }

    trading_data = await _get_bot_trading_data(request, identity.principal, bot_id)

    return {
        "status": "updated",
        "fingerprint": current_fingerprint,
        "server_time": {
            "iso": now_utc.isoformat(),
            "vn": now_vn.strftime("%d/%m/%Y %H:%M:%S"),
            "vn_time": now_vn.strftime("%H:%M:%S"),
        },
        "bot": {
            "id": bot_view.id,
            "name": bot_view.name,
            "status": bot_view.status,
            "account_mode": bot_view.account_mode,
            "account_status": bot_view.account_status,
            "readiness": bot_view.readiness,
            "entry_state": bot_view.entry_state,
            "entry_allowed": bot_view.entry_allowed,
            "control_revision": bot_view.control_revision,
            "needs_manual_enable": bot_view.needs_manual_enable,
            "symbol": bot_view.symbol,
        },
        "data_available": trading_data["data_available"],
        "metrics": trading_data["metrics"],
        "positions": trading_data["positions"],
        "recent_trades": trading_data["recent_trades"],
        "chart_data": trading_data["chart_data"],
    }


async def _root_universe(request: Request, principal: Principal) -> dict[str, Any]:
    """Universe và release mặc định của cấu hình gốc để giới hạn ô chọn overlay.

    Không đọc được (thiếu service, chưa có cấu hình, lỗi DB) thì trả rỗng: ô chọn dùng danh mục
    đầy đủ và máy chủ vẫn từ chối overlay vượt cấu hình gốc khi kiểm định.
    """
    service = getattr(request.app.state, "settings_admin_service", None)
    if service is None:
        return {}
    try:
        current = await service.get_current(principal)
    except SQLAlchemyError, PermissionError, LookupError, TypeError, ValueError:
        return {}
    settings = (current or {}).get("settings") or {}
    market = settings.get("market_data") or {}
    strategy = settings.get("strategy") or {}
    return {
        "symbols": tuple(str(item) for item in market.get("symbols") or ()),
        "timeframes": tuple(str(item) for item in market.get("timeframes") or ()),
        "default_release": str(strategy.get("default_release") or ""),
    }


def _signal_filter_choices(signal_state: Any) -> dict[str, Any] | None:
    """Danh sách chọn cho bộ lọc tín hiệu Telegram, đánh dấu giá trị đã lưu."""
    filters = getattr(signal_state, "filters", None)
    if filters is None:
        return None
    saved = filters.as_dict()
    return {
        "symbols": coin_choices(saved["symbols"]),
        "timeframes": timeframe_choices(saved["timeframes"]),
        "directions": direction_choices(saved["directions"]),
        "strategy_ids": strategy_id_choices(saved["strategy_ids"]),
        "setup_codes": setup_code_choices(saved["setup_codes"]),
    }


def _filter_published_release_choices(
    choices: tuple[Choice, ...], published_release_ids: tuple[str, ...] | None
) -> tuple[Choice, ...]:
    """Giới hạn ô chọn release của bot theo các bản đã công bố trong workspace (SC-3)."""
    if published_release_ids is None:
        return choices
    allowed = set(published_release_ids)
    filtered: list[Choice] = []
    for item in choices:
        if not item.value or item.value in allowed:
            filtered.append(item)
        elif item.selected:
            label = (
                item.label if "(ngoài danh mục)" in item.label else f"{item.label} (chưa công bố)"
            )
            filtered.append(Choice(value=item.value, label=label, selected=True, data=item.data))
    return tuple(filtered)


def _overlay_form_choices(
    overlay: dict[str, Any],
    root: dict[str, Any],
    *,
    published_release_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Ô chọn cho bản nháp overlay, điền sẵn theo bản nháp hoặc bản đang công bố."""
    strategy = overlay.get("strategy") or {}
    universe = overlay.get("universe") or {}
    root_symbols = root.get("symbols") or None
    root_timeframes = root.get("timeframes") or None
    default_release = root.get("default_release") or ""
    inherit = (
        f"Kế thừa cấu hình gốc ({default_release})" if default_release else "Kế thừa cấu hình gốc"
    )
    raw_releases = release_choices(str(strategy.get("release_id") or ""), inherit_label=inherit)
    return {
        "releases": _filter_published_release_choices(raw_releases, published_release_ids),
        "symbols": coin_choices(tuple(universe.get("symbols") or ()), only=root_symbols),
        "timeframes": timeframe_choices(
            tuple(universe.get("timeframes") or ()), only=root_timeframes
        ),
        "risk": risk_fields(dict(overlay.get("risk") or {})),
        "root_limited": bool(root_symbols),
        "has_parameters": bool(strategy.get("parameters")),
        "exit": _exit_form(overlay, default_release),
    }


EXIT_FORM_FIELDS = (
    ("stop_atr_multiple", "Stop-loss (× ATR)"),
    ("trailing_atr_multiple", "Trailing stop (× ATR)"),
    ("take_profit_atr_multiple", "Take-profit tối đa (× ATR)"),
    ("max_hold_bars", "Giữ tối đa (số nến)"),
)


def _exit_review(
    draft: dict[str, Any] | None,
    published: dict[str, Any] | None,
    backtests: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Bản nháp có đổi khối ``exit``: trạng thái backtest bắt buộc (gate thật ở service)."""
    if draft is None:
        return None
    before = dict((published or {}).get("overlay") or {}).get("exit") or {}
    after = dict(draft.get("overlay") or {}).get("exit") or {}
    if before == after:
        return None
    record = next(
        (
            item
            for item in backtests
            if item.get("source") == "OWNER_DRAFT" and item.get("draft_id") == draft.get("id")
        ),
        None,
    )
    return {"before": before, "after": after, "backtest": record}


def _ladder_text(steps: Any) -> str:
    """Thang TP dạng "1:0.5, 2:0.5" (bội số R : tỷ lệ khối lượng)."""
    parts = []
    for step in steps:
        r_value = step["r_multiple"] if isinstance(step, dict) else step.r_multiple
        fraction = step["fraction"] if isinstance(step, dict) else step.fraction
        parts.append(
            f"{decimal_text(Decimal(str(r_value)))}:{decimal_text(Decimal(str(fraction)))}"
        )
    return ", ".join(parts)


def _exit_form(overlay: dict[str, Any], default_release: str) -> dict[str, Any]:
    """CRY-428: ô ghi đè thoát lệnh — chỉ trường release vốn có; rỗng = kế thừa release."""
    from trading_bot.application.exit_policy_catalog import policy_for_release

    release = str((overlay.get("strategy") or {}).get("release_id") or default_release)
    policy = policy_for_release(release) if release else None
    current = dict(overlay.get("exit") or {})
    fields: list[dict[str, Any]] = []
    ladder: dict[str, Any] | None = None
    if policy is not None:
        for name, label in EXIT_FORM_FIELDS:
            base = getattr(policy, name)
            if base is None:
                continue
            fields.append(
                {
                    "name": name,
                    "label": label,
                    "value": "" if current.get(name) is None else str(current[name]),
                    "base": str(base) if name == "max_hold_bars" else decimal_text(base),
                }
            )
        if policy.ladder:
            current_ladder = current.get("ladder") or {}
            ladder = {
                "value": _ladder_text(current_ladder.get("steps") or ()),
                "base": _ladder_text(policy.ladder),
                "breakeven": bool(
                    current_ladder.get("breakeven_after_first", policy.breakeven_after_first)
                ),
            }
    return {
        "release": release,
        "fields": fields,
        "ladder": ladder,
        "has_override": bool(current),
        "stop_fixed": policy is not None and policy.stop_atr_multiple is None,
    }


@router.get(
    "/bots/{bot_id}",
    dependencies=[bot_scoped_route(BotPermission.READ)],
)
async def bot_detail(
    bot_id: UUID,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    draft_validation: str | None = None,
    signal_filter_error: str | None = None,
    ai_proposal: str | None = None,
    ai_proposal_error: str | None = None,
) -> Response:
    """Chi tiết bot; service dùng cùng lỗi 404 cho bot không tồn tại/ngoài grant."""
    response.headers["Cache-Control"] = "no-store"
    try:
        bot = await _admin_service(request).get_bot(identity.principal, bot_id=bot_id)
        ai_proposals = await _ai_proposals(request, identity, bot_id)
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

    bot_view = _bot_view(bot, runtime)
    trading_data = await _get_bot_trading_data(request, identity.principal, bot_id)
    drawdown_breaker = await _load_bot_drawdown_breaker(request, identity.principal, bot_id, bot)
    decision_insights: dict[str, Any] = {"narrative": [], "missing_vi": None}
    reporting = getattr(request.app.state, "bot_reporting_service", None)
    if reporting is not None:
        try:
            signal_view = await reporting.get_latest_signal(identity.principal, bot_id=bot_id)
            latest_dec = signal_view.get("latest_decision")
            latest_card = signal_view.get("card")
            from trading_bot.domain.strategy.setup_progress import (
                format_setup_missing_vi,
                setup_progress,
            )

            setup_view: dict[str, Any] | None = None
            if latest_dec is not None:
                setup_view = setup_progress(
                    strategy_release_id=str(latest_dec.get("strategy_release_id") or "—"),
                    decisions=[latest_dec],
                    bonus=(),
                    now=datetime.now(UTC),
                )
            decision_insights = {
                "narrative": decision_narrative(latest_dec, setup=setup_view, card=latest_card),
                "missing_vi": format_setup_missing_vi(setup_view) if setup_view else None,
            }
        except BotReportError, HTTPException, SQLAlchemyError, ValueError:
            decision_insights = {"narrative": [], "missing_vi": None}

    templates = _templates(request)
    versions = bot.get("versions", [])
    published = next((item for item in versions if item.get("status") == "PUBLISHED"), None)
    draft = next((item for item in versions if item.get("status") == "DRAFT"), None)
    root_universe = await _root_universe(request, identity.principal)
    current_overlay = dict((draft or published or {}).get("overlay") or {})
    exit_review = _exit_review(draft, published, ai_proposals)
    published_release_ids: tuple[str, ...] | None = None
    release_service = _strategy_release_service(request)
    if release_service.has_workspace_state(identity.principal.workspace_id):
        try:
            published_release_ids = await release_service.list_published_release_ids(
                identity.principal
            )
        except SQLAlchemyError, StrategyReleaseAdminError:
            published_release_ids = None
    bot_strategy_id = str(current_overlay.get("strategy_id") or "").strip()
    bot_strategy_card_id: str | None = None
    if bot_strategy_id:
        from trading_bot.application.strategy_catalog import PRODUCT_CARD_IDS

        if bot_strategy_id in PRODUCT_CARD_IDS:
            bot_strategy_card_id = bot_strategy_id
    return templates.TemplateResponse(
        request=request,
        name="admin/bot_detail.html",
        context={
            "identity": identity,
            "bot": bot_view,
            "bot_strategy_card_id": bot_strategy_card_id,
            "csrf_token": csrf_token,
            "config_parent_version_id": published.get("id", "") if published else "",
            "config_draft": draft,
            "config_draft_json": json.dumps(draft.get("overlay", {}), ensure_ascii=False, indent=2)
            if draft
            else '{\n  "schema_version": 1\n}',
            "draft_validation": draft_validation,
            "signal_filter_error": signal_filter_error,
            "ai_proposals": ai_proposals,
            "ai_proposal_status": "created" if ai_proposal == "created" else None,
            # Chỉ hiển thị lỗi mẫu đã biết (tránh chèn văn bản tùy ý qua query string).
            "ai_proposal_error": _AI_PROPOSAL_ERRORS.get(ai_proposal_error or ""),
            "telegram_signal_state": signal_state,
            "signal_filter_choices": _signal_filter_choices(signal_state),
            "overlay_form": _overlay_form_choices(
                current_overlay,
                root_universe,
                published_release_ids=published_release_ids,
            ),
            "exit_review": exit_review,
            "decision_insights": decision_insights,
            "drawdown_breaker": drawdown_breaker,
            "trading_data_available": trading_data["data_available"],
            "metrics": trading_data["metrics"],
            "positions": trading_data["positions"],
            "recent_trades": trading_data["recent_trades"],
            "chart_data": trading_data["chart_data"],
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/bots/{bot_id}/telegram-signals",
    dependencies=[bot_scoped_route(BotPermission.READ, csrf=CsrfMode.FORM)],
)
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


@router.post(
    "/bots/{bot_id}/telegram-signal-filters",
    dependencies=[bot_scoped_route(BotPermission.READ, csrf=CsrfMode.FORM)],
)
async def set_bot_telegram_signal_filters_form(
    bot_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Lưu allowlist signal của chính thành viên; không kích hoạt giao dịch."""
    values = await _parse_form(request, max_bytes=65_536, multi_value_fields=SIGNAL_FILTER_FIELDS)
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


SIGNAL_FILTER_FIELDS = frozenset(
    {"symbols", "timeframes", "directions", "strategy_ids", "setup_codes"}
)
OVERLAY_MULTI_FIELDS = frozenset({"universe_symbols", "universe_timeframes"})


def _parse_filter_values(raw: str) -> tuple[str, ...]:
    """Nhận danh sách tối đa 100 giá trị, ngăn cách bằng dấu phẩy hoặc dòng mới."""
    values = tuple(value.strip() for value in re.split(r"[,\r\n]+", raw) if value.strip())
    if len(values) > 100:
        raise ValueError("Một allowlist chỉ được có tối đa 100 giá trị.")
    return values


@router.post(
    "/bots/{bot_id}/config-drafts",
    dependencies=[bot_scoped_route(BotPermission.CONFIG_WRITE, csrf=CsrfMode.FORM)],
)
async def create_config_draft_form(
    bot_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    values = await _parse_form(request, multi_value_fields=OVERLAY_MULTI_FIELDS)
    await _verify_form_csrf(request, values)
    try:
        if values.get("overlay_source") == "json":
            payload = json.loads(values.get("overlay_json", ""))
        else:
            payload = _overlay_from_form(values)
        overlay = BotConfigOverlay.model_validate(payload)
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


def _overlay_from_form(values: dict[str, str]) -> dict[str, Any]:
    """Dựng overlay v1 từ ô chọn; ô để "Kế thừa" (rỗng) không được đưa vào overlay."""
    overlay: dict[str, Any] = {"schema_version": 1}
    release_id = values.get("strategy_release_id", "").strip()
    if release_id:
        overlay["strategy"] = {"release_id": release_id}
    universe: dict[str, Any] = {}
    for field, key in (("universe_symbols", "symbols"), ("universe_timeframes", "timeframes")):
        items = _parse_filter_values(values.get(field, ""))
        if items:
            universe[key] = list(items)
    if universe:
        overlay["universe"] = universe
    risk: dict[str, Any] = {}
    for name, *_rest in RISK_FIELD_SPECS:
        raw = values.get(f"risk_{name}", "").strip()
        if not raw:
            continue
        if name == "max_positions":
            if not raw.isdigit():
                raise ValueError("Số vị thế tối đa phải là số nguyên dương.")
            risk[name] = int(raw)
        else:
            risk[name] = raw
    if risk:
        overlay["risk"] = risk
    exit_block = _exit_from_form(values)
    if exit_block:
        overlay["exit"] = exit_block
    return overlay


def _exit_from_form(values: dict[str, str]) -> dict[str, Any]:
    """Ô thoát lệnh để trống = kế thừa release; thang TP dạng "1:0.5, 2:0.5"."""
    exit_block: dict[str, Any] = {}
    for name, _label in EXIT_FORM_FIELDS:
        raw = values.get(f"exit_{name}", "").strip()
        if not raw:
            continue
        if name == "max_hold_bars":
            if not raw.isdigit():
                raise ValueError("Số nến giữ tối đa phải là số nguyên dương.")
            exit_block[name] = int(raw)
        else:
            exit_block[name] = raw.replace(",", ".")
    ladder_raw = values.get("exit_ladder", "").strip()
    if ladder_raw:
        steps = []
        for part in ladder_raw.replace(";", ",").split(","):
            if not part.strip():
                continue
            r_text, separator, fraction_text = part.partition(":")
            if not separator:
                raise ValueError("Thang TP phải có dạng R:tỷ lệ, ví dụ 1:0.5, 2:0.5.")
            steps.append({"r_multiple": r_text.strip(), "fraction": fraction_text.strip()})
        exit_block["ladder"] = {
            "steps": steps,
            "breakeven_after_first": values.get("exit_ladder_breakeven") == "on",
        }
    return exit_block


async def _ai_proposals(
    request: Request, identity: AuthIdentity, bot_id: UUID
) -> list[dict[str, Any]]:
    """Mục phụ "Đề xuất AI đã backtest": lỗi đọc không được làm hỏng trang chi tiết bot."""
    try:
        rows = await _admin_service(request).list_proposal_backtests(
            identity.principal, bot_id=bot_id
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("Không đọc được đề xuất AI của bot. error=%s", type(error).__name__)
        return []
    return rows if isinstance(rows, list) else []


_AI_PROPOSAL_ERRORS = {
    "stale": "Cấu hình bot đã đổi so với bản gốc đã backtest; cần backtest lại trước khi áp.",
    "verdict": "Kết luận backtest không cho phép tạo bản nháp (kém hơn hoặc không backtest được).",
    "mfa": "Đề xuất đổi khối rủi ro: cần xác thực MFA gần đây rồi thử lại.",
    "permission": "Không đủ quyền tạo bản nháp cho bot này.",
    "conflict": "Không tạo được bản nháp (đã có bản nháp hoặc cấu hình vừa đổi). Tải lại trang.",
    "invalid": "Cấu hình đề xuất không hợp lệ với lược đồ hoặc giới hạn rủi ro gốc.",
    "missing": "Không tìm thấy kết quả backtest đề xuất.",
}


def _ai_proposal_error_code(error: BotAdminError) -> str:
    message = error.message
    if error.status_code == 404:
        return "missing"
    if "MFA" in message:
        return "mfa"
    if error.status_code == 403:
        return "permission"
    if "backtest lại" in message:
        return "stale"
    if "Kết luận" in message:
        return "verdict"
    if error.status_code == 422 or error.errors:
        return "invalid"
    return "conflict"


@router.post(
    "/bots/{bot_id}/ai-proposals/{backtest_id}/draft",
    dependencies=[bot_scoped_route(BotPermission.CONFIG_WRITE, csrf=CsrfMode.FORM)],
)
async def create_draft_from_ai_proposal_form(
    bot_id: UUID,
    backtest_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """AI-3: tạo bản nháp từ đề xuất AI đã backtest; owner vẫn kiểm định + công bố (MFA)."""
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    try:
        await _admin_service(request).create_draft_from_proposal(
            identity.principal, bot_id=bot_id, backtest_id=backtest_id
        )
    except BotAdminError as error:
        code = _ai_proposal_error_code(error)
        return Response(
            status_code=303,
            headers={"Location": f"/admin/bots/{bot_id}?ai_proposal_error={code}#ai-proposals"},
        )
    return Response(
        status_code=303,
        headers={"Location": f"/admin/bots/{bot_id}?ai_proposal=created#ai-proposals"},
    )


@router.post(
    "/bots/{bot_id}/config-drafts/{draft_id}/validate",
    dependencies=[bot_scoped_route(BotPermission.CONFIG_WRITE, csrf=CsrfMode.FORM)],
)
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


@router.post(
    "/bots/{bot_id}/config-drafts/{draft_id}/publish",
    dependencies=[bot_scoped_route(BotPermission.CONFIG_PUBLISH, csrf=CsrfMode.FORM)],
)
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
        with http_step_up_scope(request, form_values=values):
            await _admin_service(request).publish(
                identity.principal,
                bot_id=bot_id,
                draft_id=draft_id,
                expected_revision=expected_revision,
            )
    except ActionChallengeError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    except BotAdminError as error:
        if error.status_code == 409 and any(item.get("field") == "exit" for item in error.errors):
            # CRY-428: thiếu backtest bản nháp đổi thông số thoát → về trang bot kèm hướng dẫn.
            return Response(
                status_code=303,
                headers={
                    "Location": f"/admin/bots/{bot_id}?draft_validation=exit_backtest_required"
                },
            )
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    return Response(status_code=303, headers={"Location": f"/admin/bots/{bot_id}"})


@router.post(
    "/bots/{bot_id}/pause",
    dependencies=[
        bot_scoped_route(
            BotPermission.RUNTIME_PAUSE,
            csrf=CsrfMode.FORM,
            enforcement=EnforcementMode.SERVICE_AUDITED,
        )
    ],
)
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


@router.post(
    "/bots/{bot_id}/resume",
    dependencies=[
        bot_scoped_route(
            BotPermission.RUNTIME_RESUME,
            csrf=CsrfMode.FORM,
            enforcement=EnforcementMode.SERVICE_AUDITED,
        )
    ],
)
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
        with http_step_up_scope(request, form_values=values):
            await _operations(request, identity).resume_bot(bot_id, reason, expected_revision)
    except ActionChallengeError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    except BotRuntimeControlError as error:
        raise _control_error(error) from error
    return Response(status_code=303, headers={"Location": f"/admin/bots/{bot_id}"})


@router.post("/bots/{bot_id}/disable", dependencies=[owner_route(csrf=CsrfMode.FORM)])
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


@router.post("/bots/{bot_id}/enable", dependencies=[owner_route(csrf=CsrfMode.FORM)])
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
        with http_step_up_scope(request, form_values=values):
            await _operations(request, identity).enable_bot(bot_id, reason)
    except ActionChallengeError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    except BotRuntimeControlError as error:
        raise _control_error(error) from error
    return Response(status_code=303, headers={"Location": f"/admin/bots/{bot_id}"})


@router.post(
    "/workspace/kill",
    dependencies=[permission_route(Permission.WORKSPACE_KILL, csrf=CsrfMode.FORM)],
)
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


@router.post(
    "/workspace/release",
    dependencies=[permission_route(Permission.WORKSPACE_KILL, csrf=CsrfMode.FORM)],
)
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
        with http_step_up_scope(request, form_values=values):
            await _operations(request, identity).release_workspace(reason, expected_revision)
    except ActionChallengeError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    except BotRuntimeControlError as error:
        raise _control_error(error) from error
    return Response(status_code=303, headers={"Location": "/admin"})


@router.get(
    "/flatten",
    dependencies=[owner_route(denied_detail="Chỉ owner được flatten workspace.")],
)
async def flatten_page(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Hiển thị form preview PAPER; service sẽ xác minh owner và MFA gần đây."""
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


@router.post(
    "/flatten/preview",
    dependencies=[
        owner_route(csrf=CsrfMode.FORM, denied_detail="Chỉ owner được flatten workspace.")
    ],
)
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


@router.post(
    "/flatten/confirm",
    dependencies=[
        owner_route(csrf=CsrfMode.FORM, denied_detail="Chỉ owner được flatten workspace.")
    ],
)
async def flatten_confirm_form(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    try:
        with http_step_up_scope(request, form_values=values):
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
    except ActionChallengeError as error:
        raise AdminHTTPException(
            error.status_code, error.message, reason_code=error.code
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Preview flatten-all không hợp lệ.") from error
    except FlattenAllError as error:
        if error.reason_code == "STEP_UP_SERVICE_UNAVAILABLE":
            status = 503
        elif error.reason_code.startswith("STEP_UP_"):
            status = 403
        else:
            status = {
                "CONTROL_PERMISSION_DENIED": 403,
                "RECENT_MFA_REQUIRED": 403,
                "FLATTEN_PREVIEW_STALE": 409,
                "FLATTEN_PREVIEW_EXPIRED": 409,
                "FLATTEN_IDEMPOTENCY_KEY_REUSED": 409,
            }.get(error.reason_code, 422)
        raise AdminHTTPException(
            status, "Xác nhận flatten-all bị từ chối.", reason_code=error.reason_code
        ) from error
    location = f"/admin/flatten/operations/{result.operation_id}"
    if result.pause_failures:
        # Chỉ truyền số lượng (không ID bot) để trang operation cảnh báo bot chưa tạm dừng.
        location += f"?pause_failed={len(result.pause_failures)}"
    return Response(status_code=303, headers={"Location": location})


@router.get(
    "/flatten/operations/{operation_id}",
    dependencies=[owner_route(denied_detail="Chỉ owner được xem operation flatten.")],
)
async def flatten_operation_page(
    operation_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    pause_failed: Annotated[int, Query(ge=0, le=1000)] = 0,
) -> Response:
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
        context={"identity": identity, "operation": operation, "pause_failed": pause_failed},
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/telegram",
    dependencies=[owner_route(denied_detail="Không đủ quyền quản lý Telegram.")],
)
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


@router.post(
    "/telegram/link",
    dependencies=[
        owner_route(csrf=CsrfMode.FORM, denied_detail="Không đủ quyền quản lý Telegram.")
    ],
)
async def telegram_link_form(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    try:
        with http_step_up_scope(request, form_values=values):
            link = await _telegram_binding_service(request).create_link(
                identity.principal, reason=values.get("reason", "")
            )
        csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    except ActionChallengeError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
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


@router.post(
    "/telegram/revoke/{membership_id}",
    dependencies=[
        owner_route(csrf=CsrfMode.FORM, denied_detail="Không đủ quyền quản lý Telegram.")
    ],
)
async def telegram_revoke_form(
    membership_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    try:
        with http_step_up_scope(request, form_values=values):
            await _telegram_binding_service(request).revoke(
                identity.principal, membership_id, reason=values.get("reason", "")
            )
    except ActionChallengeError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    except ValueError as error:
        status = 403 if "owner" in str(error).lower() or "mfa" in str(error).lower() else 422
        raise HTTPException(
            status_code=status, detail="Không thể thu hồi liên kết Telegram."
        ) from error
    return Response(status_code=303, headers={"Location": "/admin/telegram"})


_TOPIC_SETUP_FAILED = (
    "Telegram không tạo được topic. Hãy bật chế độ topic (Threaded Mode) cho bot trong "
    "@BotFather rồi thử lại; tin vẫn đi vào chat chính như trước."
)


@router.post(
    "/telegram/topics/{membership_id}",
    dependencies=[
        owner_route(csrf=CsrfMode.FORM, denied_detail="Không đủ quyền quản lý Telegram.")
    ],
)
async def telegram_topics_form(
    membership_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """TG-4: tạo topic "Tín hiệu" và "PAPER report" trong chat riêng của binding."""
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    service = _telegram_binding_service(request)
    try:
        if values.get("action") == "clear":
            await service.clear_topics(identity.principal, membership_id)
        else:
            await service.create_topics(identity.principal, membership_id)
    except TelegramTopicSetupError as error:
        raise HTTPException(status_code=502, detail=_TOPIC_SETUP_FAILED) from error
    except ValueError as error:
        status = 403 if "owner" in str(error).lower() or "mfa" in str(error).lower() else 422
        raise HTTPException(
            status_code=status, detail="Không thể cấu hình topic Telegram."
        ) from error
    return Response(status_code=303, headers={"Location": "/admin/telegram"})


@router.get(
    "/reports",
    dependencies=[
        permission_route(
            Permission.WORKSPACE_READ,
            bot_scope=BotScopePolicy(
                BotPermission.REPORT_READ, param_name="bot_id", param_location="query"
            ),
        )
    ],
)
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
    if chart_trade_id and chart_trade_id.strip():
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

        # Phân tích Post-Mortem và rủi ro cho danh sách trades
        if report and "trades" in report and isinstance(report["trades"], list):
            plans = (
                await _approved_trade_plans(service, query, identity.principal, report["trades"])
                if report_scope is ReportScope.BOT
                else {}
            )
            for trade in report["trades"]:
                pm = evaluate_trade_post_mortem(trade, plan=plans.get(str(trade.get("trade_id"))))
                trade["post_mortem"] = pm.as_dict()

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
                if chart_data and "trade" in chart_data:
                    # Kế hoạch đã duyệt là nguồn thật cho SL/TP, đòn bẩy và ngân sách rủi ro.
                    plan = (
                        await _approved_trade_plans(
                            service, query, identity.principal, [chart_data["trade"]]
                        )
                    ).get(str(parsed_chart_trade_id))
                    pm = evaluate_trade_post_mortem(chart_data["trade"], plan=plan)
                    chart_data["trade"]["post_mortem"] = pm.as_dict()
                    chart_data["trade"]["plan"] = plan
            except BotReportError as error:
                raise HTTPException(
                    status_code=error.status_code,
                    detail={"code": error.code, "message": error.message},
                ) from error
    try:
        bots = await _admin_service(request).list_bots(identity.principal)
    except BotAdminError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    # Token CSRF chỉ cần cho nút gắn nhãn ca sai lệch; thiếu token thì nút bị máy chủ từ chối.
    try:
        csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    except AuthenticationFailure, HTTPException:
        csrf_token = ""
    templates = _templates(request)
    return templates.TemplateResponse(
        request=request,
        name="admin/reports.html",
        context={
            "identity": identity,
            "csrf_token": csrf_token,
            "bots": [_bot_view(bot) for bot in bots],
            "report": report,
            "chart_data": chart_data,
            "chart_timeframe": chart_timeframe,
            "start": start.isoformat() if start is not None else "",
            "end": end.isoformat() if end is not None else "",
            "scope": scope or "bot",
            "bot_id": str(parsed_bot_id or ""),
        },
        headers={"Cache-Control": "no-store"},
    )


# Cửa sổ mặc định khi client không gửi thời điểm đóng: khớp trang chi tiết bot (30 ngày).
RETRAINING_LABEL_DEFAULT_WINDOW = timedelta(days=30)


async def _ledger_trade_for_label(
    request: Request,
    principal: Principal,
    bot_id: UUID,
    trade_id: UUID,
    closed_at_hint: object,
) -> dict[str, Any]:
    """Tìm giao dịch đã đóng trong báo cáo ledger của bot; 404 nếu không có trong ledger."""
    end = datetime.now(UTC)
    start = end - RETRAINING_LABEL_DEFAULT_WINDOW
    if isinstance(closed_at_hint, str) and closed_at_hint:
        try:
            closed_at = datetime.fromisoformat(closed_at_hint)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="closed_at không hợp lệ.") from error
        if closed_at.tzinfo is None:
            raise HTTPException(status_code=422, detail="closed_at phải có múi giờ.")
        # Báo cáo lọc theo giờ đóng trong [start, end): cửa sổ hẹp quanh gợi ý của client.
        start, end = closed_at - timedelta(seconds=1), closed_at + timedelta(seconds=1)
    try:
        report = await _reporting_service(request).get_bot_report(
            BotReportQuery(
                workspace_id=principal.workspace_id,
                bot_id=bot_id,
                start=start,
                end=end,
                scope=ReportScope.BOT,
            ),
            principal,
        )
    except BotReportError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    trades = report.as_dict().get("trades", [])
    trade = next(
        (row for row in trades if isinstance(row, dict) and row.get("trade_id") == str(trade_id)),
        None,
    )
    if trade is None:
        raise HTTPException(
            status_code=404, detail="Không tìm thấy giao dịch đã đóng trong ledger."
        )
    return trade


def _holding_duration_seconds(trade: dict[str, Any]) -> int | None:
    try:
        opened = datetime.fromisoformat(str(trade["opened_at"]))
        closed = datetime.fromisoformat(str(trade["closed_at"]))
    except KeyError, ValueError:
        return None
    return max(0, int((closed - opened).total_seconds()))


@router.post(
    "/api/trades/{trade_id}/ingest-negative-sample",
    dependencies=[
        roles_route(
            *RETRAINING_LABEL_ROLES,
            csrf=CsrfMode.HEADER,
            denied_detail="Vai trò hiện tại không được gắn nhãn ca.",
        )
    ],
)
async def ingest_trade_negative_sample(
    request: Request,
    response: Response,
    trade_id: UUID,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    reason: str | None = None,
) -> dict[str, Any]:
    """Gắn nhãn một ca giao dịch sai lệch/thua lỗ để rà soát cho huấn luyện lại.

    Thao tác đổi trạng thái nên bắt buộc CSRF và vai trò được phép rà soát. Dữ liệu ca lấy từ
    ledger PAPER theo `trade_id` (không tin dữ liệu client). Bản ghi hiện chỉ nằm trong bộ nhớ
    tiến trình; chưa nối vào dataset WP06 và mất khi khởi động lại.
    """
    response.headers["Cache-Control"] = "no-store"
    if not identity.principal.roles & RETRAINING_LABEL_ROLES:
        raise HTTPException(status_code=403, detail="Vai trò hiện tại không được gắn nhãn ca.")
    service = _learning_post_mortem_service(request)
    body: dict[str, Any] = {}
    try:
        parsed_body = await request.json()
    except ValueError:
        parsed_body = {}
    if isinstance(parsed_body, dict):
        body = parsed_body
    try:
        bot_id = UUID(str(body.get("bot_id")))
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Thiếu mã bot hợp lệ của giao dịch.") from error
    override_reason = reason or body.get("reason")
    # Dữ liệu ca (hướng lệnh, PnL, chi phí, fill, lý do) đọc từ ledger PAPER, không lấy từ client:
    # client chỉ gửi mã bot, lý do và gợi ý thời điểm đóng để chọn cửa sổ báo cáo.
    ledger_trade = await _ledger_trade_for_label(
        request, identity.principal, bot_id, trade_id, body.get("closed_at")
    )
    trade_payload: dict[str, Any] = {"trade_id": str(trade_id), "bot_id": str(bot_id)}
    for key in RETRAINING_LABEL_TRADE_FIELDS:
        if ledger_trade.get(key) is not None:
            trade_payload[key] = ledger_trade[key]
    holding = _holding_duration_seconds(ledger_trade)
    if holding is not None:
        trade_payload["holding_duration_seconds"] = holding
    try:
        sample = service.ingest_trade_for_ai_retraining(
            trade_payload,
            override_reason=override_reason,
            ingested_by=identity.email,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=422, detail="Thiếu cặp giao dịch hoặc hướng lệnh của ca cần gắn nhãn."
        ) from error
    return {
        "status": "success",
        "message": (
            "Đã tiếp nhận ca sai lệch để rà soát; nhãn chỉ lưu tạm trong phiên chạy, "
            "chưa vào dữ liệu huấn luyện."
        ),
        "sample": sample.as_dict(),
    }


@router.post(
    "/api/trades/{trade_id}/queue-ai-retraining",
    dependencies=[
        roles_route(
            *RETRAINING_LABEL_ROLES,
            csrf=CsrfMode.HEADER,
            denied_detail="Vai trò hiện tại không được gắn nhãn ca.",
        )
    ],
)
async def queue_trade_ai_retraining(
    request: Request,
    response: Response,
    trade_id: UUID,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    reason: str | None = None,
) -> dict[str, Any]:
    """Gắn nhãn và đưa ca giao dịch sai lệch vào hàng đợi huấn luyện lại AI (alias)."""
    return await ingest_trade_negative_sample(
        request=request,
        response=response,
        trade_id=trade_id,
        identity=identity,
        reason=reason,
    )


_RELEASE_NOTICE_MESSAGES: dict[str, str] = {
    "published": "Đã công bố bản phát hành chiến lược trong không gian làm việc.",
    "retired": "Đã thu hồi bản phát hành chiến lược khỏi trạng thái hoạt động.",
}


async def _issue_optional_csrf_token(request: Request) -> str:
    try:
        return await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    except AuthenticationFailure, HTTPException:
        return ""


async def _render_strategy_cards_page(
    request: Request,
    identity: AuthIdentity,
    *,
    status_code: int = 200,
    active_tab: str = "tab-cards",
    release_notice: str | None = None,
    release_error: str | None = None,
) -> Response:
    card_service = _strategy_card_service(request)
    release_service = _strategy_release_service(request)
    cards = await card_service.list_strategy_cards(identity.principal)
    try:
        releases = await release_service.list_releases(identity.principal)
    except StrategyReleaseAdminError:
        releases = []
    csrf_token = await _issue_optional_csrf_token(request)
    templates = _templates(request)
    return templates.TemplateResponse(
        request=request,
        name="admin/strategy_cards.html",
        context={
            "identity": identity,
            "cards": cards,
            "total_cards": len(cards),
            "active_cards_count": sum(1 for c in cards if c.is_active),
            "verified_cards_count": sum(
                1 for c in cards if c.is_verified and c.consensus_status == "ok"
            ),
            "releases": releases,
            "published_releases_count": sum(1 for r in releases if r.is_published),
            "retired_releases_count": sum(1 for r in releases if r.is_retired),
            "unpublished_releases_count": sum(
                1 for r in releases if r.workspace_status in {"UNPUBLISHED", "HASH_MISMATCH"}
            ),
            "can_manage_releases": Role.OWNER in identity.principal.roles,
            "csrf_token": csrf_token,
            "active_tab": active_tab,
            "release_notice": release_notice,
            "release_error": release_error,
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/strategy-cards", dependencies=[permission_route(Permission.WORKSPACE_READ)])
async def strategy_cards_page(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    release_status: str | None = None,
    release_id: str | None = None,
) -> Response:
    """Hiển thị danh sách Strategy Cards, các tầng L0-L6, kiểm định và Quản lý Phát hành."""
    response.headers["Cache-Control"] = "no-store"
    notice: str | None = None
    active_tab = "tab-cards"
    if release_status in _RELEASE_NOTICE_MESSAGES:
        active_tab = "tab-releases"
        prefix = _RELEASE_NOTICE_MESSAGES[release_status]
        safe_id = (release_id or "").strip()
        notice = f"{prefix} ({safe_id})" if safe_id else prefix
    return await _render_strategy_cards_page(
        request,
        identity,
        active_tab=active_tab,
        release_notice=notice,
    )


@router.post(
    "/strategy-releases/publish",
    dependencies=[owner_route(csrf=CsrfMode.FORM)],
)
async def publish_strategy_release_form(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Owner công bố một `strategy_release` từ catalog vào workspace (MFA + lý do + audit)."""
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    release_id = values.get("release_id", "").strip()
    reason = values.get("reason", "")
    expected_hash = values["content_hash"] if "content_hash" in values else None
    try:
        with http_step_up_scope(request, form_values=values):
            updated = await _strategy_release_service(request).publish_release(
                identity.principal,
                release_id=release_id,
                reason=reason,
                expected_content_hash=expected_hash,
            )
    except ActionChallengeError as error:
        return await _render_strategy_cards_page(
            request,
            identity,
            status_code=error.status_code,
            active_tab="tab-releases",
            release_error=error.message,
        )
    except StrategyReleaseAdminError as error:
        return await _render_strategy_cards_page(
            request,
            identity,
            status_code=error.status_code,
            active_tab="tab-releases",
            release_error=error.message,
        )
    query = urlencode({"release_status": "published", "release_id": updated.release_id})
    return Response(
        status_code=303,
        headers={
            "Location": f"/admin/strategy-cards?{query}#tab-releases",
            "Cache-Control": "no-store",
        },
    )


@router.post(
    "/strategy-releases/retire",
    dependencies=[owner_route(csrf=CsrfMode.FORM)],
)
async def retire_strategy_release_form(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Owner thu hồi một `strategy_release` khỏi workspace (MFA + lý do + audit)."""
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    release_id = values.get("release_id", "").strip()
    reason = values.get("reason", "")
    expected_hash = values["content_hash"] if "content_hash" in values else None
    try:
        with http_step_up_scope(request, form_values=values):
            updated = await _strategy_release_service(request).retire_release(
                identity.principal,
                release_id=release_id,
                reason=reason,
                expected_content_hash=expected_hash,
            )
    except ActionChallengeError as error:
        return await _render_strategy_cards_page(
            request,
            identity,
            status_code=error.status_code,
            active_tab="tab-releases",
            release_error=error.message,
        )
    except StrategyReleaseAdminError as error:
        return await _render_strategy_cards_page(
            request,
            identity,
            status_code=error.status_code,
            active_tab="tab-releases",
            release_error=error.message,
        )
    query = urlencode({"release_status": "retired", "release_id": updated.release_id})
    return Response(
        status_code=303,
        headers={
            "Location": f"/admin/strategy-cards?{query}#tab-releases",
            "Cache-Control": "no-store",
        },
    )


MARKET_CHART_DEFAULT_TIMEFRAME = "5m"
_CHART_SYMBOL_PATTERN = re.compile(r"[A-Z0-9_]{2,32}")


def _chart_symbol(value: str | None) -> str | None:
    """Symbol hợp lệ theo quy ước cấu hình (chữ hoa/số/gạch dưới) hoặc ``None``."""
    if value is None or not value.strip():
        return None
    symbol = value.strip().upper()
    if not _CHART_SYMBOL_PATTERN.fullmatch(symbol):
        raise HTTPException(status_code=422, detail="Mã giao dịch (symbol) không hợp lệ.")
    return symbol


def _report_http_error(error: BotReportError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )


@router.get(
    "/chart",
    dependencies=[
        permission_route(
            Permission.WORKSPACE_READ,
            bot_scope=BotScopePolicy(
                BotPermission.REPORT_READ, param_name="bot_id", param_location="query"
            ),
        )
    ],
)
async def market_chart_page(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    bot_id: str | None = None,
    symbol: str | None = None,
    timeframe: str = MARKET_CHART_DEFAULT_TIMEFRAME,
) -> Response:
    """Chart thị trường kiểu TradingView (SIG-7) từ nến đã lưu; chỉ đọc, không gửi lệnh.

    Quyền xem theo bot đang chọn (``REPORT_READ``); danh sách mã/khung là những gì thực sự
    có nến đã đóng trong kho nội bộ của workspace — không liệt kê mã không có dữ liệu.
    """
    if timeframe not in MARKET_CHART_INTERVAL_SECONDS:
        raise HTTPException(status_code=422, detail="Khung biểu đồ không được hỗ trợ.")
    requested_symbol = _chart_symbol(symbol)
    try:
        bots = await _admin_service(request).list_bots(identity.principal)
    except BotAdminError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    bot_views = [_bot_view(bot) for bot in bots]
    selected: AdminBotView | None = None
    if bot_id and bot_id.strip():
        try:
            parsed = UUID(bot_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="bot_id phải là UUID hợp lệ.") from error
        selected = next((view for view in bot_views if view.id == str(parsed)), None)
        if selected is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy bot.")
    elif bot_views:
        selected = bot_views[0]
    series: dict[str, list[str]] = {}
    selected_symbol: str | None = None
    payload: dict[str, Any] | None = None
    signal_view: dict[str, Any] | None = None
    overview: dict[str, Any] | None = None
    setups: list[dict[str, Any]] = []
    insights: dict[str, Any] | None = None
    if selected is not None:
        service = _reporting_service(request)
        try:
            series = await service.list_market_series(identity.principal, bot_id=UUID(selected.id))
            if requested_symbol is not None:
                if requested_symbol not in series:
                    raise HTTPException(
                        status_code=404, detail="Chưa có nến đã lưu cho mã giao dịch này."
                    )
                selected_symbol = requested_symbol
            elif selected.symbol and selected.symbol in series:
                selected_symbol = selected.symbol
            elif series:
                selected_symbol = next(iter(series))
            if selected_symbol is not None:
                bars = await service.get_market_bars(
                    identity.principal,
                    bot_id=UUID(selected.id),
                    symbol=selected_symbol,
                    timeframe=timeframe,
                    limit=MARKET_CHART_DEFAULT_LIMIT,
                )
                payload = build_market_chart_payload(
                    symbol=selected_symbol, timeframe=timeframe, bars=bars
                )
            signal_view = await service.get_latest_signal(
                identity.principal, bot_id=UUID(selected.id)
            )
            if payload is not None:
                payload["signal_card"] = _card_for_symbol(signal_view, selected_symbol)
            if selected_symbol is not None:
                overview = await service.get_market_overview(
                    identity.principal,
                    bot_id=UUID(selected.id),
                    symbol=selected_symbol,
                    timeframe=timeframe,
                )
                # CK-6: bảng Entry — setup của các bot giao dịch mã này (hoặc chưa ghim mã).
                calendar_events = await _calendar_events(request, identity.principal)
                setups = await service.get_setup_board(
                    identity.principal,
                    bots=[
                        (UUID(view.id), view.name)
                        for view in bot_views
                        if view.symbol in (None, selected_symbol)
                    ],
                    bonus=context_conditions(
                        datetime.now(UTC),
                        next_funding_time=_next_funding(overview),
                        calendar_events=calendar_events,
                    ),
                )
                # CK-12: trạng thái vận hành từng bot (cùng nguồn với trang chi tiết bot).
                for item in setups:
                    item["runtime"] = await _runtime_status(request, identity, UUID(item["bot_id"]))
                # CK-10: cột phân tích — nhận định, mức giá, lịch, thống kê lệnh đã đóng.
                insights = {
                    "narrative": market_narrative(payload or {}, overview),
                    "levels": price_levels(
                        payload or {},
                        current_price=((overview or {}).get("candle") or {}).get("price"),
                    )[:20],
                    "calendar": [
                        event
                        for event in calendar_events or []
                        if datetime.fromisoformat(event["event_at"]) >= datetime.now(UTC)
                    ][:10],
                    "calendar_available": calendar_events is not None,
                    "statistics": trade_statistics(
                        await service.get_trade_history(
                            identity.principal,
                            bot_id=UUID(selected.id),
                            symbol=selected_symbol,
                            start=datetime.now(UTC) - STATISTICS_WINDOW,
                            end=datetime.now(UTC),
                        )
                    ),
                }
        except BotReportError as error:
            raise _report_http_error(error) from error
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/market_chart.html",
        context={
            "identity": identity,
            "bots": bot_views,
            "selected_bot": selected,
            "series": series,
            "symbol_choices": coin_choices(
                (selected_symbol,) if selected_symbol else (), only=tuple(series)
            ),
            "selected_symbol": selected_symbol,
            "available_timeframes": series.get(selected_symbol or "", []),
            "timeframe": timeframe,
            "timeframes": list(MARKET_CHART_INTERVAL_SECONDS),
            "chart_payload": payload,
            "signal_view": signal_view,
            "overview": overview,
            "setups": setups,
            "insights": insights,
            "setup_cards": [
                {"bot_name": item["bot_name"], "label": item["label"], "card": item["card"]}
                for item in setups
                if item.get("card") and item["card"].get("symbol") == selected_symbol
            ],
        },
        headers={"Cache-Control": "no-store"},
    )


STATISTICS_WINDOW = timedelta(days=90)


async def _runtime_status(
    request: Request, identity: AuthIdentity, bot_id: UUID
) -> dict[str, Any] | None:
    """Trạng thái vận hành của bot cho bảng giám sát (CK-12); ``None`` khi không đọc được."""
    service = getattr(request.app.state, "bot_operations_service", None)
    if service is None:
        return None
    try:
        status = await service.for_principal(identity.principal).bot_status(bot_id)
    except Exception:  # noqa: BLE001 - bảng giám sát chỉ đọc, lỗi một bot không làm hỏng trang
        return None
    return dict(status.as_dict())


async def _calendar_events(request: Request, principal: Principal) -> list[dict[str, Any]] | None:
    """Lịch nội bộ ±7 ngày; ``None`` khi dịch vụ lịch chưa sẵn sàng hoặc không đọc được."""
    service = getattr(request.app.state, "economic_calendar_service", None)
    if service is None:
        return None
    try:
        events: list[dict[str, Any]] = await service.around(principal, now=datetime.now(UTC))
    except EconomicCalendarError:
        return None
    return events


def _next_funding(overview: dict[str, Any] | None) -> datetime | None:
    """Mốc funding kế tiếp từ khối mark của overview (CK-1); ``None`` khi chưa có mark."""
    mark = (overview or {}).get("mark") or {}
    raw = mark.get("next_funding_time")
    return datetime.fromisoformat(raw) if raw else None


def _card_for_symbol(
    signal_view: dict[str, Any] | None, symbol: str | None
) -> dict[str, Any] | None:
    """Chỉ vẽ thẻ lên chart khi tín hiệu cùng mã đang xem và còn hiệu lực."""
    card: dict[str, Any] | None = (signal_view or {}).get("card")
    if not card or card.get("symbol") != symbol or card.get("is_expired", True):
        return None
    return card


_CHART_API_POLICY = bot_scoped_route(BotPermission.REPORT_READ, param_location="query")


@router.get("/api/chart", dependencies=[_CHART_API_POLICY])
async def market_chart_api(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    bot_id: UUID,
    symbol: str,
    timeframe: str = MARKET_CHART_DEFAULT_TIMEFRAME,
    limit: Annotated[int, Query(ge=50, le=MARKET_CHART_MAX_LIMIT)] = MARKET_CHART_DEFAULT_LIMIT,
    fp: str | None = None,
) -> dict[str, Any]:
    """JSON chart cho polling 5 giây; ``fp`` trùng thì chỉ trả ``status: unchanged``."""
    response.headers["Cache-Control"] = "no-store"
    if timeframe not in MARKET_CHART_INTERVAL_SECONDS:
        raise HTTPException(status_code=422, detail="Khung biểu đồ không được hỗ trợ.")
    chart_symbol = _chart_symbol(symbol)
    if chart_symbol is None:
        raise HTTPException(status_code=422, detail="Thiếu mã giao dịch (symbol).")
    try:
        bars = await _reporting_service(request).get_market_bars(
            identity.principal,
            bot_id=bot_id,
            symbol=chart_symbol,
            timeframe=timeframe,
            limit=limit,
        )
    except BotReportError as error:
        raise _report_http_error(error) from error
    fingerprint = chart_fingerprint(chart_symbol, timeframe, bars)
    if fp is not None and fp == fingerprint:
        return {"status": "unchanged", "fingerprint": fingerprint}
    payload = build_market_chart_payload(symbol=chart_symbol, timeframe=timeframe, bars=bars)
    try:
        signal_view = await _reporting_service(request).get_latest_signal(
            identity.principal, bot_id=bot_id
        )
    except BotReportError as error:
        raise _report_http_error(error) from error
    payload["signal_card"] = _card_for_symbol(signal_view, chart_symbol)
    return {"status": "ok"} | payload


@router.get("/api/chart/overview", dependencies=[_CHART_API_POLICY])
async def market_overview_api(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    bot_id: UUID,
    symbol: str,
    timeframe: str = MARKET_CHART_DEFAULT_TIMEFRAME,
) -> dict[str, Any]:
    """Dải thông số (CK-1) + ma trận đa khung (CK-2) cho polling ~2 giây; chỉ đọc."""
    response.headers["Cache-Control"] = "no-store"
    if timeframe not in MARKET_CHART_INTERVAL_SECONDS:
        raise HTTPException(status_code=422, detail="Khung biểu đồ không được hỗ trợ.")
    chart_symbol = _chart_symbol(symbol)
    if chart_symbol is None:
        raise HTTPException(status_code=422, detail="Thiếu mã giao dịch (symbol).")
    try:
        overview = await _reporting_service(request).get_market_overview(
            identity.principal, bot_id=bot_id, symbol=chart_symbol, timeframe=timeframe
        )
    except BotReportError as error:
        raise _report_http_error(error) from error
    return {"status": "ok"} | overview


@router.get("/api/chart/decision", dependencies=[_CHART_API_POLICY])
async def market_bar_decision_api(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    bot_id: UUID,
    time: Annotated[int, Query(ge=0)],
    timeframe: str = MARKET_CHART_DEFAULT_TIMEFRAME,
) -> dict[str, Any]:
    """Phân tích tại nến (CK-7): quyết định của bot được đánh giá khi nến ``time`` đóng."""
    response.headers["Cache-Control"] = "no-store"
    if timeframe not in MARKET_CHART_INTERVAL_SECONDS:
        raise HTTPException(status_code=422, detail="Khung biểu đồ không được hỗ trợ.")
    interval = MARKET_CHART_INTERVAL_SECONDS[timeframe]
    if time % interval:
        raise HTTPException(status_code=422, detail="Thời điểm không trùng mốc mở nến.")
    bar_open = datetime.fromtimestamp(time, UTC)
    try:
        decision = await _reporting_service(request).get_decision_at(
            identity.principal,
            bot_id=bot_id,
            bar_open=bar_open,
            interval=timedelta(seconds=interval),
        )
    except BotReportError as error:
        raise _report_http_error(error) from error
    return {
        "status": "ok",
        "bar_open": bar_open.isoformat(),
        "timeframe": timeframe,
        "decision": decision,
    }


@router.get("/api/chart/validate", dependencies=[_CHART_API_POLICY])
async def market_chart_validate_api(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    bot_id: UUID,
    symbol: str,
    timeframe: str = MARKET_CHART_DEFAULT_TIMEFRAME,
) -> dict[str, Any]:
    """Nút "Kiểm dữ liệu" (CK-9): thiếu nến, trùng, OHLC sai, trễ, khoảng trống ingest."""
    response.headers["Cache-Control"] = "no-store"
    if timeframe not in MARKET_CHART_INTERVAL_SECONDS:
        raise HTTPException(status_code=422, detail="Khung biểu đồ không được hỗ trợ.")
    chart_symbol = _chart_symbol(symbol)
    if chart_symbol is None:
        raise HTTPException(status_code=422, detail="Thiếu mã giao dịch (symbol).")
    try:
        report = await _reporting_service(request).validate_chart(
            identity.principal, bot_id=bot_id, symbol=chart_symbol, timeframe=timeframe
        )
    except BotReportError as error:
        raise _report_http_error(error) from error
    return {"status": "ok"} | report


@router.get("/api/chart/decisions", dependencies=[_CHART_API_POLICY])
async def market_chart_decisions_api(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    bot_id: UUID,
    offset: int = Query(default=0, ge=0, le=10000),
) -> dict[str, Any]:
    """Trang nhật ký 20 quyết định của bot (CK-9), chỉ quyết định khớp hash."""
    response.headers["Cache-Control"] = "no-store"
    try:
        log = await _reporting_service(request).get_decision_log(
            identity.principal, bot_id=bot_id, limit=20, offset=offset
        )
    except BotReportError as error:
        raise _report_http_error(error) from error
    return {"status": "ok"} | log


_HISTORY_PERIODS = {"today": None, "7d": timedelta(days=7), "30d": timedelta(days=30)}


@router.get("/api/chart/history", dependencies=[_CHART_API_POLICY])
async def market_chart_history_api(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    bot_id: UUID,
    symbol: str,
    period: str = "7d",
) -> dict[str, Any]:
    """Lịch sử lệnh PAPER đã đóng của mã đang xem (CK-9) kèm link xuất CSV có sẵn."""
    response.headers["Cache-Control"] = "no-store"
    if period not in _HISTORY_PERIODS:
        raise HTTPException(status_code=422, detail="Kỳ lịch sử chỉ nhận today, 7d hoặc 30d.")
    chart_symbol = _chart_symbol(symbol)
    if chart_symbol is None:
        raise HTTPException(status_code=422, detail="Thiếu mã giao dịch (symbol).")
    end = datetime.now(UTC)
    window = _HISTORY_PERIODS[period]
    start = (
        end.replace(hour=0, minute=0, second=0, microsecond=0) if window is None else end - window
    )
    try:
        trades = await _reporting_service(request).get_trade_history(
            identity.principal, bot_id=bot_id, symbol=chart_symbol, start=start, end=end
        )
    except BotReportError as error:
        raise _report_http_error(error) from error
    csv_query = urlencode(
        {"start": start.isoformat(), "end": end.isoformat(), "scope": "bot", "bot_id": str(bot_id)}
    )
    return {
        "status": "ok",
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "trades": trades,
        "csv_url": f"/admin/reports/export.csv?{csv_query}",
    }


def _analyst_review(request: Request) -> AnalystReviewService:
    service = cast(
        AnalystReviewService | None, getattr(request.app.state, "analyst_review_service", None)
    )
    if service is None:
        raise HTTPException(status_code=503, detail="Trang phân tích AI chưa sẵn sàng.")
    return service


@router.get("/ai-analyses", dependencies=[permission_route(Permission.WORKSPACE_READ)])
async def ai_analyses_page(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    topic: str | None = None,
    valid: str | None = None,
) -> Response:
    """CRY-383: danh sách lượt AI Analyst (chỉ đọc), lọc theo chủ đề và kết quả validator."""
    valid_filter = {"yes": True, "no": False}.get(valid or "")
    try:
        data = await _analyst_review(request).list_runs(
            identity.principal, topic=topic or None, valid=valid_filter
        )
    except AnalystReviewError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/ai_analyses.html",
        context={
            "identity": identity,
            "runs": data["runs"],
            "topics": data["topics"],
            "topic": topic or "",
            "valid": valid or "",
        },
        headers={"Cache-Control": "no-store"},
    )


def _signal_outcome_review(request: Request) -> SignalOutcomeReviewService:
    service = cast(
        SignalOutcomeReviewService | None,
        getattr(request.app.state, "signal_outcome_review_service", None),
    )
    if service is None:
        raise HTTPException(status_code=503, detail="Trang kết quả tín hiệu chưa sẵn sàng.")
    return service


@router.get("/signal-outcomes", dependencies=[permission_route(Permission.WORKSPACE_READ)])
async def signal_outcomes_page(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    bot: str | None = None,
    release: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    horizon: int = 4,
    period: str = "30",
) -> Response:
    """CRY-430: kết quả tín hiệu đã chấm (chỉ đọc), lọc theo bot/setup/coin/khung/horizon."""
    try:
        bot_id = UUID(bot) if bot else None
    except ValueError:
        bot_id = None
    try:
        data = await _signal_outcome_review(request).overview(
            identity.principal,
            bot_id=bot_id,
            release=(release or "")[:80] or None,
            symbol=(symbol or "")[:64] or None,
            timeframe=(timeframe or "")[:16] or None,
            horizon=horizon,
            period=period,
        )
    except SignalOutcomeReviewError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/signal_outcomes.html",
        context={"identity": identity, "data": data},
        headers={"Cache-Control": "no-store"},
    )


_REPORT_HASH = re.compile(r"^[0-9a-f]{64}$")


@router.get(
    "/run-reports/{report_hash}", dependencies=[permission_route(Permission.WORKSPACE_READ)]
)
async def run_report_page(
    request: Request,
    report_hash: str,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """CRY-380: Run Report đã lưu (bất biến) và các lượt AI đã đọc report này."""
    if not _REPORT_HASH.fullmatch(report_hash):
        raise HTTPException(status_code=404, detail="Không tìm thấy Run Report.")
    try:
        report = await _analyst_review(request).get_report(identity.principal, report_hash)
    except AnalystReviewError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/run_report.html",
        context={"identity": identity, "report": report},
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/run-reports/{report_hash}/report.json",
    dependencies=[permission_route(Permission.WORKSPACE_READ)],
)
async def run_report_json(
    request: Request,
    report_hash: str,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Tải nguyên văn JSON đã lưu (đúng dữ liệu AI đọc, không gắn bối cảnh)."""
    if not _REPORT_HASH.fullmatch(report_hash):
        raise HTTPException(status_code=404, detail="Không tìm thấy Run Report.")
    try:
        report = await _analyst_review(request).get_report(identity.principal, report_hash)
    except AnalystReviewError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    body = json.dumps(report["payload"], ensure_ascii=False, sort_keys=True, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="run-report-{report_hash[:12]}.json"',
        },
    )


@router.get("/ai-analyses/{run_id}", dependencies=[permission_route(Permission.WORKSPACE_READ)])
async def ai_analysis_detail(
    request: Request,
    run_id: UUID,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """CRY-383: một lượt AI Analyst — đầu ra, lỗi validator, bối cảnh, backtest đề xuất."""
    try:
        run = await _analyst_review(request).get_run(identity.principal, run_id)
    except AnalystReviewError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/ai_analysis_detail.html",
        context={"identity": identity, "run": run},
        headers={"Cache-Control": "no-store"},
    )


def _calendar_service(request: Request) -> EconomicCalendarService:
    service = cast(
        EconomicCalendarService | None,
        getattr(request.app.state, "economic_calendar_service", None),
    )
    if service is None:
        raise HTTPException(status_code=503, detail="Lịch sự kiện chưa sẵn sàng.")
    return service


def _calendar_http_error(error: EconomicCalendarError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.message)


@router.get("/calendar", dependencies=[permission_route(Permission.WORKSPACE_READ)])
async def economic_calendar_page(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    edit: UUID | None = None,
) -> Response:
    """Lịch sự kiện kinh tế do owner nhập (CK-10): 30 ngày tới và 7 ngày qua."""
    now = datetime.now(UTC)
    try:
        events = await _calendar_service(request).list_between(
            identity.principal, start=now - timedelta(days=7), end=now + timedelta(days=30)
        )
    except EconomicCalendarError as error:
        raise _calendar_http_error(error) from error
    csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    editing = next((event for event in events if event["id"] == str(edit)), None) if edit else None
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/economic_calendar.html",
        context={
            "identity": identity,
            "csrf_token": csrf_token,
            "events": events,
            "editing": editing,
            "can_write": authorize(identity.principal, Permission.SETTINGS_WRITE),
            "impacts": [(key, IMPACT_LABELS_VI[key]) for key in ("HIGH", "MEDIUM", "LOW")],
            "timezones": CALENDAR_TIMEZONES,
            "editing_local": (
                datetime.fromisoformat(editing["event_at"])
                .astimezone(ZoneInfo("Asia/Ho_Chi_Minh"))
                .strftime("%Y-%m-%dT%H:%M")
                if editing
                else ""
            ),
            "now": now,
        },
        headers={"Cache-Control": "no-store"},
    )


CALENDAR_TIMEZONES = {
    "Asia/Ho_Chi_Minh": "Giờ Việt Nam (UTC+7)",
    "America/New_York": "Giờ New York (theo DST)",
    "UTC": "UTC",
}


def _calendar_input(values: dict[str, str]) -> EconomicEventInput:
    """Ghép ô ``datetime-local`` (không múi giờ) với múi giờ đã chọn thành thời điểm có múi giờ."""
    zone_name = values.get("event_tz", "")
    if zone_name not in CALENDAR_TIMEZONES:
        raise HTTPException(status_code=422, detail="Múi giờ không hợp lệ.")
    try:
        local = datetime.fromisoformat(values.get("event_local", "").strip())
        if local.tzinfo is not None:
            raise ValueError("Thời điểm nhập không được kèm múi giờ.")
        moment = local.replace(tzinfo=ZoneInfo(zone_name))
        return EconomicEventInput.parse(
            title=values.get("title", ""),
            region=values.get("region", ""),
            impact=values.get("impact", ""),
            event_at=moment.isoformat(),
            note=values.get("note"),
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def _calendar_redirect() -> Response:
    return Response(
        status_code=303, headers={"Location": "/admin/calendar", "Cache-Control": "no-store"}
    )


def _calendar_reason(values: dict[str, str]) -> str:
    reason = values.get("reason", "").strip()
    if not 3 <= len(reason) <= 300:
        raise HTTPException(status_code=422, detail="Lý do thay đổi cần 3–300 ký tự.")
    return reason


@router.post(
    "/calendar",
    dependencies=[
        permission_route(
            Permission.SETTINGS_WRITE,
            csrf=CsrfMode.FORM,
            denied_detail="Không đủ quyền chỉnh sửa lịch sự kiện kinh tế.",
        )
    ],
)
async def economic_calendar_create(
    request: Request, identity: Annotated[AuthIdentity, Depends(current_identity)]
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    data = _calendar_input(values)
    try:
        await _calendar_service(request).create(
            identity.principal, data, reason=_calendar_reason(values)
        )
    except EconomicCalendarError as error:
        raise _calendar_http_error(error) from error
    return _calendar_redirect()


@router.post(
    "/calendar/{event_id}",
    dependencies=[
        permission_route(
            Permission.SETTINGS_WRITE,
            csrf=CsrfMode.FORM,
            denied_detail="Không đủ quyền chỉnh sửa lịch sự kiện kinh tế.",
        )
    ],
)
async def economic_calendar_update(
    event_id: UUID, request: Request, identity: Annotated[AuthIdentity, Depends(current_identity)]
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    data = _calendar_input(values)
    try:
        await _calendar_service(request).update(
            identity.principal, event_id, data, reason=_calendar_reason(values)
        )
    except EconomicCalendarError as error:
        raise _calendar_http_error(error) from error
    return _calendar_redirect()


@router.post(
    "/calendar/{event_id}/delete",
    dependencies=[
        permission_route(
            Permission.SETTINGS_WRITE,
            csrf=CsrfMode.FORM,
            denied_detail="Không đủ quyền chỉnh sửa lịch sự kiện kinh tế.",
        )
    ],
)
async def economic_calendar_delete(
    event_id: UUID, request: Request, identity: Annotated[AuthIdentity, Depends(current_identity)]
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    try:
        await _calendar_service(request).delete(
            identity.principal, event_id, reason=_calendar_reason(values)
        )
    except EconomicCalendarError as error:
        raise _calendar_http_error(error) from error
    return _calendar_redirect()


SCANNER_VOLUME_CHOICES = (0, 1_000_000, 5_000_000, 10_000_000, 50_000_000, 100_000_000)
SCANNER_RANGE_MIN_CHOICES = (0, 5, 10, 20, 30)
SCANNER_RANGE_MAX_CHOICES = (20, 40, 60, 100, 1000)


def _scanner_service(request: Request) -> MarketScannerService:
    service = cast(
        MarketScannerService | None, getattr(request.app.state, "market_scanner_service", None)
    )
    if service is None:
        raise HTTPException(status_code=503, detail="Bộ quét chưa sẵn sàng.")
    return service


def _scanner_params(values: Mapping[str, str]) -> tuple[str, ScanCriteria]:
    """Tiêu chí chỉ nhận giá trị trong danh sách chọn (không nhập tự do)."""
    timeframe = values.get("timeframe", "4h")
    try:
        volume = int(values.get("min_volume", "1000000"))
        range_min = int(values.get("range_min", "0"))
        range_max = int(values.get("range_max", "1000"))
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Tiêu chí quét không hợp lệ.") from error
    if (
        timeframe not in SCAN_INTERVALS
        or volume not in SCANNER_VOLUME_CHOICES
        or range_min not in SCANNER_RANGE_MIN_CHOICES
        or range_max not in SCANNER_RANGE_MAX_CHOICES
        or range_min >= range_max
    ):
        raise HTTPException(status_code=422, detail="Tiêu chí quét không hợp lệ.")
    return timeframe, ScanCriteria(
        min_quote_volume_24h=float(volume),
        range_min_pct=float(range_min),
        range_max_pct=float(range_max),
    )


@router.get("/scanner", dependencies=[permission_route(Permission.WORKSPACE_READ)])
async def market_scanner_page(
    request: Request, identity: Annotated[AuthIdentity, Depends(current_identity)]
) -> Response:
    """Bộ quét thị trường (CK-11) trên nến đã lưu; chỉ xem, không gọi mạng sàn."""
    timeframe, criteria = _scanner_params(dict(request.query_params))
    service = _scanner_service(request)
    try:
        result = await service.scan(identity.principal, timeframe=timeframe, criteria=criteria)
        runs = await service.recent_runs(identity.principal)
    except MarketScannerError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    symbols = [row["symbol"] for row in result["rows"]] + [
        row["symbol"] for row in result["excluded"]
    ]
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/market_scanner.html",
        context={
            "identity": identity,
            "csrf_token": csrf_token,
            "result": result,
            "runs": runs,
            "timeframe": timeframe,
            "criteria": criteria,
            "volume_choices": SCANNER_VOLUME_CHOICES,
            "range_min_choices": SCANNER_RANGE_MIN_CHOICES,
            "range_max_choices": SCANNER_RANGE_MAX_CHOICES,
            "watch_choices": coin_choices((), only=tuple(sorted(set(symbols)))),
            "logged": request.query_params.get("logged") == "1",
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/scanner",
    dependencies=[permission_route(Permission.WORKSPACE_READ, csrf=CsrfMode.FORM)],
)
async def market_scanner_run(
    request: Request, identity: Annotated[AuthIdentity, Depends(current_identity)]
) -> Response:
    """Chạy quét và ghi nhật ký (audit) rồi quay về trang kết quả với cùng tiêu chí."""
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    timeframe, criteria = _scanner_params(values)
    try:
        await _scanner_service(request).scan(
            identity.principal, timeframe=timeframe, criteria=criteria, record=True
        )
    except MarketScannerError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    query = urlencode(
        {
            "timeframe": timeframe,
            "min_volume": int(criteria.min_quote_volume_24h),
            "range_min": int(criteria.range_min_pct),
            "range_max": int(criteria.range_max_pct),
            "logged": 1,
        }
    )
    return Response(
        status_code=303,
        headers={"Location": f"/admin/scanner?{query}", "Cache-Control": "no-store"},
    )


@router.get("/api/strategy-cards", dependencies=[permission_route(Permission.WORKSPACE_READ)])
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


@router.get("/api/strategy-releases", dependencies=[permission_route(Permission.WORKSPACE_READ)])
async def strategy_releases_api(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Trả về JSON danh sách release từ catalog kèm trạng thái hiện hành trong workspace."""
    response.headers["Cache-Control"] = "no-store"
    try:
        releases = await _strategy_release_service(request).list_releases(identity.principal)
    except StrategyReleaseAdminError as error:
        return JSONResponse(
            {"status": "error", "code": error.reason_code, "detail": error.message},
            status_code=error.status_code,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        {
            "status": "success",
            "total": len(releases),
            "releases": [r.to_dict() for r in releases],
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/api/strategy-releases/publish",
    dependencies=[owner_route(csrf=CsrfMode.HEADER, denied_code="CONTROL_PERMISSION_DENIED")],
)
async def publish_strategy_release_api(
    payload: StrategyReleaseActionRequest,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> Response:
    """API công bố một `strategy_release` từ catalog vào workspace (Owner + MFA + CSRF)."""
    try:
        with http_step_up_scope(request):
            updated = await _strategy_release_service(request).publish_release(
                identity.principal,
                release_id=payload.release_id,
                reason=payload.reason,
                expected_content_hash=payload.content_hash,
            )
    except ActionChallengeError as error:
        return JSONResponse(
            {"status": "error", "code": error.code, "detail": error.message},
            status_code=error.status_code,
            headers={"Cache-Control": "no-store"},
        )
    except StrategyReleaseAdminError as error:
        return JSONResponse(
            {"status": "error", "code": error.reason_code, "detail": error.message},
            status_code=error.status_code,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        {"status": "success", "release": updated.to_dict()},
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/api/strategy-releases/retire",
    dependencies=[owner_route(csrf=CsrfMode.HEADER, denied_code="CONTROL_PERMISSION_DENIED")],
)
async def retire_strategy_release_api(
    payload: StrategyReleaseActionRequest,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> Response:
    """API thu hồi một `strategy_release` trong workspace (Owner + MFA + CSRF)."""
    try:
        with http_step_up_scope(request):
            updated = await _strategy_release_service(request).retire_release(
                identity.principal,
                release_id=payload.release_id,
                reason=payload.reason,
                expected_content_hash=payload.content_hash,
            )
    except ActionChallengeError as error:
        return JSONResponse(
            {"status": "error", "code": error.code, "detail": error.message},
            status_code=error.status_code,
            headers={"Cache-Control": "no-store"},
        )
    except StrategyReleaseAdminError as error:
        return JSONResponse(
            {"status": "error", "code": error.reason_code, "detail": error.message},
            status_code=error.status_code,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        {"status": "success", "release": updated.to_dict()},
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/strategy-cards/{card_id}/microscope",
    dependencies=[permission_route(Permission.WORKSPACE_READ)],
)
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

    templates = _templates(request)
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


@router.post(
    "/api/strategy-cards/{card_id}/inspect-bar",
    dependencies=[
        permission_route(
            Permission.WORKSPACE_READ,
            csrf=CsrfMode.NONE,
            csrf_exempt_reason="Mô phỏng chỉ đọc một nến không thay đổi trạng thái hệ thống.",
        )
    ],
)
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


def _sanitize_csv_cell(value: Any) -> str:
    """Chống CSV formula injection khi mở bằng Excel/Google Sheets, giữ nguyên số âm hợp lệ."""
    text = "" if value is None else str(value)
    if not text:
        return ""
    if text[0] in ("=", "+", "@", "\t", "\r"):
        return f"'{text}"
    if text[0] == "-":
        try:
            Decimal(text)
        except InvalidOperation:
            return f"'{text}"
    return text


@router.get(
    "/api/workers/readiness",
    dependencies=[permission_route(Permission.WORKSPACE_READ)],
)
async def workers_readiness(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, Any]:
    """Trả readiness reason-coded của các worker cho admin-web (CRY-375)."""
    response.headers["Cache-Control"] = "no-store"
    workers = await _collect_worker_readiness(request, workspace_id=identity.principal.workspace_id)
    return {"workers": workers}


@router.get(
    "/api/workbench/metrics-alerts",
    dependencies=[permission_route(Permission.WORKSPACE_READ)],
)
async def workbench_metrics_alerts(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, Any]:
    """Trả metric freshness/coverage/lag/latency và cảnh báo tiếng Việt WP-07E (CRY-376)."""
    from trading_bot.admin.workbench_metrics_service import collect_workbench_metrics_alerts
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.observability.workbench_metrics import evaluate_workbench_metrics_and_alerts

    response.headers["Cache-Control"] = "no-store"
    sessions = getattr(request.app.state, "session_factory", None)
    if sessions is None:
        return evaluate_workbench_metrics_and_alerts().to_dict()
    async with UnitOfWork(sessions, workspace_id=identity.principal.workspace_id) as unit:
        report = await collect_workbench_metrics_alerts(
            unit.session,
            workspace_id=identity.principal.workspace_id,
        )
    return report.to_dict()


@router.get(
    "/api/operations/health",
    dependencies=[
        permission_route(
            Permission.WORKSPACE_READ,
            bot_scope=BotScopePolicy(BotPermission.READ, param_name=None, param_location="service"),
        )
    ],
)
async def operations_health(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, Any]:
    """Cung cấp ảnh chụp sức khỏe vận hành đã che bí mật và gắn correlation_id (OPS07A/B)."""
    response.headers["Cache-Control"] = "no-store"
    now_utc = datetime.now(UTC)
    correlation_id = (
        request.headers.get("X-Correlation-Id", "").strip() or f"corr-{uuid4().hex[:12]}"
    )

    try:
        operations = _operations(request, identity)
        runtime = [_runtime_dict(item) for item in await operations.list_bots()]
        workspace = (
            _runtime_dict(await operations.workspace_status())
            if hasattr(operations, "workspace_status")
            else {"kill_state": "ACTIVE", "revision": 1}
        )
    except BotAdminError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error

    return await _build_operations_health_data(
        workspace_id=str(identity.principal.workspace_id),
        runtime=runtime,
        workspace=workspace,
        correlation_id=correlation_id,
        now_utc=now_utc,
        request=request,
        actor_membership_id=identity.principal.membership_id,
    )


@router.post(
    "/api/operations/breaker/resume",
    dependencies=[
        permission_route(
            Permission.RUNTIME_RESUME,
            csrf=CsrfMode.FORM,
            denied_detail="Không đủ quyền mở lại cầu dao vận hành.",
        )
    ],
)
async def operations_breaker_resume(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    bot_id: UUID | None = None,
    reason: str = "",
    csrf_token: str = "",
) -> dict[str, Any]:
    """Mở lại Circuit Breaker bền vững qua quyền riêng + MFA + lý do tiếng Việt (OPS07B)."""
    response.headers["Cache-Control"] = "no-store"
    principal = identity.principal
    if not authorize(principal, Permission.RUNTIME_RESUME):
        raise HTTPException(
            status_code=403,
            detail="Không đủ quyền mở lại cầu dao vận hành.",
            headers={"Cache-Control": "no-store"},
        )
    try:
        await _auth_service(request).verify_csrf(read_session_cookie(request), csrf_token)
    except AuthenticationFailure as error:
        raise HTTPException(status_code=403, detail=error.message) from error
    try:
        step_up_proof = extract_step_up_proof(request)
    except ActionChallengeError as error:
        raise HTTPException(status_code=error.status_code, detail=error.as_detail()) from error
    app_settings = getattr(request.app.state, "settings", None)
    challenge_service = getattr(request.app.state, "action_challenge_service", None)
    step_up_strict = bool(
        app_settings is not None
        and (
            app_settings.is_action_step_up_required
            or (
                app_settings.app_env == "test"
                and app_settings.action_step_up_required is None
                and challenge_service is not None
            )
        )
    )
    if step_up_proof is not None or step_up_strict:
        await enforce_endpoint_step_up(
            request,
            principal,
            action=ACTION_BREAKER_RESUME,
            payload=breaker_resume_payload(bot_id=bot_id, reason=reason),
        )
        recent_mfa = True
    else:
        recent_mfa = (
            principal.mfa_verified_at is not None
            and principal.mfa_method is MfaMethod.TOTP
            and datetime.now(UTC) - principal.mfa_verified_at <= RECENT_MFA_LIFETIME
        )
    health_service = getattr(request.app.state, "operational_health_service", None)
    if health_service is None:
        session_factory = getattr(request.app.state, "session_factory", None)
        if session_factory is not None:
            health_service = OperationalHealthService(session_factory)
    if health_service is None:
        raise HTTPException(
            status_code=503,
            detail="Dịch vụ lưu trạng thái cầu dao chưa sẵn sàng.",
            headers={"Cache-Control": "no-store"},
        )
    try:
        resumed = await health_service.resume_breaker(
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            operator_membership_id=principal.membership_id,
            operator_id=identity.email,
            mfa_verified=recent_mfa,
            approval_reason=reason,
        )
    except CircuitBreakerResumeDenied as error:
        raise HTTPException(
            status_code=409,
            detail=str(error),
            headers={"Cache-Control": "no-store"},
        ) from error
    return {
        "state": resumed.state.value,
        "entry_allowed": resumed.entry_allowed,
        "revision": resumed.revision,
        "detail_vi": resumed.detail_vi,
    }


# Cột CSV khớp khóa thật của dòng trade (`domain/portfolio/reporting.py::_trade_row`). Không có
# đòn bẩy hay MFE/MAE vì ledger chưa ghi các giá trị này — không xuất cột rỗng gây hiểu nhầm.
REPORT_CSV_FIELDS: tuple[str, ...] = (
    "trade_id",
    "bot_id",
    "symbol",
    "side",
    "quote_asset",
    "opened_at",
    "closed_at",
    "quantity",
    "entry_price",
    "exit_price",
    "gross_pnl",
    "fee_total",
    "funding_total",
    "slippage_total",
    "total_cost",
    "net_pnl",
    "entry_reason",
    "exit_reason",
)


def _report_csv_row(trade: dict[str, Any]) -> dict[str, Any]:
    """Một dòng CSV từ dòng trade của báo cáo; giá vào/ra là bình quân gia quyền fill ledger."""
    entry_quantity, entry_price = _fill_summary(trade.get("fills"), "ENTRY")
    _exit_quantity, exit_price = _fill_summary(trade.get("fills"), "EXIT")
    row = {key: trade.get(key) for key in REPORT_CSV_FIELDS}
    row.update(quantity=entry_quantity, entry_price=entry_price, exit_price=exit_price)
    return {key: "" if value is None else value for key, value in row.items()}


@router.get(
    "/reports/export.csv",
    dependencies=[
        permission_route(
            Permission.WORKSPACE_READ,
            bot_scope=BotScopePolicy(
                BotPermission.REPORT_READ, param_name="bot_id", param_location="query"
            ),
        )
    ],
)
async def export_reports_csv(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    start: datetime,
    end: datetime,
    scope: str = "bot",
    bot_id: str | None = None,
) -> Response:
    """Xuất giao dịch PAPER đã đóng ra CSV an toàn (WB-07D), chỉ dùng cột có trong ledger."""
    parsed_bot_id: UUID | None = None
    if bot_id and bot_id.strip():
        try:
            parsed_bot_id = UUID(bot_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="bot_id phải là UUID hợp lệ.") from error

    try:
        report_scope = ReportScope(scope)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="scope chỉ nhận bot hoặc workspace.") from error

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

    trades = report.get("trades", []) if isinstance(report, dict) else []
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=REPORT_CSV_FIELDS)
    writer.writeheader()
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        row = _report_csv_row(trade)
        writer.writerow({key: _sanitize_csv_cell(row.get(key, "")) for key in REPORT_CSV_FIELDS})

    filename = f"crypidian-report-{report_scope.value}.csv"
    headers = {
        "Cache-Control": "no-store",
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Report-Scope": report_scope.value,
        "X-Report-Start": start.isoformat(),
        "X-Report-End": end.isoformat(),
        "X-Report-Version": (
            str(report.get("report_version", "")) if isinstance(report, dict) else ""
        ),
    }
    if parsed_bot_id is not None:
        headers["X-Report-Bot-Id"] = str(parsed_bot_id)
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )
