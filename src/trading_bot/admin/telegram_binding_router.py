"""API quản lý Telegram binding dùng cùng application service với Admin Web."""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from trading_bot.auth.router import require_csrf
from trading_bot.auth.service import AuthIdentity

router = APIRouter(prefix="/api/v1/telegram-binding", tags=["telegram binding"])


class TelegramBindingReason(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


def _service(request: Request) -> Any:
    service = getattr(request.app.state, "telegram_binding_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ liên kết Telegram chưa sẵn sàng")
    return service


def _service_error(error: ValueError) -> HTTPException:
    message = str(error)
    status = 403 if "owner" in message.lower() or "mfa" in message.lower() else 422
    return HTTPException(status_code=status, detail="Thao tác liên kết Telegram bị từ chối.")


@router.post("/link")
async def create_telegram_link(
    body: TelegramBindingReason,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, str | datetime]:
    """Trả deep link có mã thô một lần; client phải tránh lưu/cache response."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    try:
        link = await _service(request).create_link(identity.principal, reason=body.reason)
    except ValueError as error:
        raise _service_error(error) from error
    return {"url": link.url, "expires_at": link.expires_at}


@router.post("/{membership_id}/revoke")
async def revoke_telegram_binding(
    membership_id: UUID,
    body: TelegramBindingReason,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, int]:
    try:
        revoked = await _service(request).revoke(
            identity.principal, membership_id, reason=body.reason
        )
    except ValueError as error:
        raise _service_error(error) from error
    return {"revoked": revoked}
