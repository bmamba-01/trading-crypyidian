from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from trading_bot.auth.cookies import (
    clear_session_cookie,
    read_session_cookie,
    set_session_cookie,
)
from trading_bot.auth.policy import Permission, Principal, authorize
from trading_bot.auth.schemas import (
    BootstrapRequest,
    BootstrapVerifyRequest,
    LoginRequest,
    ReauthenticationRequest,
    RiskProfileRequest,
)
from trading_bot.auth.security import LoginRateLimiter, verify_bootstrap_token
from trading_bot.auth.service import (
    RECENT_MFA_LIFETIME,
    SESSION_LIFETIME,
    AuthenticationFailure,
    AuthIdentity,
    AuthService,
)
from trading_bot.config.settings import AppSettings

router = APIRouter(prefix="/api/v1", tags=["authentication and administration"])
THROTTLED_DETAIL = "Thử lại sau khi hết thời gian giới hạn"


def get_auth_service(request: Request) -> AuthService:
    service = cast(AuthService | None, getattr(request.app.state, "auth_service", None))
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ xác thực chưa sẵn sàng")
    return service


def get_rate_limiter(request: Request) -> LoginRateLimiter:
    return cast(LoginRateLimiter, request.app.state.login_limiter)


def client_address(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def raise_auth_failure(error: AuthenticationFailure) -> None:
    raise HTTPException(status_code=error.status_code, detail=error.message) from error


@router.post("/auth/bootstrap", status_code=status.HTTP_201_CREATED)
async def bootstrap_owner(
    body: BootstrapRequest,
    request: Request,
    response: Response,
    service: Annotated[AuthService, Depends(get_auth_service)],
    limiter: Annotated[LoginRateLimiter, Depends(get_rate_limiter)],
) -> dict[str, str]:
    email = body.email.strip().casefold()
    address = client_address(request)
    if not limiter.allow(address, email):
        raise HTTPException(status_code=429, detail=THROTTLED_DETAIL)
    settings: AppSettings = request.app.state.settings
    if not verify_bootstrap_token(
        settings.bootstrap_token_file,
        request.headers.get("X-Bootstrap-Token"),
    ):
        limiter.record_failure(address, email)
        raise HTTPException(status_code=403, detail="Mã bootstrap không hợp lệ")
    try:
        result = await service.bootstrap(
            workspace_name=body.workspace_name,
            workspace_slug=body.workspace_slug,
            email=email,
            password=body.password,
            mode=body.mode,
        )
    except AuthenticationFailure as error:
        limiter.record_failure(address, email)
        raise_auth_failure(error)
    limiter.record_success(address, email)
    response.headers["Cache-Control"] = "no-store"
    return {
        "workspace_id": str(result.workspace_id),
        "mfa_setup_uri": result.mfa_setup_uri,
        "status": "pending_mfa_verification",
    }


@router.post("/auth/bootstrap/verify")
async def verify_bootstrap(
    body: BootstrapVerifyRequest,
    request: Request,
    response: Response,
    service: Annotated[AuthService, Depends(get_auth_service)],
    limiter: Annotated[LoginRateLimiter, Depends(get_rate_limiter)],
) -> dict[str, str]:
    email = body.email.strip().casefold()
    address = client_address(request)
    if not limiter.allow(address, email):
        raise HTTPException(status_code=429, detail=THROTTLED_DETAIL)
    try:
        await service.confirm_bootstrap(
            workspace_slug=body.workspace_slug,
            email=email,
            code=body.code,
        )
    except AuthenticationFailure as error:
        limiter.record_failure(address, email)
        raise_auth_failure(error)
    limiter.record_success(address, email)
    response.headers["Cache-Control"] = "no-store"
    return {"status": "active"}


@router.post("/auth/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    service: Annotated[AuthService, Depends(get_auth_service)],
    limiter: Annotated[LoginRateLimiter, Depends(get_rate_limiter)],
) -> dict[str, object]:
    email = body.email.strip().casefold()
    address = client_address(request)
    if not limiter.allow(address, email):
        raise HTTPException(status_code=429, detail=THROTTLED_DETAIL)
    try:
        result = await service.login(
            workspace_slug=body.workspace_slug,
            email=email,
            password=body.password,
            code=body.code,
        )
    except AuthenticationFailure as error:
        limiter.record_failure(address, email)
        raise_auth_failure(error)
    limiter.record_success(address, email)
    settings: AppSettings = request.app.state.settings
    set_session_cookie(
        response,
        settings,
        result.cookie_value,
        max_age=int(SESSION_LIFETIME.total_seconds()),
    )
    response.headers["Cache-Control"] = "no-store"
    return identity_body(result.email, result.principal) | {"csrf_token": result.csrf_token}


async def current_identity(
    request: Request, service: Annotated[AuthService, Depends(get_auth_service)]
) -> AuthIdentity:
    try:
        return await service.authenticate(read_session_cookie(request))
    except AuthenticationFailure as error:
        raise_auth_failure(error)
    raise AssertionError("Không thể tiếp tục sau lỗi xác thực")


async def require_csrf(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> AuthIdentity:
    try:
        await service.verify_csrf(
            read_session_cookie(request),
            request.headers.get("X-CSRF-Token"),
        )
    except AuthenticationFailure as error:
        raise_auth_failure(error)
    return identity


@router.get("/auth/me")
async def get_current_identity(
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, object]:
    return identity_body(identity.email, identity.principal)


@router.get("/auth/csrf")
async def get_csrf_token(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, str]:
    del identity
    try:
        token = await service.issue_csrf_token(read_session_cookie(request))
    except AuthenticationFailure as error:
        raise_auth_failure(error)
    response.headers["Cache-Control"] = "no-store"
    return {"csrf_token": token}


@router.post("/auth/reauthenticate", status_code=status.HTTP_204_NO_CONTENT)
async def reauthenticate(
    body: ReauthenticationRequest,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    service: Annotated[AuthService, Depends(get_auth_service)],
    limiter: Annotated[LoginRateLimiter, Depends(get_rate_limiter)],
) -> None:
    # Dùng chung bộ đếm với login vì cùng kiểm mật khẩu + TOTP (SEC-08); khóa theo
    # email của phiên nên không lộ thông tin tài khoản khác.
    address = client_address(request)
    if not limiter.allow(address, identity.email):
        raise HTTPException(status_code=429, detail=THROTTLED_DETAIL)
    try:
        await service.reauthenticate(
            read_session_cookie(request),
            password=body.password,
            code=body.code,
        )
    except AuthenticationFailure as error:
        limiter.record_failure(address, identity.email)
        raise_auth_failure(error)
    limiter.record_success(address, identity.email)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> None:
    del identity
    await service.logout(read_session_cookie(request))
    clear_session_cookie(response, request.app.state.settings)


@router.post("/risk-profiles", status_code=status.HTTP_201_CREATED)
async def create_risk_profile(
    body: RiskProfileRequest,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, str]:
    if not authorize(identity.principal, Permission.RISK_PROFILE_WRITE):
        raise HTTPException(status_code=403, detail="Không đủ quyền thay đổi hồ sơ rủi ro")
    if datetime.now(UTC) - identity.principal.mfa_verified_at > RECENT_MFA_LIFETIME:
        raise HTTPException(status_code=403, detail="Cần xác thực MFA gần đây")
    profile_id = await service.create_risk_profile(
        identity.principal,
        name=body.name,
        max_risk_fraction=body.max_risk_fraction,
        settings=body.settings,
    )
    return {"id": str(profile_id), "status": "draft"}


def identity_body(email: str, principal: Principal) -> dict[str, object]:
    return {
        "email": email,
        "workspace_id": str(principal.workspace_id),
        "roles": sorted(role.value for role in principal.roles),
    }
