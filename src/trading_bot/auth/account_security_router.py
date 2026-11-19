"""HTTP API vòng đời MFA, mã khôi phục, đổi mật khẩu và trạng thái bootstrap (SEC-14..16)."""

from __future__ import annotations

from typing import Annotated, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from trading_bot.auth.cookies import set_session_cookie
from trading_bot.auth.dependencies import current_identity, raise_auth_failure, require_csrf
from trading_bot.auth.mfa_recovery import (
    DEFAULT_RECOVERY_CODE_COUNT,
    MAX_RECOVERY_CODE_COUNT,
    MIN_RECOVERY_CODE_COUNT,
    AccountSecurityError,
    AccountSecurityService,
    check_bootstrap_token_retired,
    check_database_bootstrap_retirement,
)
from trading_bot.auth.policy import Permission, authorize
from trading_bot.auth.route_policy import CsrfMode, owner_route, permission_route, public_route
from trading_bot.auth.router import (
    THROTTLED_DETAIL,
    client_address,
    get_rate_limiter,
    identity_body,
    login_account_scope,
    session_account_scope,
)
from trading_bot.auth.security import LoginRateLimiter
from trading_bot.auth.service import SESSION_LIFETIME, AuthenticationFailure, AuthIdentity
from trading_bot.config.settings import AppSettings

router = APIRouter(prefix="/api/v1/auth", tags=["account security and mfa lifecycle"])


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecoveryCodesRegenerateRequest(_StrictRequest):
    current_password: str = Field(min_length=1, max_length=128)
    count: int = Field(
        default=DEFAULT_RECOVERY_CODE_COUNT,
        ge=MIN_RECOVERY_CODE_COUNT,
        le=MAX_RECOVERY_CODE_COUNT,
    )


class RecoveryLoginRequest(_StrictRequest):
    workspace_slug: str = Field(min_length=2, max_length=80)
    email: str = Field(min_length=5, max_length=320)
    password: str = Field(min_length=1, max_length=128)
    recovery_code: str = Field(min_length=8, max_length=64)


class TotpRotateInitiateRequest(_StrictRequest):
    current_password: str = Field(min_length=1, max_length=128)
    current_code: str | None = Field(
        default=None, min_length=6, max_length=6, pattern=r"^[0-9]{6}$"
    )


class TotpRotateConfirmRequest(_StrictRequest):
    factor_id: UUID
    code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")


class AdminMfaResetRequest(_StrictRequest):
    membership_id: UUID
    current_password: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=5, max_length=500)
    identity_verified: bool = True
    issue_one_time_recovery_code: bool = True


class PasswordChangeRequest(_StrictRequest):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


def get_account_security_service(request: Request) -> AccountSecurityService:
    service = cast(
        AccountSecurityService | None,
        getattr(request.app.state, "account_security_service", None),
    )
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Dịch vụ bảo mật tài khoản chưa sẵn sàng",
        )
    return service


def _raise_security_error(error: AccountSecurityError) -> NoReturn:
    raise HTTPException(status_code=error.status_code, detail=error.as_detail()) from error


def _request_id(request: Request) -> UUID | None:
    raw = request.headers.get("X-Request-ID")
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


@router.post(
    "/mfa/recovery-codes/regenerate",
    summary="Tạo lại bộ mã khôi phục MFA một lần",
    dependencies=[permission_route(Permission.WORKSPACE_READ, csrf=CsrfMode.HEADER)],
)
async def regenerate_recovery_codes(
    body: RecoveryCodesRegenerateRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    service: Annotated[AccountSecurityService, Depends(get_account_security_service)],
    limiter: Annotated[LoginRateLimiter, Depends(get_rate_limiter)],
) -> dict[str, object]:
    """Sinh 8–10 mã khôi phục mới, thu hồi bộ cũ và trả plaintext đúng một lần (SEC-14)."""
    response.headers["Cache-Control"] = "no-store"
    address = client_address(request)
    scope = session_account_scope(identity.principal.workspace_id)
    if not limiter.allow(address, identity.email, account_scope=scope):
        raise HTTPException(status_code=429, detail=THROTTLED_DETAIL)
    try:
        result = await service.regenerate_recovery_codes(
            identity.principal,
            current_password=body.current_password,
            count=body.count,
            request_id=_request_id(request),
        )
    except AccountSecurityError as error:
        if error.status_code == 401:
            limiter.record_failure(address, identity.email, account_scope=scope)
        _raise_security_error(error)
    limiter.record_success(address, identity.email, account_scope=scope)
    return result


@router.get(
    "/mfa/recovery-codes/status",
    summary="Trạng thái mã khôi phục MFA hiện có",
    dependencies=[permission_route(Permission.WORKSPACE_READ)],
)
async def get_recovery_code_status(
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    service: Annotated[AccountSecurityService, Depends(get_account_security_service)],
) -> dict[str, object]:
    """Trả về số lượng mã khôi phục còn lại/đã dùng; không bao giờ trả mã thô."""
    response.headers["Cache-Control"] = "no-store"
    return await service.recovery_code_status(identity.principal)


@router.post(
    "/mfa/recovery-login",
    summary="Đăng nhập khôi phục bằng mã recovery một lần",
    dependencies=[
        public_route(
            reason="Đăng nhập khôi phục bằng mật khẩu và mã recovery một lần kèm rate limit.",
            csrf=CsrfMode.PUBLIC_AUTH,
        )
    ],
)
async def recovery_login(
    body: RecoveryLoginRequest,
    request: Request,
    response: Response,
    service: Annotated[AccountSecurityService, Depends(get_account_security_service)],
    limiter: Annotated[LoginRateLimiter, Depends(get_rate_limiter)],
) -> dict[str, object]:
    """Đăng nhập bằng mật khẩu + recovery code một lần (tiêu thụ nguyên tử `used_at`)."""
    response.headers["Cache-Control"] = "no-store"
    normalized_email = body.email.strip().casefold()
    address = client_address(request)
    scope = login_account_scope(body.workspace_slug)
    if not limiter.allow(address, normalized_email, account_scope=scope):
        raise HTTPException(status_code=429, detail=THROTTLED_DETAIL)
    try:
        result = await service.recovery_login(
            workspace_slug=body.workspace_slug,
            email=normalized_email,
            password=body.password,
            recovery_code=body.recovery_code,
            request_id=_request_id(request),
        )
    except AuthenticationFailure as error:
        limiter.record_failure(address, normalized_email, account_scope=scope)
        raise_auth_failure(error)
    limiter.record_success(address, normalized_email, account_scope=scope)
    settings: AppSettings = request.app.state.settings
    set_session_cookie(
        response,
        settings,
        result.cookie_value,
        max_age=int(SESSION_LIFETIME.total_seconds()),
    )
    return identity_body(result.email, result.principal) | {
        "csrf_token": result.csrf_token,
        "mfa_recovery_used": True,
    }


@router.post(
    "/mfa/totp/rotate",
    status_code=status.HTTP_201_CREATED,
    summary="Khởi tạo xoay vòng yếu tố TOTP mới",
    dependencies=[permission_route(Permission.WORKSPACE_READ, csrf=CsrfMode.HEADER)],
)
async def initiate_totp_rotation(
    body: TotpRotateInitiateRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    service: Annotated[AccountSecurityService, Depends(get_account_security_service)],
    limiter: Annotated[LoginRateLimiter, Depends(get_rate_limiter)],
) -> dict[str, object]:
    """Tạo factor TOTP mới ở trạng thái chờ xác minh; yêu cầu MFA gần đây + mật khẩu."""
    response.headers["Cache-Control"] = "no-store"
    address = client_address(request)
    scope = session_account_scope(identity.principal.workspace_id)
    if not limiter.allow(address, identity.email, account_scope=scope):
        raise HTTPException(status_code=429, detail=THROTTLED_DETAIL)
    try:
        result = await service.initiate_totp_rotation(
            identity.principal,
            current_password=body.current_password,
            current_code=body.current_code,
            request_id=_request_id(request),
        )
    except AccountSecurityError as error:
        if error.status_code == 401:
            limiter.record_failure(address, identity.email, account_scope=scope)
        _raise_security_error(error)
    limiter.record_success(address, identity.email, account_scope=scope)
    return result


@router.post(
    "/mfa/totp/rotate/confirm",
    summary="Xác minh và kích hoạt yếu tố TOTP mới",
    dependencies=[permission_route(Permission.WORKSPACE_READ, csrf=CsrfMode.HEADER)],
)
async def confirm_totp_rotation(
    body: TotpRotateConfirmRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    service: Annotated[AccountSecurityService, Depends(get_account_security_service)],
    limiter: Annotated[LoginRateLimiter, Depends(get_rate_limiter)],
) -> dict[str, object]:
    """Xác nhận mã TOTP mới trước khi kích hoạt, vô hiệu factor cũ và thu hồi phiên khác."""
    response.headers["Cache-Control"] = "no-store"
    address = client_address(request)
    scope = session_account_scope(identity.principal.workspace_id)
    if not limiter.allow(address, identity.email, account_scope=scope):
        raise HTTPException(status_code=429, detail=THROTTLED_DETAIL)
    try:
        result = await service.confirm_totp_rotation(
            identity.principal,
            factor_id=body.factor_id,
            code=body.code,
            request_id=_request_id(request),
        )
    except AccountSecurityError as error:
        if error.status_code == 401:
            limiter.record_failure(address, identity.email, account_scope=scope)
        _raise_security_error(error)
    limiter.record_success(address, identity.email, account_scope=scope)
    return result


@router.post(
    "/mfa/admin-reset",
    summary="Đặt lại MFA thành viên mà quản trị viên không biết secret",
    dependencies=[permission_route(Permission.MEMBER_WRITE, csrf=CsrfMode.HEADER)],
)
async def admin_reset_member_mfa(
    body: AdminMfaResetRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    service: Annotated[AccountSecurityService, Depends(get_account_security_service)],
    limiter: Annotated[LoginRateLimiter, Depends(get_rate_limiter)],
) -> dict[str, object]:
    """Đặt lại MFA thành viên sau xác minh danh tính; không tiết lộ secret TOTP cho owner."""
    response.headers["Cache-Control"] = "no-store"
    if not authorize(identity.principal, Permission.MEMBER_WRITE):
        raise HTTPException(status_code=403, detail="Không đủ quyền đặt lại MFA thành viên.")
    address = client_address(request)
    scope = session_account_scope(identity.principal.workspace_id)
    if not limiter.allow(address, identity.email, account_scope=scope):
        raise HTTPException(status_code=429, detail=THROTTLED_DETAIL)
    try:
        result = await service.admin_reset_member_mfa(
            identity.principal,
            target_membership_id=body.membership_id,
            current_password=body.current_password,
            reason=body.reason,
            identity_verified=body.identity_verified,
            issue_one_time_recovery_code=body.issue_one_time_recovery_code,
            request_id=_request_id(request),
        )
    except AccountSecurityError as error:
        if error.status_code == 401:
            limiter.record_failure(address, identity.email, account_scope=scope)
        _raise_security_error(error)
    limiter.record_success(address, identity.email, account_scope=scope)
    return result


@router.post(
    "/password/change",
    summary="Đổi mật khẩu tài khoản và thu hồi các phiên khác",
    dependencies=[permission_route(Permission.WORKSPACE_READ, csrf=CsrfMode.HEADER)],
)
async def change_password(
    body: PasswordChangeRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    service: Annotated[AccountSecurityService, Depends(get_account_security_service)],
    limiter: Annotated[LoginRateLimiter, Depends(get_rate_limiter)],
) -> dict[str, object]:
    """Đổi mật khẩu (SEC-15): kiểm mật khẩu cũ, MFA gần đây, blocklist, thu hồi phiên khác."""
    response.headers["Cache-Control"] = "no-store"
    address = client_address(request)
    scope = session_account_scope(identity.principal.workspace_id)
    if not limiter.allow(address, identity.email, account_scope=scope):
        raise HTTPException(status_code=429, detail=THROTTLED_DETAIL)
    try:
        result = await service.change_password(
            identity.principal,
            current_password=body.current_password,
            new_password=body.new_password,
            request_id=_request_id(request),
        )
    except AccountSecurityError as error:
        if error.status_code == 401:
            limiter.record_failure(address, identity.email, account_scope=scope)
        _raise_security_error(error)
    limiter.record_success(address, identity.email, account_scope=scope)
    return result


@router.get(
    "/bootstrap/status",
    summary="Kiểm tra trạng thái hoàn tất bootstrap và vô hiệu hóa bootstrap token",
    dependencies=[owner_route()],
)
async def bootstrap_retirement_status(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, object]:
    """Báo trạng thái vô hiệu hóa file bootstrap token sau khi hệ thống đã bootstrap (SEC-16)."""
    del identity
    response.headers["Cache-Control"] = "no-store"
    settings: AppSettings = request.app.state.settings
    sessions = getattr(request.app.state, "session_factory", None)
    if sessions is not None:
        status_obj = await check_database_bootstrap_retirement(
            sessions,
            settings.bootstrap_token_file,
            log_warning=False,
        )
    else:
        status_obj = check_bootstrap_token_retired(
            settings.bootstrap_token_file,
            bootstrapped=True,
            log_warning=False,
        )
    return {
        "bootstrapped": status_obj.bootstrapped,
        "token_file_configured": status_obj.token_file_configured,
        "token_file_present": status_obj.token_file_present,
        "retired": status_obj.retired,
        "warning": status_obj.warning,
    }


__all__ = ["get_account_security_service", "router"]
