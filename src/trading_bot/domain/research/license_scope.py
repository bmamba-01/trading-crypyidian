"""Phạm vi quyền có kiểu và hợp đồng băm canonical cho bằng chứng license."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar

from trading_bot.domain.research.source_policy import SourceUsePurpose

SCOPE_VERSION = "wp06-license-scope-v1"
_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_UPPER_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9_.:-]{0,127}\Z")
_INTERVAL = re.compile(r"[1-9][0-9]*[mhdwM]\Z")
_OUTPUTS = frozenset({"derived_features", "trained_model", "aggregate_report", "paper_inference"})
_DERIVED_USE = frozenset(
    {"feature_extraction", "model_training", "paper_inference", "aggregate_reporting"}
)


def _members[T: str](values: tuple[T, ...], name: str, valid: Callable[[T], bool]) -> tuple[T, ...]:
    if not isinstance(values, (tuple, list, set, frozenset)) or not values:
        raise ValueError(f"{name} phải là tập không rỗng.")
    if any(not isinstance(value, str) or not valid(value) for value in values):
        raise ValueError(f"{name} chứa giá trị không hợp lệ.")
    if len(values) != len(set(values)):
        raise ValueError(f"{name} không được có thành viên trùng.")
    return tuple(sorted(values))


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")
    return value.astimezone(UTC)


def _retention(value: int | None, name: str) -> None:
    if value is not None and (type(value) is not int or value <= 0):
        raise ValueError(f"{name} phải là số nguyên dương hoặc null.")


@dataclass(frozen=True, slots=True)
class _SourceScope:
    """Các chiều bắt buộc, không suy diễn khi dữ liệu chưa đầy đủ."""

    source_id: str
    provider: str
    product: str
    venue: str
    contract: str
    symbols: tuple[str, ...]
    intervals: tuple[str, ...]
    data_start: datetime
    data_end: datetime
    derived_use: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("source_id", "provider", "product", "venue", "contract"):
            value = getattr(self, name)
            pattern = _SOURCE_ID if name == "source_id" else _UPPER_TOKEN
            if not isinstance(value, str) or pattern.fullmatch(value) is None:
                raise ValueError(f"{name} phải là token ASCII hợp lệ, không có wildcard.")
        object.__setattr__(
            self,
            "symbols",
            _members(
                self.symbols, "symbols", lambda value: _UPPER_TOKEN.fullmatch(value) is not None
            ),
        )
        object.__setattr__(
            self,
            "intervals",
            _members(
                self.intervals, "intervals", lambda value: _INTERVAL.fullmatch(value) is not None
            ),
        )
        object.__setattr__(
            self,
            "derived_use",
            _members(self.derived_use, "derived_use", _DERIVED_USE.__contains__),
        )
        object.__setattr__(self, "data_start", _utc(self.data_start, "data_start"))
        object.__setattr__(self, "data_end", _utc(self.data_end, "data_end"))
        if self.data_end <= self.data_start:
            raise ValueError("data_end phải sau data_start trong khoảng nửa mở.")


@dataclass(frozen=True, slots=True)
class SourceUseScope(_SourceScope):
    """Phạm vi dùng do consumer dựng từ dữ liệu nội bộ có thẩm quyền."""

    purpose: SourceUsePurpose
    outputs: tuple[str, ...]
    retention_days: int | None

    def __post_init__(self) -> None:
        super(SourceUseScope, self).__post_init__()
        if not isinstance(self.purpose, SourceUsePurpose):
            raise ValueError("purpose phải là SourceUsePurpose.")
        object.__setattr__(
            self, "outputs", _members(self.outputs, "outputs", _OUTPUTS.__contains__)
        )
        _retention(self.retention_days, "retention_days")


@dataclass(frozen=True, slots=True)
class SourceLicenseScope(_SourceScope):
    """Phạm vi license v1 bất biến; không xác nhận tính hợp pháp của tài liệu."""

    allowed_purposes: tuple[SourceUsePurpose, ...]
    allowed_outputs: tuple[str, ...]
    max_retention_days: int | None
    attribution: str
    scope_version: ClassVar[str] = SCOPE_VERSION

    def __post_init__(self) -> None:
        super(SourceLicenseScope, self).__post_init__()
        object.__setattr__(
            self,
            "allowed_purposes",
            _members(
                self.allowed_purposes,
                "allowed_purposes",
                lambda value: isinstance(value, SourceUsePurpose),
            ),
        )
        object.__setattr__(
            self,
            "allowed_outputs",
            _members(self.allowed_outputs, "allowed_outputs", _OUTPUTS.__contains__),
        )
        _retention(self.max_retention_days, "max_retention_days")
        if not isinstance(self.attribution, str) or not self.attribution.strip():
            raise ValueError("attribution không được để trống.")

    def covers(self, use: SourceUseScope) -> bool:
        """Chỉ bao phủ khi mọi chiều đều khớp và quyền giữ dữ liệu đủ dài."""
        if not isinstance(use, SourceUseScope):
            return False
        return (
            all(
                getattr(self, name) == getattr(use, name)
                for name in ("source_id", "provider", "product", "venue", "contract")
            )
            and set(use.symbols).issubset(self.symbols)
            and set(use.intervals).issubset(self.intervals)
            and self.data_start <= use.data_start < use.data_end <= self.data_end
            and use.purpose in self.allowed_purposes
            and set(use.outputs).issubset(self.allowed_outputs)
            and set(use.derived_use).issubset(self.derived_use)
            and (
                self.max_retention_days is None
                or (
                    use.retention_days is not None and use.retention_days <= self.max_retention_days
                )
            )
        )


def canonical_scope_json(scope: SourceLicenseScope) -> str:
    """Mã hóa JSON UTF-8 v1 với UTC Z và sáu chữ số microsecond cố định."""
    if not isinstance(scope, SourceLicenseScope):
        raise ValueError("scope phải là SourceLicenseScope.")
    payload = {
        "scope_version": scope.scope_version,
        "source_id": scope.source_id,
        "provider": scope.provider,
        "product": scope.product,
        "venue": scope.venue,
        "contract": scope.contract,
        "symbols": scope.symbols,
        "intervals": scope.intervals,
        "data_start": scope.data_start.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "data_end": scope.data_end.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "allowed_purposes": scope.allowed_purposes,
        "allowed_outputs": scope.allowed_outputs,
        "max_retention_days": scope.max_retention_days,
        "attribution": scope.attribution,
        "derived_use": scope.derived_use,
    }
    return json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def scope_sha256(scope: SourceLicenseScope) -> str:
    """Băm SHA-256 đúng bytes UTF-8 của canonical scope, không hash JSONB text."""
    return hashlib.sha256(canonical_scope_json(scope).encode("utf-8")).hexdigest()
