"""Nạp hồ sơ rủi ro có kiểu và chặn cấu hình thiếu giới hạn cứng."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import RiskProfileVersion
from trading_bot.domain.risk.planner import PlannerSettings, RiskLimits


class RiskProfileRuntimeError(ValueError):
    """Lỗi dữ liệu hồ sơ khiến runtime phải từ chối đánh giá rủi ro."""


class _HardCaps(BaseModel):
    """Các giới hạn cứng bắt buộc trong settings schema v1."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_aggregate_open_risk: Decimal
    max_notional: Decimal
    max_leverage: Decimal
    max_positions: int = Field(ge=1)
    max_symbol_notional: Decimal
    max_concentration_fraction: Decimal
    max_daily_loss: Decimal
    max_rolling_loss: Decimal
    max_account_age_seconds: int = Field(ge=0)
    max_data_age_seconds: int = Field(ge=0)

    @field_validator(
        "max_aggregate_open_risk",
        "max_notional",
        "max_leverage",
        "max_symbol_notional",
        "max_concentration_fraction",
        "max_daily_loss",
        "max_rolling_loss",
        mode="before",
    )
    @classmethod
    def parse_decimal_string(cls, value: object) -> Decimal:
        """Chỉ nhận chuỗi thập phân để JSON float không làm mất độ chính xác."""
        if not isinstance(value, str):
            raise ValueError("Giới hạn Decimal phải được lưu dưới dạng chuỗi.")
        try:
            parsed = Decimal(value)
        except (InvalidOperation, ValueError) as error:
            raise ValueError("Giới hạn Decimal không hợp lệ.") from error
        if not parsed.is_finite():
            raise ValueError("Giới hạn Decimal phải hữu hạn.")
        return parsed


class _RiskProfileSettingsV1(BaseModel):
    """Cấu trúc JSON duy nhất runtime hiện có thể diễn giải an toàn."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: int = Field(ge=1, le=1)
    hard_caps: _HardCaps


@dataclass(frozen=True, slots=True)
class RuntimeRiskProfile:
    """Phiên bản hồ sơ đã ghim cùng bộ hard caps bất biến."""

    profile_id: UUID
    workspace_id: UUID
    name: str
    version: int
    limits: RiskLimits

    def apply_to(self, planner_settings: PlannerSettings) -> PlannerSettings:
        """Gắn caps chỉ khi profile và planner cùng workspace/version."""
        if planner_settings.workspace_id != str(self.workspace_id):
            raise RiskProfileRuntimeError("Risk profile thuộc workspace khác.")
        if planner_settings.risk_profile_version_id != str(self.profile_id):
            raise RiskProfileRuntimeError("PlannerSettings ghim sai phiên bản risk profile.")
        return replace(planner_settings, limits=self.limits)


def risk_limits_from_profile(record: object) -> RuntimeRiskProfile:
    """Kiểm định một hàng RiskProfileVersion và chuyển thành RiskLimits."""
    try:
        profile_id = _uuid_value(record, "id")
        workspace_id = _uuid_value(record, "workspace_id")
        name = _string_value(record, "name")
        version = _int_value(record, "version")
        max_risk_fraction = _decimal_value(record, "max_risk_fraction")
        raw_open_risk = getattr(record, "max_open_risk_fraction", None)
        max_open_risk_fraction = (
            _decimal_value(record, "max_open_risk_fraction")
            if raw_open_risk is not None
            else Decimal("1.0")
        )
        settings = _value(record, "settings")
        parsed = _RiskProfileSettingsV1.model_validate(settings)
        caps = parsed.hard_caps
        limits = RiskLimits(
            per_trade_risk_fraction=max_risk_fraction,
            account_max_open_risk_fraction=max_open_risk_fraction,
            max_aggregate_open_risk=caps.max_aggregate_open_risk,
            max_notional=caps.max_notional,
            max_leverage=caps.max_leverage,
            max_positions=caps.max_positions,
            max_symbol_notional=caps.max_symbol_notional,
            max_concentration_fraction=caps.max_concentration_fraction,
            max_daily_loss=caps.max_daily_loss,
            max_rolling_loss=caps.max_rolling_loss,
            max_account_age=timedelta(seconds=caps.max_account_age_seconds),
            max_data_age=timedelta(seconds=caps.max_data_age_seconds),
        )
    except (AttributeError, TypeError, ValueError, ValidationError) as error:
        raise RiskProfileRuntimeError(
            "Risk profile không hợp lệ hoặc thiếu hard caps schema_version=1; "
            "runtime từ chối dùng giới hạn mặc định."
        ) from error
    if version < 1 or not name.strip():
        raise RiskProfileRuntimeError("Risk profile thiếu tên hoặc version hợp lệ.")
    return RuntimeRiskProfile(profile_id, workspace_id, name, version, limits)


async def load_risk_profile(
    session: AsyncSession, *, workspace_id: UUID, profile_id: UUID
) -> RuntimeRiskProfile:
    """Nạp profile theo cả workspace và ID, tránh truy xuất chéo tenant."""
    record = await session.scalar(
        select(RiskProfileVersion).where(
            RiskProfileVersion.workspace_id == workspace_id,
            RiskProfileVersion.id == profile_id,
        )
    )
    if record is None:
        raise RiskProfileRuntimeError("Risk profile không tồn tại trong workspace hiện tại.")
    return risk_limits_from_profile(record)


def _value(record: object, name: str) -> object:
    return cast(object, getattr(record, name))


def _uuid_value(record: object, name: str) -> UUID:
    value = _value(record, name)
    if not isinstance(value, UUID):
        raise ValueError(f"{name} phải là UUID.")
    return value


def _string_value(record: object, name: str) -> str:
    value = _value(record, name)
    if not isinstance(value, str):
        raise ValueError(f"{name} phải là chuỗi.")
    return value


def _int_value(record: object, name: str) -> int:
    value = _value(record, name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} phải là số nguyên.")
    return value


def _decimal_value(record: object, name: str) -> Decimal:
    value = _value(record, name)
    if isinstance(value, bool) or not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} phải là Decimal hữu hạn.")
    return value
