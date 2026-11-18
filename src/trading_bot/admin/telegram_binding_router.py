"""API quản lý Telegram binding dùng cùng application service với Admin Web."""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from trading_bot.application.telegram_binding import (
    TelegramStepUpError,
    TelegramTopicSetupError,
)
from trading_bot.auth.action_challenge import ActionChallengeError, http_step_up_scope
from trading_bot.auth.route_policy import CsrfMode, owner_route
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
    if isinstance(error, TelegramStepUpError):
        return HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": str(error)},
        )
    message = str(error)
    status = 403 if "owner" in message.lower() or "mfa" in message.lower() else 422
    return HTTPException(status_code=status, detail="Thao tác liên kết Telegram bị từ chối.")


@router.post("/link", dependencies=[owner_route(csrf=CsrfMode.HEADER)])
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
        with http_step_up_scope(request):
            link = await _service(request).create_link(identity.principal, reason=body.reason)
    except ActionChallengeError as error:
        raise HTTPException(status_code=error.status_code, detail=error.as_detail()) from error
    except ValueError as error:
        raise _service_error(error) from error
    return {"url": link.url, "expires_at": link.expires_at}


@router.post("/{membership_id}/revoke", dependencies=[owner_route(csrf=CsrfMode.HEADER)])
async def revoke_telegram_binding(
    membership_id: UUID,
    body: TelegramBindingReason,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, int]:
    try:
        with http_step_up_scope(request):
            revoked = await _service(request).revoke(
                identity.principal, membership_id, reason=body.reason
            )
    except ActionChallengeError as error:
        raise HTTPException(status_code=error.status_code, detail=error.as_detail()) from error
    except ValueError as error:
        raise _service_error(error) from error
    return {"revoked": revoked}


@router.post("/{membership_id}/topics", dependencies=[owner_route(csrf=CsrfMode.HEADER)])
async def create_telegram_topics(
    membership_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, bool]:
    """TG-4: tạo hai topic (tín hiệu / PAPER report); không trả id topic hay chat."""
    try:
        await _service(request).create_topics(identity.principal, membership_id)
    except TelegramTopicSetupError as error:
        raise HTTPException(
            status_code=502, detail=f"Telegram không tạo được topic ({error.code})."
        ) from error
    except ValueError as error:
        raise _service_error(error) from error
    return {"topics": True}


@router.delete("/{membership_id}/topics", dependencies=[owner_route(csrf=CsrfMode.HEADER)])
async def clear_telegram_topics(
    membership_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, bool]:
    try:
        cleared = await _service(request).clear_topics(identity.principal, membership_id)
    except ValueError as error:
        raise _service_error(error) from error
    return {"cleared": cleared}
