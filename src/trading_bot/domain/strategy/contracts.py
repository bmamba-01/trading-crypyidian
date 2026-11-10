"""Hợp đồng dữ liệu bất biến cho đánh giá chiến lược tại một thời điểm."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from trading_bot.domain.market.feature_snapshot import DataFreshness, MarketRegime
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.strategy.recipes import ExitRecipe


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")


class FeatureQuality(StrEnum):
    """Chất lượng quan sát của một đặc trưng đã tính."""

    READY = "ready"
    MISSING = "missing"
    STALE = "stale"
    INVALID = "invalid"


class SeriesQuality(StrEnum):
    """Mức kiểm định chuỗi nến trước khi dùng cho quyết định."""

    UNKNOWN = "unknown"
    READY = "ready"
    STALE = "stale"
    INCOMPLETE = "incomplete"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class FeatureObservation:
    """Giá trị đặc trưng kèm định danh và thời điểm hệ thống biết giá trị đó."""

    feature_id: str
    name: str
    timeframe: str
    value: float | None
    previous_value: float | None
    available_at: datetime
    quality: FeatureQuality = FeatureQuality.READY

    def __post_init__(self) -> None:
        for name in ("feature_id", "name", "timeframe"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} không được để trống.")
        _aware(self.available_at, "available_at")
        for name in ("value", "previous_value"):
            value = getattr(self, name)
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{name} phải hữu hạn hoặc None.")


@dataclass(frozen=True, slots=True)
class MarketSeries:
    """Chuỗi nến cùng khung thời gian, có thể kèm quote volume đã căn chỉnh."""

    timeframe: str
    bars: tuple[OHLCVBar, ...]
    quote_volumes: tuple[float, ...] | None = None
    quality: SeriesQuality = SeriesQuality.UNKNOWN
    expected_interval: timedelta | None = None
    max_event_age: timedelta | None = None
    backfill_complete: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "bars", _immutable_tuple(self.bars, "bars"))
        if self.quote_volumes is not None:
            object.__setattr__(
                self,
                "quote_volumes",
                _immutable_tuple(self.quote_volumes, "quote_volumes"),
            )
        if not self.timeframe.strip():
            raise ValueError("timeframe không được để trống.")
        if any(bar.timeframe != self.timeframe for bar in self.bars):
            raise ValueError("Tất cả nến phải có cùng timeframe với chuỗi.")
        if any(
            left.open_time >= right.open_time
            for left, right in zip(self.bars, self.bars[1:], strict=False)
        ):
            raise ValueError("Thời gian nến phải tăng nghiêm ngặt.")
        if self.expected_interval is not None and self.expected_interval <= timedelta(0):
            raise ValueError("expected_interval phải lớn hơn 0.")
        if self.max_event_age is not None and self.max_event_age <= timedelta(0):
            raise ValueError("max_event_age phải lớn hơn 0.")
        if self.quote_volumes is not None:
            if len(self.quote_volumes) != len(self.bars):
                raise ValueError("quote volume phải có độ dài bằng chuỗi nến.")
            if any(not math.isfinite(value) or value < 0 for value in self.quote_volumes):
                raise ValueError("quote volume phải hữu hạn và không âm.")


@dataclass(frozen=True, slots=True)
class DecisionSnapshotTimeframeLineage:
    """Bằng chứng bất biến cho feature snapshot của đúng một timeframe."""

    workspace_id: UUID
    symbol: str
    timeframe: str
    feature_snapshot_id: str
    schema_version: str
    schema_hash: str
    formula_version: str
    formula_hash: str
    code_hash: str
    feature_order: tuple[str, ...]
    source_event_ids: tuple[str, ...]
    source_bar_ids: tuple[str, ...]
    source_event_digest: str
    as_of: datetime
    freshness_as_of: datetime
    available_at: datetime | None
    freshness: DataFreshness
    series_quality: SeriesQuality
    data_coverage: float
    warmup_by_feature: tuple[tuple[str, bool], ...]
    quality_by_feature: tuple[tuple[str, FeatureQuality], ...]
    quality_flags: tuple[str, ...]
    regime_version: str
    regime: MarketRegime
    regime_confidence: float
    regime_coverage: float
    regime_reason_code: str
    source_identity_pinned: bool

    def __post_init__(self) -> None:
        for name in (
            "feature_order",
            "source_event_ids",
            "source_bar_ids",
            "warmup_by_feature",
            "quality_by_feature",
            "quality_flags",
        ):
            if type(getattr(self, name)) is not tuple:
                raise ValueError(f"{name} phải là tuple bất biến.")
        if type(self.workspace_id) is not UUID:
            raise ValueError("workspace_id của lineage phải là UUID.")
        for name in (
            "symbol",
            "timeframe",
            "feature_snapshot_id",
            "schema_version",
            "formula_version",
            "regime_version",
            "regime_reason_code",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} của lineage không được để trống.")
        for name in ("schema_hash", "formula_hash", "code_hash", "source_event_digest"):
            value = getattr(self, name)
            if not _is_sha256(value):
                raise ValueError(f"{name} của lineage phải là SHA-256 hex chữ thường.")
        _aware(self.as_of, "lineage.as_of")
        _aware(self.freshness_as_of, "lineage.freshness_as_of")
        if self.freshness_as_of < self.as_of:
            raise ValueError("Mốc freshness của lineage không được trước evidence cutoff.")
        if self.available_at is not None:
            _aware(self.available_at, "lineage.available_at")
        if (
            type(self.freshness) is not DataFreshness
            or type(self.series_quality) is not SeriesQuality
        ):
            raise ValueError("Lineage cần enum freshness và series quality tường minh.")
        if not math.isfinite(self.data_coverage) or not 0.0 <= self.data_coverage <= 1.0:
            raise ValueError("data_coverage của lineage phải nằm trong [0, 1].")
        for name in ("regime_confidence", "regime_coverage"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} của lineage phải nằm trong [0, 1].")
        if type(self.regime) is not MarketRegime:
            raise ValueError("regime của lineage phải là MarketRegime.")
        if type(self.source_identity_pinned) is not bool:
            raise ValueError("source_identity_pinned phải là bool.")
        if not self.feature_order or any(
            not isinstance(name, str) or not name.strip() for name in self.feature_order
        ):
            raise ValueError("Lineage cần feature_order không rỗng.")
        if len(self.feature_order) != len(set(self.feature_order)):
            raise ValueError("feature_order của lineage không được trùng.")
        if len(self.source_event_ids) != len(self.source_bar_ids):
            raise ValueError("Source event và bar IDs phải cùng độ dài.")
        for field_name in ("source_event_ids", "source_bar_ids"):
            values = getattr(self, field_name)
            if any(not value.strip() for value in values) or len(values) != len(set(values)):
                raise ValueError(f"{field_name} cần IDs không rỗng và duy nhất.")
        if any(type(item) is not tuple or len(item) != 2 for item in self.warmup_by_feature):
            raise ValueError("Warmup summary cần các cặp tuple.")
        if tuple(name for name, _ in self.warmup_by_feature) != self.feature_order:
            raise ValueError("Warmup summary phải theo đúng feature_order.")
        if any(type(value) is not bool for _, value in self.warmup_by_feature):
            raise ValueError("Warmup summary chỉ nhận bool.")
        if any(type(item) is not tuple or len(item) != 2 for item in self.quality_by_feature):
            raise ValueError("Quality summary cần các cặp tuple.")
        if tuple(name for name, _ in self.quality_by_feature) != self.feature_order:
            raise ValueError("Quality summary phải theo đúng feature_order.")
        if any(type(value) is not FeatureQuality for _, value in self.quality_by_feature):
            raise ValueError("Quality summary cần FeatureQuality tường minh.")
        if len(self.quality_flags) != len(set(self.quality_flags)) or any(
            not item.strip() for item in self.quality_flags
        ):
            raise ValueError("quality_flags phải không rỗng và duy nhất.")
        if self.source_event_digest != _source_event_digest(
            self.timeframe, self.source_event_ids, self.source_bar_ids
        ):
            raise ValueError("source_event_digest không khớp source IDs của timeframe.")


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    """Đầu vào đóng băng cho một lần đánh giá, không tự loại dữ liệu tương lai."""

    symbol: str
    decision_time: datetime
    snapshot_id: str
    series: tuple[MarketSeries, ...]
    features: tuple[FeatureObservation, ...]
    position_is_flat: bool | None = None
    macro_blackout: bool | None = None
    macro_evidence_ids: tuple[str, ...] = ()
    macro_evidence_available_at: datetime | None = None
    macro_is_fresh: bool | None = None
    # Rỗng chỉ để giữ tương thích với snapshot legacy không đủ điều kiện training.
    timeframe_lineage: tuple[DecisionSnapshotTimeframeLineage, ...] = ()
    freshness_as_of: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("series", "features", "macro_evidence_ids", "timeframe_lineage"):
            object.__setattr__(self, name, _immutable_tuple(getattr(self, name), name))
        if not self.symbol.strip():
            raise ValueError("symbol không được để trống.")
        if not self.snapshot_id.strip():
            raise ValueError("snapshot_id không được để trống.")
        _aware(self.decision_time, "decision_time")
        if self.freshness_as_of is not None:
            _aware(self.freshness_as_of, "freshness_as_of")
            if self.freshness_as_of < self.decision_time:
                raise ValueError("freshness_as_of không được trước evidence cutoff.")
        timeframes = [item.timeframe for item in self.series]
        if len(timeframes) != len(set(timeframes)):
            raise ValueError("Không được lặp timeframe trong snapshot.")
        if self.timeframe_lineage:
            if self.freshness_as_of is None:
                raise ValueError("Snapshot có timeframe lineage phải ghim freshness_as_of.")
            if any(
                type(item) is not DecisionSnapshotTimeframeLineage
                for item in self.timeframe_lineage
            ):
                raise ValueError("timeframe_lineage phải chứa domain lineage đã đóng băng.")
            lineage_timeframes = tuple(item.timeframe for item in self.timeframe_lineage)
            if len(lineage_timeframes) != len(set(lineage_timeframes)):
                raise ValueError("Không được lặp timeframe lineage trong snapshot.")
            if lineage_timeframes != tuple(timeframes):
                raise ValueError("Timeframe lineage phải theo đúng thứ tự các series.")
            if any(item.symbol != self.symbol for item in self.timeframe_lineage):
                raise ValueError("Timeframe lineage phải cùng symbol với snapshot.")
            if any(item.freshness_as_of != self.freshness_as_of for item in self.timeframe_lineage):
                raise ValueError("Timeframe lineage phải khớp freshness_as_of của snapshot.")
        keys = [(item.name, item.timeframe) for item in self.features]
        if len(keys) != len(set(keys)):
            raise ValueError("feature key bị lặp trong snapshot.")
        if any(bar.symbol != self.symbol for item in self.series for bar in item.bars):
            raise ValueError("Nến trong snapshot phải cùng symbol.")
        if self.position_is_flat is not None and not isinstance(self.position_is_flat, bool):
            raise ValueError("position_is_flat chỉ nhận bool hoặc None.")
        if self.macro_blackout is not None and not isinstance(self.macro_blackout, bool):
            raise ValueError("macro_blackout chỉ nhận bool hoặc None.")
        if self.macro_evidence_available_at is not None:
            _aware(self.macro_evidence_available_at, "macro_evidence_available_at")
        if self.macro_is_fresh is not None and not isinstance(self.macro_is_fresh, bool):
            raise ValueError("macro_is_fresh chỉ nhận bool hoặc None.")
        if len(self.macro_evidence_ids) != len(set(self.macro_evidence_ids)):
            raise ValueError("Không được lặp macro evidence ID.")

    def get_series(self, timeframe: str) -> MarketSeries | None:
        """Tìm chuỗi nến theo khung thời gian."""
        return next((item for item in self.series if item.timeframe == timeframe), None)

    def get_feature(self, name: str, timeframe: str) -> FeatureObservation | None:
        """Tìm đặc trưng theo tên và khung thời gian."""
        return next(
            (item for item in self.features if item.name == name and item.timeframe == timeframe),
            None,
        )


def _source_event_digest(
    timeframe: str,
    source_event_ids: tuple[str, ...],
    source_bar_ids: tuple[str, ...],
) -> str:
    payload = {
        "timeframe": timeframe,
        "source_event_ids": list(source_event_ids),
        "source_bar_ids": list(source_bar_ids),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _immutable_tuple(value: object, name: str) -> tuple[object, ...]:
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{name} phải là list hoặc tuple để đóng băng.")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class StrategySelection:
    """Tham chiếu phiên bản cấu hình đã công bố và các bản phát hành được chọn."""

    settings_version_id: str
    active_release_ids: tuple[str, ...]
    published: bool
    parameters: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if not self.settings_version_id.strip():
            raise ValueError("settings_version_id không được để trống.")
        if len(self.active_release_ids) != len(set(self.active_release_ids)):
            raise ValueError("Không được lặp strategy release.")
        if any(not release_id.strip() for release_id in self.active_release_ids):
            raise ValueError("strategy release không được để trống.")
        parameter_keys = tuple(key for key, _ in self.parameters)
        if len(parameter_keys) != len(set(parameter_keys)) or any(
            not key.strip() for key in parameter_keys
        ):
            raise ValueError("strategy parameter cần key không rỗng và duy nhất.")
        supported_ranges = {
            "donchian_lookback": (20, 200),
            "volume_median_lookback": (5, 100),
        }
        for key, value in self.parameters:
            bounds = supported_ranges.get(key)
            if bounds is None:
                raise ValueError(f"strategy parameter '{key}' không được hỗ trợ.")
            if type(value) is not int or not bounds[0] <= value <= bounds[1]:
                raise ValueError(
                    f"strategy parameter '{key}' phải là số nguyên trong miền "
                    f"{bounds[0]}–{bounds[1]}."
                )


@dataclass(frozen=True, slots=True)
class StrategyCandidate:
    """Tín hiệu giả định có cửa sổ hiệu lực, bằng chứng và kế hoạch thoát."""

    strategy_id: str
    strategy_release_id: str
    release_content_hash: str
    settings_version_id: str
    snapshot_id: str
    symbol: str
    direction: str
    reference_price: Decimal
    atr_value: float
    signal_at: datetime
    eligible_after: datetime
    expires_at: datetime
    recipe: ExitRecipe
    evidence_feature_ids: tuple[str, ...]
    evidence_bar_keys: tuple[str, ...]
    macro_evidence_ids: tuple[str, ...]
    geometry_version: str | None
    reason_code: str
    detail_vi: str

    def __post_init__(self) -> None:
        for name in (
            "strategy_id",
            "strategy_release_id",
            "settings_version_id",
            "snapshot_id",
            "symbol",
            "reason_code",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} không được để trống.")
        if self.direction not in {"long", "short"}:
            raise ValueError("direction chỉ nhận long hoặc short.")
        if (
            not isinstance(self.reference_price, Decimal)
            or not self.reference_price.is_finite()
            or self.reference_price <= 0
        ):
            raise ValueError("reference_price phải là Decimal hữu hạn dương.")
        if not math.isfinite(self.atr_value) or self.atr_value <= 0:
            raise ValueError("atr_value phải hữu hạn và dương.")
        for name in ("signal_at", "eligible_after", "expires_at"):
            _aware(getattr(self, name), name)
        if not self.signal_at < self.eligible_after < self.expires_at:
            raise ValueError("Cửa sổ candidate phải sau tín hiệu và có thời hạn dương.")
        if not self.evidence_feature_ids or len(self.evidence_feature_ids) != len(
            set(self.evidence_feature_ids)
        ):
            raise ValueError("Candidate cần danh sách evidence feature không rỗng và duy nhất.")
        if len(self.release_content_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.release_content_hash
        ):
            raise ValueError("release_content_hash phải là SHA-256 dạng hex chữ thường.")
        if not self.evidence_bar_keys or len(self.evidence_bar_keys) != len(
            set(self.evidence_bar_keys)
        ):
            raise ValueError("Candidate cần natural key nến bằng chứng duy nhất.")
        if len(self.macro_evidence_ids) != len(set(self.macro_evidence_ids)):
            raise ValueError("Macro evidence ID không được trùng.")
        if any(
            not item.strip()
            for item in (
                *self.evidence_feature_ids,
                *self.evidence_bar_keys,
                *self.macro_evidence_ids,
            )
        ):
            raise ValueError("Evidence ID và natural key không được để trống.")

    @property
    def candidate_id(self) -> str:
        """Hash định danh toàn bộ dữ liệu strategy, kế hoạch thoát và evidence."""
        recipe = {
            name: _canonical_candidate_value(getattr(self.recipe, name))
            for name in self.recipe.__dataclass_fields__
        }
        payload = {
            "strategy_id": self.strategy_id,
            "strategy_release_id": self.strategy_release_id,
            "release_content_hash": self.release_content_hash,
            "settings_version_id": self.settings_version_id,
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "reference_price": str(self.reference_price),
            "atr_value": self.atr_value,
            "signal_at": self.signal_at.isoformat(),
            "eligible_after": self.eligible_after.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "recipe": recipe,
            "evidence_feature_ids": sorted(self.evidence_feature_ids),
            "evidence_bar_keys": sorted(self.evidence_bar_keys),
            "macro_evidence_ids": sorted(self.macro_evidence_ids),
            "geometry_version": self.geometry_version,
            "reason_code": self.reason_code,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_candidate_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_canonical_candidate_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class NoTrade:
    """Kết quả từ chối có mã và giải thích để ghi nhận hành vi."""

    strategy_id: str
    code: str
    detail_vi: str
    missing_features: tuple[str, ...] = ()
    gate_details: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


class Strategy(Protocol):
    """Giao diện thuần cho một rule chiến lược có phiên bản."""

    strategy_id: str
    release_id: str

    def evaluate(
        self, snapshot: DecisionSnapshot, settings: StrategySelection
    ) -> StrategyCandidate | NoTrade: ...
