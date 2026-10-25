"""Lược đồ overlay cấu hình theo bot; chỉ chứa allowlist được phép thay root.

Overlay không chứa venue, mode, hard cap workspace, ID workspace/account/bot hay bí mật.
Mọi trường lạ bị từ chối (``extra="forbid"``) thay vì bị bỏ qua âm thầm.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast

from pydantic import Field, field_serializer, field_validator, model_validator

from trading_bot.config.workspace_settings import (
    FrozenSettingsModel,
    Timeframe,
    _freeze_parameter_value,
    _thaw_parameter_value,
)

BOT_OVERLAY_SCHEMA_VERSION = 1
_SECRET_KEY_PATTERN = re.compile(r"(?:secret|token|password|credential|api[_-]?key|cookie)", re.I)
_SYMBOL_PATTERN = re.compile(r"[A-Z0-9_]{2,32}")
# Thứ tự cố định để so sánh/hiển thị; MB-04 sẽ thêm trường fraction mở rộng tại đây.
RISK_FRACTION_FIELDS = ("per_trade_risk_fraction", "max_concentration_fraction")
RISK_AMOUNT_FIELDS = (
    "max_aggregate_open_risk",
    "max_notional",
    "max_leverage",
    "max_symbol_notional",
    "max_daily_loss",
    "max_rolling_loss",
)
RISK_INTEGER_FIELDS = ("max_positions",)
RISK_FIELDS = RISK_FRACTION_FIELDS + RISK_AMOUNT_FIELDS + RISK_INTEGER_FIELDS


def _reject_secret_keys(value: Any, path: str) -> None:
    """Duyệt đệ quy và từ chối khóa giống bí mật; thông báo không chứa giá trị."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _SECRET_KEY_PATTERN.search(str(key)):
                raise ValueError(f"Không cho phép trường bí mật tại {path}{key}.")
            _reject_secret_keys(child, f"{path}{key}.")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_secret_keys(child, f"{path}{index}.")


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


class BotStrategyOverlay(FrozenSettingsModel):
    """Thay nguyên khối release + parameters của root (không merge từng tham số)."""

    release_id: str = Field(min_length=1, max_length=120)
    parameters: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("parameters")
    @classmethod
    def freeze_parameters(cls, values: Mapping[str, Any]) -> Mapping[str, Any]:
        _reject_secret_keys(values, "strategy.parameters.")
        return cast(Mapping[str, Any], _freeze_parameter_value(values))

    @field_serializer("parameters")
    def serialize_parameters(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], _thaw_parameter_value(values))


class BotUniverseOverlay(FrozenSettingsModel):
    """Tập con symbol/timeframe của root; thiếu trường nghĩa là kế thừa root."""

    symbols: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=200)
    timeframes: tuple[Timeframe, ...] | None = Field(default=None, min_length=1, max_length=12)

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, values: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if values is None:
            return None
        if any(not _SYMBOL_PATTERN.fullmatch(symbol) for symbol in values):
            raise ValueError("Mã giao dịch phải viết hoa và chỉ chứa chữ, số hoặc dấu gạch dưới.")
        if len(set(values)) != len(values):
            raise ValueError("Danh sách mã giao dịch không được trùng lặp.")
        return values

    @field_validator("timeframes")
    @classmethod
    def validate_timeframes(
        cls, values: tuple[Timeframe, ...] | None
    ) -> tuple[Timeframe, ...] | None:
        if values is not None and len(set(values)) != len(values):
            raise ValueError("Danh sách khung thời gian không được trùng lặp.")
        return values


class BotRiskOverlay(FrozenSettingsModel):
    """Giới hạn risk tùy chọn; resolver bắt buộc mỗi giá trị nhỏ hơn hoặc bằng root."""

    per_trade_risk_fraction: Decimal | None = None
    max_concentration_fraction: Decimal | None = None
    max_aggregate_open_risk: Decimal | None = None
    max_notional: Decimal | None = None
    max_leverage: Decimal | None = None
    max_symbol_notional: Decimal | None = None
    max_daily_loss: Decimal | None = None
    max_rolling_loss: Decimal | None = None
    max_positions: int | None = Field(default=None, ge=1, strict=True)

    @field_validator(*RISK_FRACTION_FIELDS, mode="before")
    @classmethod
    def parse_fraction(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        parsed = exact_decimal(value)
        if not Decimal("0") < parsed <= Decimal("1"):
            raise ValueError("Tỷ lệ risk phải nằm trong (0, 1].")
        return parsed

    @field_validator(*RISK_AMOUNT_FIELDS, mode="before")
    @classmethod
    def parse_amount(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        parsed = exact_decimal(value)
        if parsed <= Decimal("0"):
            raise ValueError("Giới hạn tiền/đòn bẩy phải dương.")
        return parsed

    @field_serializer(*RISK_FRACTION_FIELDS, *RISK_AMOUNT_FIELDS)
    def serialize_decimal(self, value: Decimal | None) -> str | None:
        return None if value is None else decimal_text(value)


class BotConfigOverlay(FrozenSettingsModel):
    """Overlay bot schema v1; resolver kiểm thêm ràng buộc so với root đã publish."""

    schema_version: Literal[1]
    strategy: BotStrategyOverlay | None = None
    universe: BotUniverseOverlay | None = None
    risk: BotRiskOverlay | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_secret_fields(cls, value: Any) -> Any:
        _reject_secret_keys(value, "")
        return value


def canonical_overlay(overlay: BotConfigOverlay) -> dict[str, Any]:
    """JSON ổn định dùng chung cho lưu trữ và băm; trường None (kế thừa) bị lược bỏ."""
    return overlay.model_dump(mode="json", exclude_none=True)


def overlay_hash(payload: Mapping[str, Any]) -> str:
    """SHA-256 trên JSON UTF-8 sắp xếp khóa, không khoảng trắng thừa."""
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def risk_section(overlay: BotConfigOverlay) -> dict[str, Any]:
    """Khối risk canonical để phát hiện thay đổi cần MFA gần đây."""
    return cast(dict[str, Any], canonical_overlay(overlay).get("risk", {}))
