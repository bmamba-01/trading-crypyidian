from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from trading_bot.auth.action_challenge import (
    ACTION_RISK_PROFILE_CREATE,
    ActionChallengeError,
    ActionChallengeService,
    extract_step_up_proof,
    resolve_step_up_proof,
    risk_profile_create_payload,
)
from trading_bot.auth.cookies import (
    clear_session_cookie,
    read_session_cookie,
    set_session_cookie,
)
from trading_bot.auth.dependencies import (
    current_identity as current_identity,
)
from trading_bot.auth.dependencies import (
    get_auth_service as get_auth_service,
)
from trading_bot.auth.dependencies import (
    raise_auth_failure as raise_auth_failure,
)
from trading_bot.auth.dependencies import (
    require_csrf as require_csrf,
)
from trading_bot.auth.policy import Permission, Principal, authorize
from trading_bot.auth.route_policy import (
    CsrfMode,
    authenticated_route,
    permission_route,
    public_route,
)
from trading_bot.auth.schemas import (
    ActionChallengeCreateRequest,
    ActionChallengeVerifyRequest,
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
    LoginResult,
)
from trading_bot.common.http_limits import IPNetwork, resolve_client_ip
from trading_bot.config.settings import AppSettings

router = APIRouter(prefix="/api/v1", tags=["authentication and administration"])
THROTTLED_DETAIL = "Thử lại sau khi hết thời gian giới hạn"


def get_action_challenge_service(request: Request) -> ActionChallengeService:
    service = cast(
        ActionChallengeService | None,
        getattr(request.app.state, "action_challenge_service", None),
    )
    if service is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "STEP_UP_SERVICE_UNAVAILABLE",
                "message": "Dịch vụ xác thực step-up chưa sẵn sàng.",
            },
        )
    return service


async def enforce_endpoint_step_up(
    request: Request,
    principal: Principal,
    *,
    action: str,
    payload: Mapping[str, Any],
    form_values: Mapping[str, str] | None = None,
) -> None:
    """Thực thi step-up theo hành động (SEC-20) ở lớp HTTP router."""
    try:
        proof = resolve_step_up_proof(extract_step_up_proof(request, form_values=form_values))
    except ActionChallengeError as error:
        raise HTTPException(status_code=error.status_code, detail=error.as_detail()) from error

    service = cast(
        ActionChallengeService | None,
        getattr(request.app.state, "action_challenge_service", None),
    )
    settings = cast(AppSettings | None, getattr(request.app.state, "settings", None))
    strict = bool(settings.is_action_step_up_required) if settings is not None else False
    if (
        settings is not None
        and settings.app_env == "test"
        and settings.action_step_up_required is None
        and service is not None
    ):
        strict = True

    if proof is not None:
        if service is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "STEP_UP_SERVICE_UNAVAILABLE",
                    "message": "Dịch vụ xác thực step-up chưa sẵn sàng.",
                },
            )
        try:
            await service.consume_proof(principal, proof=proof, action=action, payload=payload)
        except ActionChallengeError as error:
            raise HTTPException(status_code=error.status_code, detail=error.as_detail()) from error
        return

    if strict:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "STEP_UP_REQUIRED",
                "message": "Thao tác rủi ro cao yêu cầu xác thực step-up theo hành động.",
            },
        )
    now = datetime.now(UTC)
    if principal.mfa_verified_at > now or now - principal.mfa_verified_at > RECENT_MFA_LIFETIME:
        raise HTTPException(status_code=403, detail="Cần xác thực MFA gần đây")


def get_rate_limiter(request: Request) -> LoginRateLimiter:
    return cast(LoginRateLimiter, request.app.state.login_limiter)


def client_address(request: Request) -> str:
    """Trả IP client thực tế (chỉ tin `X-Forwarded-For` khi peer nằm trong proxy tin cậy)."""
    peer = request.client.host if request.client is not None else None
    app = request.scope.get("app")
    state = getattr(app, "state", None)
    trusted = cast(
        tuple[IPNetwork, ...],
        getattr(state, "trusted_proxies", ()) if state is not None else (),
    )
    return resolve_client_ip(peer, request.headers.get("x-forwarded-for"), trusted)


def login_account_scope(workspace_slug: str) -> str:
    """Phạm vi bộ đếm tài khoản cho login/bootstrap theo slug người gửi nhập.

    Tiền tố `slug:` tách namespace khỏi `workspace-id:` của reauth: slug thô chưa qua
    `normalize_slug` (ví dụ `workspace-id:<uuid>`) không thể trùng khóa reauth.
    """
    return f"slug:{workspace_slug.strip().casefold()}"


def session_account_scope(workspace_id: UUID) -> str:
    """Phạm vi bộ đếm tài khoản cho reauth, lấy từ phiên đã xác thực."""
    return f"workspace-id:{workspace_id}"


async def authenticate_login(
    *,
    workspace_slug: str,
    email: str,
    password: str,
    code: str,
    address: str,
    service: AuthService,
    limiter: LoginRateLimiter,
) -> LoginResult:
    """Dùng chung xác thực, MFA và throttle cho API lẫn form HTML."""
    normalized_email = email.strip().casefold()
    scope = login_account_scope(workspace_slug)
    if not limiter.allow(address, normalized_email, account_scope=scope):
        raise HTTPException(status_code=429, detail=THROTTLED_DETAIL)
    try:
        result = await service.login(
            workspace_slug=workspace_slug,
            email=normalized_email,
            password=password,
            code=code,
        )
    except AuthenticationFailure as error:
        limiter.record_failure(address, normalized_email, account_scope=scope)
        raise_auth_failure(error)
    limiter.record_success(address, normalized_email, account_scope=scope)
    return result


@router.post(
    "/auth/bootstrap",
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        public_route(
            reason=(
                "Khởi tạo workspace đầu tiên khi DB trống; xác thực bằng "
                "X-Bootstrap-Token và rate limit."
            ),
            csrf=CsrfMode.BOOTSTRAP_TOKEN,
        )
    ],
)
async def bootstrap_owner(
    body: BootstrapRequest,
    request: Request,
    response: Response,
    service: Annotated[AuthService, Depends(get_auth_service)],
    limiter: Annotated[LoginRateLimiter, Depends(get_rate_limiter)],
) -> dict[str, str]:
    email = body.email.strip().casefold()
    scope = login_account_scope(body.workspace_slug)
    address = client_address(request)
    if not limiter.allow(address, email, account_scope=scope):
        raise HTTPException(status_code=429, detail=THROTTLED_DETAIL)
    settings: AppSettings = request.app.state.settings
    if not verify_bootstrap_token(
        settings.bootstrap_token_file,
        request.headers.get("X-Bootstrap-Token"),
    ):
        # Token bootstrap sai không chứng minh gì về tài khoản: không ghi bộ đếm tài khoản
        # để endpoint này không thành đường khóa email bất kỳ (fix round 1 SEC-08).
        limiter.record_failure(address, email, count_account=False)
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
        limiter.record_failure(address, email, count_account=False)
        raise_auth_failure(error)
    limiter.record_success(address, email, account_scope=scope)
    response.headers["Cache-Control"] = "no-store"
    return {
        "workspace_id": str(result.workspace_id),
        "mfa_setup_uri": result.mfa_setup_uri,
        "status": "pending_mfa_verification",
    }


@router.post(
    "/auth/bootstrap/verify",
    dependencies=[
        public_route(
            reason="Xác minh TOTP kích hoạt tài khoản bootstrap; bảo vệ bằng rate limit.",
            csrf=CsrfMode.PUBLIC_AUTH,
        )
    ],
)
async def verify_bootstrap(
    body: BootstrapVerifyRequest,
    request: Request,
    response: Response,
    service: Annotated[AuthService, Depends(get_auth_service)],
    limiter: Annotated[LoginRateLimiter, Depends(get_rate_limiter)],
) -> dict[str, str]:
    email = body.email.strip().casefold()
    scope = login_account_scope(body.workspace_slug)
    address = client_address(request)
    if not limiter.allow(address, email, account_scope=scope):
        raise HTTPException(status_code=429, detail=THROTTLED_DETAIL)
    try:
        await service.confirm_bootstrap(
            workspace_slug=body.workspace_slug,
            email=email,
            code=body.code,
        )
    except AuthenticationFailure as error:
        limiter.record_failure(address, email, account_scope=scope)
        raise_auth_failure(error)
    limiter.record_success(address, email, account_scope=scope)
    response.headers["Cache-Control"] = "no-store"
    return {"status": "active"}


@router.post(
    "/auth/login",
    dependencies=[
        public_route(
            reason="Đăng nhập API bằng mật khẩu và TOTP kèm rate limit.",
            csrf=CsrfMode.PUBLIC_AUTH,
        )
    ],
)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    service: Annotated[AuthService, Depends(get_auth_service)],
    limiter: Annotated[LoginRateLimiter, Depends(get_rate_limiter)],
) -> dict[str, object]:
    result = await authenticate_login(
        workspace_slug=body.workspace_slug,
        email=body.email,
        password=body.password,
        code=body.code,
        address=client_address(request),
        service=service,
        limiter=limiter,
    )
    settings: AppSettings = request.app.state.settings
    set_session_cookie(
        response,
        settings,
        result.cookie_value,
        max_age=int(SESSION_LIFETIME.total_seconds()),
    )
    response.headers["Cache-Control"] = "no-store"
    return identity_body(result.email, result.principal) | {"csrf_token": result.csrf_token}


@router.get("/auth/me", dependencies=[authenticated_route()])
async def get_current_identity(
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, object]:
    return identity_body(identity.email, identity.principal)


@router.get("/auth/csrf", dependencies=[authenticated_route()])
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


@router.post(
    "/auth/reauthenticate",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[authenticated_route(csrf=CsrfMode.HEADER)],
)
async def reauthenticate(
    body: ReauthenticationRequest,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    service: Annotated[AuthService, Depends(get_auth_service)],
    limiter: Annotated[LoginRateLimiter, Depends(get_rate_limiter)],
) -> None:
    # SEC-08: khóa theo IP + email của phiên. Bộ đếm tài khoản dùng phạm vi riêng theo
    # workspace_id của phiên (khác phạm vi slug của login) để lỗi login do kẻ tấn công
    # dồn vào không chặn được reauth của phiên đang sống; cặp IP quen vẫn bỏ qua nó.
    address = client_address(request)
    scope = session_account_scope(identity.principal.workspace_id)
    if not limiter.allow(address, identity.email, account_scope=scope):
        raise HTTPException(status_code=429, detail=THROTTLED_DETAIL)
    try:
        await service.reauthenticate(
            read_session_cookie(request),
            password=body.password,
            code=body.code,
        )
    except AuthenticationFailure as error:
        limiter.record_failure(address, identity.email, account_scope=scope)
        raise_auth_failure(error)
    limiter.record_success(address, identity.email, account_scope=scope)


@router.post(
    "/auth/challenges",
    status_code=status.HTTP_201_CREATED,
    dependencies=[permission_route(Permission.WORKSPACE_READ, csrf=CsrfMode.HEADER)],
)
async def create_action_challenge(
    body: ActionChallengeCreateRequest,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    challenge_service: Annotated[ActionChallengeService, Depends(get_action_challenge_service)],
) -> dict[str, object]:
    """Tạo challenge một lần (TTL ≤ 5 phút) gắn phiên, hành động và hash payload WYSIWYS."""
    if not authorize(identity.principal, Permission.WORKSPACE_READ):
        raise HTTPException(status_code=403, detail="Không đủ quyền tạo challenge xác thực")
    response.headers["Cache-Control"] = "no-store"
    try:
        created = await challenge_service.create_challenge(
            identity.principal,
            action=body.action,
            payload=body.payload,
            ttl_seconds=body.ttl_seconds,
        )
    except ActionChallengeError as error:
        raise HTTPException(status_code=error.status_code, detail=error.as_detail()) from error
    return {
        "challenge": {
            "id": str(created.challenge_id),
            "action": created.action,
            "payload_hash": created.payload_hash,
            "created_at": created.created_at.isoformat(),
            "expires_at": created.expires_at.isoformat(),
            "max_attempts": created.max_attempts,
            "remaining_attempts": created.remaining_attempts,
            "status": created.status,
        }
    }


@router.post(
    "/auth/challenges/{challenge_id}/verify",
    dependencies=[permission_route(Permission.WORKSPACE_READ, csrf=CsrfMode.HEADER)],
)
async def verify_action_challenge(
    challenge_id: UUID,
    body: ActionChallengeVerifyRequest,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    challenge_service: Annotated[ActionChallengeService, Depends(get_action_challenge_service)],
) -> dict[str, object]:
    """Xác minh TOTP cho challenge và cấp token một lần gắn chặt phiên/hành động/payload."""
    if not authorize(identity.principal, Permission.WORKSPACE_READ):
        raise HTTPException(status_code=403, detail="Không đủ quyền xác minh challenge")
    response.headers["Cache-Control"] = "no-store"
    try:
        verified = await challenge_service.verify_challenge(
            identity.principal,
            challenge_id=challenge_id,
            code=body.code,
            action=body.action,
            payload=body.payload,
        )
    except ActionChallengeError as error:
        raise HTTPException(status_code=error.status_code, detail=error.as_detail()) from error
    return {
        "authorization": {
            "challenge_id": str(verified.challenge_id),
            "step_up_token": verified.step_up_token,
            "action": verified.action,
            "payload_hash": verified.payload_hash,
            "expires_at": verified.expires_at.isoformat(),
        }
    }


@router.post(
    "/auth/challenges/{challenge_id}/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[permission_route(Permission.WORKSPACE_READ, csrf=CsrfMode.HEADER)],
)
async def cancel_action_challenge(
    challenge_id: UUID,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    challenge_service: Annotated[ActionChallengeService, Depends(get_action_challenge_service)],
) -> None:
    """Hủy challenge đang chờ khi người dùng bỏ thao tác."""
    if not authorize(identity.principal, Permission.WORKSPACE_READ):
        raise HTTPException(status_code=403, detail="Không đủ quyền hủy challenge")
    try:
        await challenge_service.cancel_challenge(identity.principal, challenge_id=challenge_id)
    except ActionChallengeError as error:
        raise HTTPException(status_code=error.status_code, detail=error.as_detail()) from error


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[authenticated_route(csrf=CsrfMode.HEADER)],
)
async def logout(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> None:
    del identity
    await service.logout(read_session_cookie(request))
    clear_session_cookie(response, request.app.state.settings)


@router.post(
    "/risk-profiles",
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        permission_route(
            Permission.RISK_PROFILE_WRITE,
            csrf=CsrfMode.HEADER,
            denied_detail="Không đủ quyền thay đổi hồ sơ rủi ro",
        )
    ],
)
async def create_risk_profile(
    body: RiskProfileRequest,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, str]:
    if not authorize(identity.principal, Permission.RISK_PROFILE_WRITE):
        raise HTTPException(status_code=403, detail="Không đủ quyền thay đổi hồ sơ rủi ro")
    await enforce_endpoint_step_up(
        request,
        identity.principal,
        action=ACTION_RISK_PROFILE_CREATE,
        payload=risk_profile_create_payload(
            name=body.name,
            max_risk_fraction=body.max_risk_fraction,
            settings=body.settings,
        ),
    )
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
