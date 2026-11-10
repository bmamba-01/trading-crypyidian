"""Hợp đồng vector học bất biến, có lineage theo timeframe và hash canonical."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from trading_bot.domain.strategy.contracts import (
    DecisionSnapshotTimeframeLineage,
    FeatureQuality,
)

LEARNING_FEATURE_VECTOR_SCHEMA_VERSION = "crypidian-learning-feature-vector-v1"
LEARNING_FEATURE_VECTOR_BUILDER_VERSION = "crypidian-learning-vector-builder-v1"


@dataclass(frozen=True, slots=True)
class LearningFeatureVectorFeature:
    """Một giá trị số hữu hạn với khóa, đơn vị, chất lượng và thời điểm khả dụng."""

    key: str
    timeframe: str
    name: str
    value: float
    quality: FeatureQuality
    available_at: datetime
    unit: str
    value_type: str = "float64"

    def __post_init__(self) -> None:
        for name in ("key", "timeframe", "name", "unit", "value_type"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} không được để trống.")
        if self.key != f"{self.timeframe}:{self.name}":
            raise ValueError("Feature key phải có dạng timeframe:name.")
        if type(self.value) not in (float, int) or isinstance(self.value, bool):
            raise ValueError("Feature vector chỉ nhận giá trị số float64.")
        try:
            float_value = float(self.value)
        except (OverflowError, ValueError) as error:
            raise ValueError(
                "Feature vector không nhận giá trị ngoài miền float64 hữu hạn."
            ) from error
        if not math.isfinite(float_value):
            raise ValueError("Feature vector không nhận NaN hoặc infinity.")
        if type(self.quality) is not FeatureQuality or self.quality is not FeatureQuality.READY:
            raise ValueError("Feature vector chỉ nhận quality READY.")
        if self.value_type != "float64":
            raise ValueError("Feature vector chỉ hỗ trợ value_type float64.")
        _aware(self.available_at, "available_at")
        object.__setattr__(self, "value", float_value)


@dataclass(frozen=True, slots=True)
class LearningFeatureVector:
    """Vector feature đã canonical hóa; không mang source-rights authorization."""

    workspace_id: UUID
    symbol: str
    decision_time: datetime
    snapshot_id: str
    snapshot_hash: str
    schema_version: str
    feature_schema_hash: str
    strategy_release_id: str
    strategy_release_content_hash: str
    feature_keys: tuple[str, ...]
    features: tuple[LearningFeatureVectorFeature, ...]
    timeframe_lineage: tuple[DecisionSnapshotTimeframeLineage, ...]
    vector_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("feature_keys", "features", "timeframe_lineage"):
            if type(getattr(self, name)) is not tuple:
                raise ValueError(f"{name} phải là tuple bất biến.")
        if type(self.workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        for name in ("symbol", "snapshot_id", "strategy_release_id", "schema_version"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} không được để trống.")
        _aware(self.decision_time, "decision_time")
        for name in (
            "snapshot_id",
            "snapshot_hash",
            "feature_schema_hash",
            "strategy_release_content_hash",
        ):
            if not _is_sha256(getattr(self, name)):
                raise ValueError(f"{name} phải là SHA-256 hex chữ thường.")
        if self.snapshot_hash != self.snapshot_id:
            raise ValueError("snapshot_hash phải khớp content ID của DecisionSnapshot.")
        if not self.feature_keys or len(self.feature_keys) != len(set(self.feature_keys)):
            raise ValueError("feature_keys phải không rỗng và duy nhất.")
        if tuple(feature.key for feature in self.features) != self.feature_keys:
            raise ValueError("Feature values phải theo đúng feature_keys canonical.")
        if not self.timeframe_lineage:
            raise ValueError("LearningFeatureVector cần timeframe lineage.")
        if any(type(item) is not LearningFeatureVectorFeature for item in self.features):
            raise ValueError("features phải chứa LearningFeatureVectorFeature.")
        if any(
            type(item) is not DecisionSnapshotTimeframeLineage for item in self.timeframe_lineage
        ):
            raise ValueError("timeframe_lineage phải chứa domain lineage đã đóng băng.")
        if tuple(item.timeframe for item in self.timeframe_lineage) != tuple(
            dict.fromkeys(feature.timeframe for feature in self.features)
        ):
            raise ValueError("Feature timeframe phải khớp thứ tự timeframe lineage.")
        if any(
            item.workspace_id != self.workspace_id
            or item.symbol != self.symbol
            or item.as_of != self.decision_time
            for item in self.timeframe_lineage
        ):
            raise ValueError("Timeframe lineage khác workspace/symbol/decision time của vector.")
        if any(feature.available_at > self.decision_time for feature in self.features):
            raise ValueError("Feature vector không được có available_at ở tương lai.")
        if self.feature_schema_hash != learning_feature_schema_hash(
            self.features,
            self.timeframe_lineage,
        ):
            raise ValueError("feature_schema_hash không khớp schema canonical.")
        object.__setattr__(self, "vector_hash", _hash(self.canonical_payload()))

    def canonical_payload(self) -> dict[str, object]:
        """Trả payload ổn định để kiểm tra hash và so parity runtime/replay."""
        return {
            "decision_time": _utc_iso(self.decision_time),
            "feature_keys": list(self.feature_keys),
            "feature_schema_hash": self.feature_schema_hash,
            "features": [
                {
                    "available_at": _utc_iso(feature.available_at),
                    "key": feature.key,
                    "name": feature.name,
                    "quality": feature.quality.value,
                    "timeframe": feature.timeframe,
                    "unit": feature.unit,
                    "value": feature.value,
                    "value_type": feature.value_type,
                }
                for feature in self.features
            ],
            "schema_version": self.schema_version,
            "snapshot_hash": self.snapshot_hash,
            "snapshot_id": self.snapshot_id,
            "strategy_release_content_hash": self.strategy_release_content_hash,
            "strategy_release_id": self.strategy_release_id,
            "timeframe_lineage": [_lineage_payload(item) for item in self.timeframe_lineage],
            "workspace_id": str(self.workspace_id),
            "symbol": self.symbol,
        }

    def canonical_json(self) -> str:
        """Serialize payload với thứ tự JSON ổn định."""
        return _canonical_json(self.canonical_payload())


def learning_feature_schema_hash(
    features: tuple[LearningFeatureVectorFeature, ...],
    timeframe_lineage: tuple[DecisionSnapshotTimeframeLineage, ...],
) -> str:
    """Hash schema tổng hợp từ key/order, đơn vị, kiểu và contract từng timeframe."""
    schema_payload = {
        "builder_version": LEARNING_FEATURE_VECTOR_BUILDER_VERSION,
        "feature_schema": [
            {
                "key": feature.key,
                "name": feature.name,
                "timeframe": feature.timeframe,
                "unit": feature.unit,
                "value_type": feature.value_type,
            }
            for feature in features
        ],
        "schema_version": LEARNING_FEATURE_VECTOR_SCHEMA_VERSION,
        "timeframes": [
            {
                "code_hash": item.code_hash,
                "feature_order": list(item.feature_order),
                "formula_hash": item.formula_hash,
                "formula_version": item.formula_version,
                "regime_version": item.regime_version,
                "schema_hash": item.schema_hash,
                "schema_version": item.schema_version,
                "timeframe": item.timeframe,
            }
            for item in timeframe_lineage
        ],
    }
    return _hash(schema_payload)


def _lineage_payload(lineage: DecisionSnapshotTimeframeLineage) -> dict[str, object]:
    return {
        "as_of": _utc_iso(lineage.as_of),
        "available_at": None if lineage.available_at is None else _utc_iso(lineage.available_at),
        "code_hash": lineage.code_hash,
        "data_coverage": lineage.data_coverage,
        "feature_order": list(lineage.feature_order),
        "feature_snapshot_id": lineage.feature_snapshot_id,
        "formula_hash": lineage.formula_hash,
        "formula_version": lineage.formula_version,
        "freshness": lineage.freshness.value,
        "freshness_as_of": _utc_iso(lineage.freshness_as_of),
        "quality_by_feature": [
            [name, quality.value] for name, quality in lineage.quality_by_feature
        ],
        "quality_flags": list(lineage.quality_flags),
        "regime": lineage.regime.value,
        "regime_confidence": lineage.regime_confidence,
        "regime_coverage": lineage.regime_coverage,
        "regime_reason_code": lineage.regime_reason_code,
        "regime_version": lineage.regime_version,
        "schema_hash": lineage.schema_hash,
        "schema_version": lineage.schema_version,
        "series_quality": lineage.series_quality.value,
        "source_bar_ids": list(lineage.source_bar_ids),
        "source_event_digest": lineage.source_event_digest,
        "source_event_ids": list(lineage.source_event_ids),
        "source_identity_pinned": lineage.source_identity_pinned,
        "symbol": lineage.symbol,
        "timeframe": lineage.timeframe,
        "warmup_by_feature": [[name, complete] for name, complete in lineage.warmup_by_feature],
        "workspace_id": str(lineage.workspace_id),
    }


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
