"""API opt-in Telegram signal-only; không tạo hoặc thao tác lệnh giao dịch."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from trading_bot.application.telegram_signal_subscription import TelegramSignalSubscriptionError
from trading_bot.application.telegram_signals import TelegramSignalFilters
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity

router = APIRouter(prefix="/api/v1/telegram-signals", tags=["telegram signals"])


class TelegramSignalFilterPreference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(default_factory=list, max_length=100)
    timeframes: list[str] = Field(default_factory=list, max_length=100)
    directions: list[str] = Field(default_factory=list, max_length=100)
    strategy_ids: list[str] = Field(default_factory=list, max_length=100)
    setup_codes: list[str] = Field(default_factory=list, max_length=100)


class TelegramSignalPreference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    filters: TelegramSignalFilterPreference | None = None

    @model_validator(mode="after")
    def require_preference_fields(self) -> TelegramSignalPreference:
        if not self.model_fields_set:
            raise ValueError("Cần chỉ định enabled hoặc ít nhất một bộ lọc.")
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("Giá trị preference không được null.")
        return self


def _service(request: Request) -> Any:
    service = getattr(request.app.state, "telegram_signal_subscription_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Đăng ký tín hiệu Telegram chưa sẵn sàng.")
    return service


@router.put("/{bot_id}/subscription")
async def set_telegram_signal_subscription(
    bot_id: UUID,
    body: TelegramSignalPreference,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
) -> dict[str, object]:
    """Cập nhật opt-in và allowlist nhận tín hiệu của thành viên hiện tại."""
    response.headers["Cache-Control"] = "no-store"
    try:
        service = _service(request)
        if body.filters is None:
            state = await service.set_enabled(
                identity.principal, bot_id=bot_id, enabled=body.enabled
            )
        else:
            try:
                filters = TelegramSignalFilters(
                    **{
                        name: tuple(getattr(body.filters, name))
                        for name in (
                            "symbols",
                            "timeframes",
                            "directions",
                            "strategy_ids",
                            "setup_codes",
                        )
                    }
                )
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            state = await service.update_preference(
                identity.principal,
                bot_id=bot_id,
                enabled=body.enabled,
                filters=filters,
            )
    except TelegramSignalSubscriptionError as error:
        message = str(error)
        status = 404 if "Không tìm thấy bot" in message else 409
        raise HTTPException(status_code=status, detail=message) from error
    return _state_payload(state)


@router.get("/{bot_id}/subscription")
async def get_telegram_signal_subscription(
    bot_id: UUID,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, object]:
    """Đọc opt-in và allowlist của thành viên hiện tại, không đọc subscription khác."""
    response.headers["Cache-Control"] = "no-store"
    try:
        state = await _service(request).get_state(identity.principal, bot_id=bot_id)
    except TelegramSignalSubscriptionError as error:
        message = str(error)
        status = 404 if "Không tìm thấy bot" in message else 409
        raise HTTPException(status_code=status, detail=message) from error
    return _state_payload(state)


def _state_payload(state: Any) -> dict[str, object]:
    return {
        "bot_id": str(state.bot_id),
        "enabled": state.enabled,
        "binding_linked": state.binding_linked,
        "filters": state.filters.as_dict(),
    }
