"""Các dependency xác thực phiên và kiểm tra CSRF dùng chung cho HTTP routers."""

from __future__ import annotations

from typing import Annotated, NoReturn, cast

from fastapi import Depends, HTTPException, Request

from trading_bot.auth.cookies import read_session_cookie
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity, AuthService


def get_auth_service(request: Request) -> AuthService:
    """Lấy ``AuthService`` từ ``app.state``; lỗi 503 nếu tiến trình chưa nối dịch vụ xác thực."""
    service = cast(AuthService | None, getattr(request.app.state, "auth_service", None))
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ xác thực chưa sẵn sàng")
    return service


def raise_auth_failure(error: AuthenticationFailure) -> NoReturn:
    """Chuyển lỗi xác thực thành ``HTTPException`` với mã trạng thái an toàn."""
    raise HTTPException(status_code=error.status_code, detail=error.message) from error


async def current_identity(
    request: Request, service: Annotated[AuthService, Depends(get_auth_service)]
) -> AuthIdentity:
    """Xác thực cookie phiên ở mọi request và trả về ``AuthIdentity`` hiện hành."""
    try:
        return await service.authenticate(read_session_cookie(request))
    except AuthenticationFailure as error:
        raise_auth_failure(error)


async def require_csrf(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> AuthIdentity:
    """Xác thực phiên và kiểm tra header ``X-CSRF-Token`` cho API thay đổi trạng thái."""
    try:
        await service.verify_csrf(
            read_session_cookie(request),
            request.headers.get("X-CSRF-Token"),
        )
    except AuthenticationFailure as error:
        raise_auth_failure(error)
    return identity


__all__ = [
    "current_identity",
    "get_auth_service",
    "raise_auth_failure",
    "require_csrf",
]
