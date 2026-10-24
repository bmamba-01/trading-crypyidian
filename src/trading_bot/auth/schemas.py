from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AuthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BootstrapRequest(AuthRequest):
    workspace_name: str = Field(min_length=2, max_length=120)
    workspace_slug: str = Field(min_length=2, max_length=80)
    email: str = Field(min_length=5, max_length=320)
    password: str = Field(min_length=14, max_length=128)
    mode: Literal["PAPER", "DEMO"] = "PAPER"


class BootstrapVerifyRequest(AuthRequest):
    workspace_slug: str = Field(min_length=2, max_length=80)
    email: str = Field(min_length=5, max_length=320)
    code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")


class LoginRequest(BootstrapVerifyRequest):
    password: str = Field(min_length=1, max_length=128)


class ReauthenticationRequest(AuthRequest):
    password: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")


class RiskProfileRequest(AuthRequest):
    name: str = Field(min_length=2, max_length=120)
    max_risk_fraction: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    settings: dict[str, object] = Field(default_factory=dict)
