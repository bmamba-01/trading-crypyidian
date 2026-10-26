"""Lược đồ cấu hình nghiệp vụ gốc cho toàn workspace."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

Timeframe = Literal["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"]
FillModel = Literal["next_bar_with_adverse_costs"]
_SECRET_KEY_PATTERN = re.compile(r"(?:secret|token|password|credential|api[_-]?key|cookie)", re.I)


class FrozenSettingsModel(BaseModel):
    """Cấu hình có cấu trúc chặt, bất biến trong bộ nhớ và không nhận khóa lạ."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeSettings(FrozenSettingsModel):
    mode: Literal["PAPER"]
    timezone: Literal["UTC"]


class MarketDataSettings(FrozenSettingsModel):
    venue: Literal["BINANCE_USDM"]
    symbols: tuple[str, ...] = Field(min_length=1, max_length=200)
    timeframes: tuple[Timeframe, ...] = Field(min_length=1, max_length=12)
    max_age_seconds: int = Field(ge=1, le=86_400)

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"[A-Z0-9_]{2,32}", symbol) for symbol in values):
            raise ValueError("Mã giao dịch phải viết hoa và chỉ chứa chữ, số hoặc dấu gạch dưới.")
        if len(set(values)) != len(values):
            raise ValueError("Danh sách mã giao dịch không được trùng lặp.")
        return values

    @field_validator("timeframes")
    @classmethod
    def validate_timeframes(cls, values: tuple[Timeframe, ...]) -> tuple[Timeframe, ...]:
        if len(set(values)) != len(values):
            raise ValueError("Danh sách khung thời gian không được trùng lặp.")
        return values


class FeatureSettings(FrozenSettingsModel):
    formula_version: str = Field(min_length=1, max_length=120)
    sessions_timezone: Literal["UTC"]


class StrategySettings(FrozenSettingsModel):
    default_release: str = Field(min_length=1, max_length=120)
    parameters: Mapping[str, Any] = Field(default_factory=dict)
    entry_timeframe: Timeframe
    confirmation_timeframe: Timeframe

    @field_validator("parameters")
    @classmethod
    def reject_secret_parameters(cls, values: Mapping[str, Any]) -> Mapping[str, Any]:
        def visit(value: Any, path: str) -> None:
            if isinstance(value, Mapping):
                for key, child in value.items():
                    if _SECRET_KEY_PATTERN.search(str(key)):
                        raise ValueError(
                            f"Không cho phép trường bí mật tại strategy.parameters.{path}{key}."
                        )
                    visit(child, f"{path}{key}.")
            elif isinstance(value, (list, tuple)):
                for index, child in enumerate(value):
                    visit(child, f"{path}{index}.")

        visit(values, "")
        return cast(Mapping[str, Any], _freeze_parameter_value(values))

    @field_serializer("parameters")
    def serialize_parameters(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Xuất bản sao JSON có thể serialize mà không lộ cấu trúc nội bộ."""
        return cast(dict[str, Any], _thaw_parameter_value(values))


def _freeze_parameter_value(value: Any) -> Any:
    """Sao chép đệ quy cấu trúc tham số sang mapping và tuple chỉ đọc."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_parameter_value(child) for key, child in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_parameter_value(child) for child in value)
    return value


def _thaw_parameter_value(value: Any) -> Any:
    """Tạo cấu trúc JSON mới từ các mapping và tuple chỉ đọc."""
    if isinstance(value, Mapping):
        return {key: _thaw_parameter_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_parameter_value(child) for child in value]
    return value


def exact_decimal(value: object) -> Decimal:
    """Chỉ nhận chuỗi/Decimal/int để không mất độ chính xác qua float JSON."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("Giá trị Decimal phải là chuỗi hoặc số nguyên, không dùng float.")
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, (str, int)):
        try:
            parsed = Decimal(value)
        except (InvalidOperation, ValueError) as error:
            raise ValueError("Giá trị Decimal không hợp lệ.") from error
    else:
        raise ValueError("Giá trị Decimal phải là chuỗi hoặc số nguyên.")
    if not parsed.is_finite():
        raise ValueError("Giá trị Decimal phải hữu hạn.")
    return parsed


def decimal_text(value: Decimal) -> str:
    """Biểu diễn chuẩn hóa: giá trị bằng nhau luôn ra cùng chuỗi để hash ổn định."""
    normalized = value.normalize()
    text = format(normalized, "f")
    return "0" if text in {"-0", "0"} else text


class RiskSettings(FrozenSettingsModel):
    profile_version_id: UUID
    workspace_max_open_risk_fraction: Decimal = Field(default=Decimal("1.0"))
    max_equity_age_seconds: int = Field(default=300, ge=1, le=86_400)
    max_conversion_age_seconds: int = Field(default=300, ge=1, le=86_400)
    max_mark_age_seconds: int = Field(default=60, ge=1, le=3_600)

    @field_validator("workspace_max_open_risk_fraction", mode="before")
    @classmethod
    def parse_workspace_max_open_risk_fraction(cls, value: object) -> Decimal:
        parsed = exact_decimal(value)
        if not Decimal("0") < parsed <= Decimal("1"):
            raise ValueError("workspace_max_open_risk_fraction phải nằm trong (0, 1].")
        return parsed

    @field_serializer("workspace_max_open_risk_fraction")
    def serialize_fraction(self, value: Decimal) -> str:
        return decimal_text(value)


class PaperExecutionSettings(FrozenSettingsModel):
    quote_asset: str = Field(min_length=2, max_length=16, pattern=r"^[A-Z0-9]+$")
    starting_equity: Decimal = Field(gt=Decimal("0"), max_digits=38, decimal_places=18)
    fee_schedule_version: str = Field(min_length=1, max_length=120)
    fill_model: FillModel
    recovery_policy: Literal["STRICT_FAIL_CLOSED"] = "STRICT_FAIL_CLOSED"


class WorkspaceSettings(FrozenSettingsModel):
    """Cấu hình gốc được version hóa; lược đồ hiện chỉ cho phép PAPER."""

    schema_version: Literal[1]
    runtime: RuntimeSettings
    market_data: MarketDataSettings
    features: FeatureSettings
    strategy: StrategySettings
    risk: RiskSettings
    paper_execution: PaperExecutionSettings

    @model_validator(mode="after")
    def validate_strategy_timeframes(self) -> WorkspaceSettings:
        if self.strategy.entry_timeframe not in self.market_data.timeframes:
            raise ValueError("entry_timeframe phải có trong market_data.timeframes.")
        if self.strategy.confirmation_timeframe not in self.market_data.timeframes:
            raise ValueError("confirmation_timeframe phải có trong market_data.timeframes.")
        return self


@dataclass(frozen=True, slots=True)
class PublishedWorkspaceSettings:
    """Bản cấu hình đã publish, có version/hash để runtime ghim lineage."""

    workspace_id: UUID
    settings_version_id: UUID
    settings_hash: str
    settings: WorkspaceSettings
    effective_at: datetime | None = None

    def __post_init__(self) -> None:
        if len(self.settings_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.settings_hash
        ):
            raise ValueError("settings_hash phải là SHA-256 dạng hex chữ thường.")
        if self.effective_at is not None and (
            self.effective_at.tzinfo is None or self.effective_at.utcoffset() is None
        ):
            raise ValueError("effective_at phải có múi giờ.")
