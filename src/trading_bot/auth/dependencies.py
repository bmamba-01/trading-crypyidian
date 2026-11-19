"""Các dependency xác thực phiên và kiểm tra CSRF dùng chung cho HTTP routers."""

from __future__ import annotations

from typing import Annotated, NoReturn, cast

from fastapi import Depends, HTTPException, Request

from trading_bot.auth.cookies import read_session_cookie
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity, AuthService
from trading_bot.observability.security_audit import (
    OUTCOME_DENIED,
    SECURITY_ACTION_CSRF_FAILED,
    extract_client_ip_from_request,
    record_security_audit_event,
    request_security_audit_scope,
)


def get_auth_service(request: Request) -> AuthService:
    """Lấy ``AuthService`` từ ``app.state``; lỗi 503 nếu tiến trình chưa nối dịch vụ xác thực."""
    service = cast(AuthService | None, getattr(request.app.state, "auth_service", None))
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ xác thực chưa sẵn sàng")
    return service


def raise_auth_failure(error: AuthenticationFailure) -> NoReturn:
    """Chuyển lỗi xác thực thành ``HTTPException`` với mã trạng thái an toàn."""
    detail: str | dict[str, str] = (
        {"code": error.code, "message": error.message} if error.code is not None else error.message
    )
    raise HTTPException(status_code=error.status_code, detail=detail) from error


async def current_identity(
    request: Request, service: Annotated[AuthService, Depends(get_auth_service)]
) -> AuthIdentity:
    """Xác thực cookie phiên ở mọi request và trả về ``AuthIdentity`` hiện hành."""
    cookie_value = read_session_cookie(request)
    try:
        identity = await service.authenticate(cookie_value)
    except AuthenticationFailure as error:
        raise_auth_failure(error)
    request.state.security_identity = identity
    request.state.security_session_cookie = cookie_value
    return identity


async def require_csrf(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> AuthIdentity:
    """Xác thực phiên và kiểm tra header ``X-CSRF-Token`` cho API thay đổi trạng thái."""
    cookie_value = read_session_cookie(request)
    try:
        with request_security_audit_scope(request, session_cookie=cookie_value):
            await service.verify_csrf(
                cookie_value,
                request.headers.get("X-CSRF-Token"),
            )
    except AuthenticationFailure as error:
        request.state.security_403_audited = True
        if not getattr(service, "_sec32_csrf_audited", False):
            with request_security_audit_scope(request, session_cookie=cookie_value):
                await record_security_audit_event(
                    workspace_id=identity.principal.workspace_id,
                    actor_membership_id=identity.principal.membership_id,
                    action=SECURITY_ACTION_CSRF_FAILED,
                    target_type="http_route",
                    target_id=identity.principal.session_id,
                    reason=f"{request.method} {request.url.path}: {error.message}",
                    outcome=OUTCOME_DENIED,
                    sessions=getattr(request.app.state, "session_factory", None),
                )
                detector = getattr(request.app.state, "security_anomaly_detector", None)
                if detector is not None:
                    await detector.observe_probing_failure(
                        workspace_id=identity.principal.workspace_id,
                        actor_membership_id=identity.principal.membership_id,
                        action=SECURITY_ACTION_CSRF_FAILED,
                        client_ip=extract_client_ip_from_request(request),
                        reason=error.message,
                    )
        raise_auth_failure(error)
    return identity


__all__ = [
    "current_identity",
    "get_auth_service",
    "raise_auth_failure",
    "require_csrf",
]
