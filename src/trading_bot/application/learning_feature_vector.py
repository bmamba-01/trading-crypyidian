"""Dựng LearningFeatureVector thuần từ chính DecisionSnapshot đã đóng băng."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import NoReturn

from trading_bot.application.decision_snapshot_builder import build_decision_snapshot_from_bars
from trading_bot.application.strategy_market_requirements import feature_requirements_for_release
from trading_bot.domain.learning.feature_vector import (
    LEARNING_FEATURE_VECTOR_SCHEMA_VERSION,
    LearningFeatureVector,
    LearningFeatureVectorFeature,
    learning_feature_schema_hash,
)
from trading_bot.domain.market.feature_snapshot import DataFreshness
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    DecisionSnapshotTimeframeLineage,
    FeatureQuality,
    SeriesQuality,
)

MINIMUM_VECTOR_DATA_COVERAGE = 0.8
_SHA256 = re.compile(r"[0-9a-f]{64}")


class LearningFeatureVectorBuildReason(StrEnum):
    """Mã từ chối ổn định cho vector không đủ lineage hoặc chất lượng."""

    INVALID_SNAPSHOT = "INVALID_SNAPSHOT"
    INVALID_RELEASE_HASH = "INVALID_RELEASE_HASH"
    UNSUPPORTED_RELEASE = "UNSUPPORTED_RELEASE"
    MISSING_LINEAGE = "MISSING_LINEAGE"
    DUPLICATE_LINEAGE = "DUPLICATE_LINEAGE"
    MISSING_PROVENANCE = "MISSING_PROVENANCE"
    DUPLICATE_PROVENANCE = "DUPLICATE_PROVENANCE"
    SOURCE_DIGEST_MISMATCH = "SOURCE_DIGEST_MISMATCH"
    UNPINNED_SOURCE = "UNPINNED_SOURCE"
    FUTURE_SOURCE = "FUTURE_SOURCE"
    FUTURE_FEATURE = "FUTURE_FEATURE"
    STALE_SOURCE = "STALE_SOURCE"
    LOW_COVERAGE = "LOW_COVERAGE"
    WARMUP_INCOMPLETE = "WARMUP_INCOMPLETE"
    QUALITY_NOT_READY = "QUALITY_NOT_READY"
    MISSING_FEATURE = "MISSING_FEATURE"
    DUPLICATE_FEATURE = "DUPLICATE_FEATURE"
    FEATURE_ORDER_MISMATCH = "FEATURE_ORDER_MISMATCH"
    NON_FINITE_FEATURE = "NON_FINITE_FEATURE"
    FEATURE_SCHEMA_MISMATCH = "FEATURE_SCHEMA_MISMATCH"
    WORKSPACE_MISMATCH = "WORKSPACE_MISMATCH"
    SNAPSHOT_CUTOFF_MISMATCH = "SNAPSHOT_CUTOFF_MISMATCH"
    SNAPSHOT_CONTENT_MISMATCH = "SNAPSHOT_CONTENT_MISMATCH"


class LearningFeatureVectorBuildError(ValueError):
    """Từ chối vector với reason code có thể audit, không tạo vector một phần."""

    def __init__(self, reason_code: LearningFeatureVectorBuildReason, detail: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"{reason_code.value}: {detail}")


def build_learning_feature_vector(
    snapshot: DecisionSnapshot,
    *,
    strategy_release_id: str,
    strategy_release_content_hash: str,
) -> LearningFeatureVector:
    """Canonical hóa snapshot chính xác; không tra cứu lại feature, source hay quyền."""
    if type(snapshot) is not DecisionSnapshot:
        _reject(LearningFeatureVectorBuildReason.INVALID_SNAPSHOT, "Cần DecisionSnapshot.")
    if not isinstance(strategy_release_content_hash, str) or not _SHA256.fullmatch(
        strategy_release_content_hash
    ):
        _reject(
            LearningFeatureVectorBuildReason.INVALID_RELEASE_HASH,
            "strategy release content hash phải là SHA-256 hex chữ thường.",
        )
    try:
        requirements = feature_requirements_for_release(strategy_release_id)
    except ValueError as error:
        raise LearningFeatureVectorBuildError(
            LearningFeatureVectorBuildReason.UNSUPPORTED_RELEASE,
            "Strategy release không có feature contract đã ghim.",
        ) from error

    if not snapshot.timeframe_lineage:
        _reject(
            LearningFeatureVectorBuildReason.MISSING_LINEAGE,
            "Snapshot legacy không có timeframe feature lineage.",
        )
    lineage_by_timeframe = _lineage_index(snapshot.timeframe_lineage)
    expected_timeframes = tuple(requirements)
    if tuple(item.timeframe for item in snapshot.timeframe_lineage) != expected_timeframes:
        _reject(
            LearningFeatureVectorBuildReason.MISSING_LINEAGE,
            "Timeframe lineage không khớp contract của strategy release.",
        )
    if tuple(item.timeframe for item in snapshot.series) != expected_timeframes:
        _reject(
            LearningFeatureVectorBuildReason.FEATURE_SCHEMA_MISMATCH,
            "Series timeframe không khớp contract của strategy release.",
        )

    expected_pairs = tuple(
        (timeframe, name)
        for timeframe, feature_names in requirements.items()
        for name in feature_names
    )
    actual_pairs = tuple((item.timeframe, item.name) for item in snapshot.features)
    if len(actual_pairs) != len(set(actual_pairs)):
        _reject(
            LearningFeatureVectorBuildReason.DUPLICATE_FEATURE,
            "Snapshot lặp feature key.",
        )
    if any(pair not in actual_pairs for pair in expected_pairs):
        _reject(
            LearningFeatureVectorBuildReason.MISSING_FEATURE,
            "Snapshot thiếu feature bắt buộc trong release contract.",
        )
    if len(actual_pairs) != len(expected_pairs):
        _reject(
            LearningFeatureVectorBuildReason.FEATURE_SCHEMA_MISMATCH,
            "Snapshot chứa feature ngoài release contract.",
        )
    if actual_pairs != expected_pairs:
        _reject(
            LearningFeatureVectorBuildReason.FEATURE_ORDER_MISMATCH,
            "Feature order phải khớp release contract theo timeframe.",
        )

    _validate_lineage(
        snapshot,
        requirements=requirements,
        lineage_by_timeframe=lineage_by_timeframe,
    )

    features: list[LearningFeatureVectorFeature] = []
    for (timeframe, name), observation in zip(expected_pairs, snapshot.features, strict=True):
        if observation.available_at > snapshot.decision_time:
            _reject(
                LearningFeatureVectorBuildReason.FUTURE_FEATURE,
                f"Feature {timeframe}:{name} available sau evidence cutoff.",
            )
        if observation.value is None:
            _reject(
                LearningFeatureVectorBuildReason.MISSING_FEATURE,
                f"Feature {timeframe}:{name} chưa có giá trị.",
            )
        if not math.isfinite(observation.value):
            _reject(
                LearningFeatureVectorBuildReason.NON_FINITE_FEATURE,
                f"Feature {timeframe}:{name} không hữu hạn.",
            )
        if observation.quality is FeatureQuality.STALE:
            _reject(
                LearningFeatureVectorBuildReason.STALE_SOURCE,
                f"Feature {timeframe}:{name} bị stale.",
            )
        if observation.quality is not FeatureQuality.READY:
            _reject(
                LearningFeatureVectorBuildReason.QUALITY_NOT_READY,
                f"Feature {timeframe}:{name} không READY.",
            )
        lineage = lineage_by_timeframe[timeframe]
        quality = dict(lineage.quality_by_feature).get(name)
        warmup_complete = dict(lineage.warmup_by_feature).get(name)
        if quality is not FeatureQuality.READY:
            _reject(
                LearningFeatureVectorBuildReason.QUALITY_NOT_READY,
                f"Quality summary của {timeframe}:{name} không READY.",
            )
        if warmup_complete is not True:
            _reject(
                LearningFeatureVectorBuildReason.WARMUP_INCOMPLETE,
                f"Warmup của {timeframe}:{name} chưa hoàn tất.",
            )
        if observation.available_at != lineage.available_at:
            _reject(
                LearningFeatureVectorBuildReason.FEATURE_SCHEMA_MISMATCH,
                f"Availability của {timeframe}:{name} khác timeframe lineage.",
            )
        unit = _unit_for_feature(name)
        if unit is None:
            _reject(
                LearningFeatureVectorBuildReason.FEATURE_SCHEMA_MISMATCH,
                f"Chưa có unit contract cho feature {name}.",
            )
        features.append(
            LearningFeatureVectorFeature(
                key=f"{timeframe}:{name}",
                timeframe=timeframe,
                name=name,
                value=float(observation.value),
                quality=observation.quality,
                available_at=observation.available_at,
                unit=unit,
            )
        )

    _validate_snapshot_content_identity(
        snapshot,
        strategy_release_id=strategy_release_id,
    )
    feature_keys = tuple(feature.key for feature in features)
    feature_schema_hash = learning_feature_schema_hash(tuple(features), snapshot.timeframe_lineage)
    workspace_ids = {item.workspace_id for item in snapshot.timeframe_lineage}
    if len(workspace_ids) != 1:
        _reject(
            LearningFeatureVectorBuildReason.WORKSPACE_MISMATCH,
            "Timeframe lineage phải cùng workspace.",
        )
    try:
        return LearningFeatureVector(
            workspace_id=next(iter(workspace_ids)),
            symbol=snapshot.symbol,
            decision_time=snapshot.decision_time,
            snapshot_id=snapshot.snapshot_id,
            snapshot_hash=snapshot.snapshot_id,
            schema_version=LEARNING_FEATURE_VECTOR_SCHEMA_VERSION,
            feature_schema_hash=feature_schema_hash,
            strategy_release_id=strategy_release_id,
            strategy_release_content_hash=strategy_release_content_hash,
            feature_keys=feature_keys,
            features=tuple(features),
            timeframe_lineage=snapshot.timeframe_lineage,
        )
    except ValueError as error:
        raise LearningFeatureVectorBuildError(
            LearningFeatureVectorBuildReason.FEATURE_SCHEMA_MISMATCH,
            "LearningFeatureVector domain contract từ chối snapshot.",
        ) from error


def _lineage_index(
    lineages: tuple[DecisionSnapshotTimeframeLineage, ...],
) -> dict[str, DecisionSnapshotTimeframeLineage]:
    if any(type(item) is not DecisionSnapshotTimeframeLineage for item in lineages):
        _reject(
            LearningFeatureVectorBuildReason.MISSING_LINEAGE,
            "Timeframe lineage không đúng kiểu domain đã ghim.",
        )
    timeframes = tuple(item.timeframe for item in lineages)
    if len(timeframes) != len(set(timeframes)):
        _reject(
            LearningFeatureVectorBuildReason.DUPLICATE_LINEAGE,
            "Snapshot lặp timeframe lineage.",
        )
    return {item.timeframe: item for item in lineages}


def _validate_lineage(
    snapshot: DecisionSnapshot,
    *,
    requirements: Mapping[str, tuple[str, ...]],
    lineage_by_timeframe: Mapping[str, DecisionSnapshotTimeframeLineage],
) -> None:
    for timeframe, expected_features in requirements.items():
        lineage = lineage_by_timeframe.get(timeframe)
        if lineage is None:
            _reject(
                LearningFeatureVectorBuildReason.MISSING_LINEAGE,
                f"Thiếu lineage timeframe {timeframe}.",
            )
        if lineage.symbol != snapshot.symbol or lineage.as_of != snapshot.decision_time:
            _reject(
                LearningFeatureVectorBuildReason.SNAPSHOT_CUTOFF_MISMATCH,
                f"Lineage timeframe {timeframe} không khớp snapshot/cutoff.",
            )
        if lineage.feature_order != tuple(expected_features):
            _reject(
                LearningFeatureVectorBuildReason.FEATURE_SCHEMA_MISMATCH,
                f"Feature order/schema timeframe {timeframe} không khớp.",
            )
        for hash_value in (
            lineage.feature_snapshot_id,
            lineage.schema_hash,
            lineage.formula_hash,
            lineage.code_hash,
        ):
            if not _SHA256.fullmatch(hash_value):
                _reject(
                    LearningFeatureVectorBuildReason.MISSING_PROVENANCE,
                    f"Timeframe {timeframe} thiếu feature/schema identity hợp lệ.",
                )
        if not lineage.source_event_ids or not lineage.source_bar_ids:
            _reject(
                LearningFeatureVectorBuildReason.MISSING_PROVENANCE,
                f"Timeframe {timeframe} thiếu source event/bar IDs.",
            )
        if len(lineage.source_event_ids) != len(set(lineage.source_event_ids)) or len(
            lineage.source_bar_ids
        ) != len(set(lineage.source_bar_ids)):
            _reject(
                LearningFeatureVectorBuildReason.DUPLICATE_PROVENANCE,
                f"Timeframe {timeframe} lặp source event/bar ID.",
            )
        expected_digest = _hash(
            {
                "timeframe": timeframe,
                "source_event_ids": list(lineage.source_event_ids),
                "source_bar_ids": list(lineage.source_bar_ids),
            }
        )
        if lineage.source_event_digest != expected_digest:
            _reject(
                LearningFeatureVectorBuildReason.SOURCE_DIGEST_MISMATCH,
                f"Source event digest timeframe {timeframe} không khớp IDs.",
            )
        if not lineage.source_identity_pinned:
            _reject(
                LearningFeatureVectorBuildReason.UNPINNED_SOURCE,
                f"Timeframe {timeframe} không có MarketBar source identity ghim.",
            )
        if any(not item.startswith("market-bar-record:") for item in lineage.source_bar_ids):
            _reject(
                LearningFeatureVectorBuildReason.UNPINNED_SOURCE,
                f"Timeframe {timeframe} source bar IDs chưa lấy từ MarketBar identity.",
            )
        if any(not item.startswith("market-bar:") for item in lineage.source_event_ids):
            _reject(
                LearningFeatureVectorBuildReason.UNPINNED_SOURCE,
                f"Timeframe {timeframe} event IDs chưa được canonical hóa từ MarketBar.",
            )
        if (
            lineage.freshness is DataFreshness.STALE
            or lineage.series_quality is SeriesQuality.STALE
        ):
            _reject(
                LearningFeatureVectorBuildReason.STALE_SOURCE,
                f"Timeframe {timeframe} đã stale tại freshness cutoff.",
            )
        if lineage.freshness is not DataFreshness.FRESH:
            _reject(
                LearningFeatureVectorBuildReason.QUALITY_NOT_READY,
                f"Freshness timeframe {timeframe} chưa READY.",
            )
        if lineage.series_quality is not SeriesQuality.READY:
            _reject(
                LearningFeatureVectorBuildReason.QUALITY_NOT_READY,
                f"Series quality timeframe {timeframe} chưa READY.",
            )
        if lineage.data_coverage < MINIMUM_VECTOR_DATA_COVERAGE:
            _reject(
                LearningFeatureVectorBuildReason.LOW_COVERAGE,
                f"Coverage timeframe {timeframe} thấp hơn {MINIMUM_VECTOR_DATA_COVERAGE:.0%}.",
            )
        if lineage.available_at is None or lineage.available_at > snapshot.decision_time:
            _reject(
                LearningFeatureVectorBuildReason.FUTURE_SOURCE,
                f"Source timeframe {timeframe} thiếu hoặc vượt evidence cutoff.",
            )
        series = snapshot.get_series(timeframe)
        if series is None:
            _reject(
                LearningFeatureVectorBuildReason.MISSING_LINEAGE,
                f"Snapshot thiếu series timeframe {timeframe}.",
            )
        if any(
            bar.close_time > snapshot.decision_time
            or bar.event_at > snapshot.decision_time
            or bar.available_at > snapshot.decision_time
            for bar in series.bars
        ):
            _reject(
                LearningFeatureVectorBuildReason.FUTURE_SOURCE,
                f"Series timeframe {timeframe} chứa bar sau evidence cutoff.",
            )


def _validate_snapshot_content_identity(
    snapshot: DecisionSnapshot,
    *,
    strategy_release_id: str,
) -> None:
    """Tái dựng bằng canonical builder và so toàn envelope với ID đã gắn."""
    workspace_ids = {item.workspace_id for item in snapshot.timeframe_lineage}
    if snapshot.freshness_as_of is None:
        _reject(
            LearningFeatureVectorBuildReason.MISSING_LINEAGE,
            "Snapshot không ghim mốc freshness ở envelope.",
        )
    if len(workspace_ids) != 1 or any(
        item.freshness_as_of != snapshot.freshness_as_of for item in snapshot.timeframe_lineage
    ):
        _reject(
            LearningFeatureVectorBuildReason.SNAPSHOT_CONTENT_MISMATCH,
            "Workspace/freshness cutoff của timeframe lineage không đồng nhất.",
        )
    try:
        canonical_snapshot = build_decision_snapshot_from_bars(
            workspace_id=next(iter(workspace_ids)),
            symbol=snapshot.symbol,
            strategy_release_id=strategy_release_id,
            decision_time=snapshot.decision_time,
            series_quality_as_of=snapshot.freshness_as_of,
            bars_by_timeframe={series.timeframe: series.bars for series in snapshot.series},
            position_is_flat=snapshot.position_is_flat,
            macro_blackout=snapshot.macro_blackout,
            macro_evidence_ids=snapshot.macro_evidence_ids,
            macro_evidence_available_at=snapshot.macro_evidence_available_at,
            macro_is_fresh=snapshot.macro_is_fresh,
            source_identity_required=True,
        )
    except ValueError as error:
        raise LearningFeatureVectorBuildError(
            LearningFeatureVectorBuildReason.SNAPSHOT_CONTENT_MISMATCH,
            "Snapshot không thể tái dựng từ bars/source identity đã ghim.",
        ) from error
    if (
        canonical_snapshot.snapshot_id != snapshot.snapshot_id
        or canonical_snapshot.series != snapshot.series
        or canonical_snapshot.features != snapshot.features
        or canonical_snapshot.timeframe_lineage != snapshot.timeframe_lineage
    ):
        _reject(
            LearningFeatureVectorBuildReason.SNAPSHOT_CONTENT_MISMATCH,
            "Snapshot ID không khớp nội dung series/features/timeframe lineage.",
        )


def _unit_for_feature(name: str) -> str | None:
    if name in {"tr", "session_vwap"} or name.startswith(("sma_", "ema_", "atr_", "bollinger_")):
        return "quote_price"
    if name.startswith(("adx_", "dx_", "plus_di_", "minus_di_", "rsi_")):
        return "index_0_100"
    if name.startswith("efficiency_ratio_"):
        return "ratio_0_1"
    if name.startswith("zscore_"):
        return "standard_deviation"
    if name.startswith("realized_volatility_"):
        return "return_ratio"
    return None


def _reject(reason: LearningFeatureVectorBuildReason, detail: str) -> NoReturn:
    raise LearningFeatureVectorBuildError(reason, detail)


def _hash(value: object) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
