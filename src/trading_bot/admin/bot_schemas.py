"""Lược đồ HTTP cho quản trị bot và overlay cấu hình theo bot."""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trading_bot.config.bot_settings import BotConfigOverlay, exact_decimal


class CreateBotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
    max_notional: Decimal

    @field_validator("max_notional", mode="before")
    @classmethod
    def parse_max_notional(cls, value: object) -> Decimal:
        parsed = exact_decimal(value)
        if parsed <= 0:
            raise ValueError("max_notional phải dương.")
        return parsed


class CreateBotDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overlay: BotConfigOverlay
    expected_parent_version_id: UUID | None = None


class UpdateBotDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    overlay: BotConfigOverlay


class PublishBotDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class RollbackBotConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_current_version_id: UUID
    reason: str = Field(min_length=1, max_length=500)
