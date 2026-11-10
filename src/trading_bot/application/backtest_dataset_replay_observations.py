"""Dựng replay observation xác định từ dataset bundle đã kiểm quyền và hash."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime

from trading_bot.application.backtest import ReplayObservation
from trading_bot.application.backtest_dataset_replay_bundle import BacktestDatasetReplayBundle
from trading_bot.application.decision_snapshot_builder import (
    build_decision_snapshot_from_bars,
)
from trading_bot.application.strategy_market_requirements import (
    entry_timeframe_for_release,
    feature_requirements_for_release,
)
from trading_bot.domain.market.models import OHLCVBar


class ReplayObservationBuildError(ValueError):
    """Từ chối bundle không thể biểu diễn an toàn thành observation point-in-time."""


def build_backtest_dataset_replay_observations(
    bundle: BacktestDatasetReplayBundle,
    *,
    strategy_release_id: str,
    position_is_flat_at: Callable[[datetime], bool | None] | None = None,
) -> tuple[ReplayObservation, ...]:
    """Tạo snapshot ở từng nến entry đã đóng và khả dụng trong evaluation window.

    Mỗi timeframe chỉ thấy bars có `close_time` và `available_at` không sau
    decision time. Builder không tự tạo position-state hoặc macro evidence.
    """
    if not isinstance(bundle, BacktestDatasetReplayBundle):
        raise ReplayObservationBuildError("Cần BacktestDatasetReplayBundle đã kiểm định.")
    if any(bar.available_at < bar.close_time for item in bundle.components for bar in item.bars):
        raise ReplayObservationBuildError("Bar available trước khi nến đóng.")
    requirements = feature_requirements_for_release(strategy_release_id)
    decision_timeframe = entry_timeframe_for_release(strategy_release_id)
    if decision_timeframe is None:
        raise ReplayObservationBuildError("Strategy release chưa có timeframe quyết định.")
    if decision_timeframe not in requirements:
        raise ReplayObservationBuildError("Feature contract thiếu timeframe quyết định.")
    components = {item.manifest.interval: item for item in bundle.components}
    decision_component = components.get(decision_timeframe)
    if decision_component is None:
        raise ReplayObservationBuildError("Bundle thiếu timeframe quyết định của strategy.")
    missing_timeframes = tuple(
        timeframe for timeframe in requirements if timeframe not in components
    )
    if missing_timeframes:
        raise ReplayObservationBuildError(
            f"Bundle thiếu timeframe bắt buộc {missing_timeframes[0]} cho {strategy_release_id}."
        )

    decision_bars = tuple(
        sorted(
            (
                bar
                for bar in decision_component.bars
                if bundle.evaluation_start <= bar.open_time < bundle.range_end
            ),
            key=lambda bar: bar.open_time,
        )
    )
    if not decision_bars:
        raise ReplayObservationBuildError("Bundle không có nến quyết định trong evaluation window.")
    if any(bar.available_at >= bundle.range_end for bar in decision_bars):
        raise ReplayObservationBuildError(
            "Nến quyết định available sau range_end; cần mở rộng evaluation window."
        )

    observations: list[ReplayObservation] = []
    source_identity_pinned = all(
        components[timeframe].manifest.has_pinned_source_identities for timeframe in requirements
    )
    for decision_bar in decision_bars:
        decision_time = decision_bar.available_at.astimezone(UTC)
        position_is_flat = (
            position_is_flat_at(decision_time) if position_is_flat_at is not None else None
        )
        event_id = _event_id(
            bundle.dataset_hash,
            strategy_release_id,
            decision_timeframe,
            decision_bar,
        )
        snapshot = build_decision_snapshot_from_bars(
            workspace_id=bundle.workspace_id,
            symbol=bundle.symbol,
            strategy_release_id=strategy_release_id,
            decision_time=decision_time,
            series_quality_as_of=decision_time,
            bars_by_timeframe={timeframe: components[timeframe].bars for timeframe in requirements},
            position_is_flat=position_is_flat,
            source_identity_required=source_identity_pinned,
            legacy_identity_namespace=(None if source_identity_pinned else bundle.dataset_hash),
        )
        observations.append(
            ReplayObservation(
                event_id=event_id,
                event_at=decision_bar.close_time,
                available_at=decision_bar.available_at,
                snapshot=snapshot,
            )
        )
    return tuple(observations)


def _event_id(
    bundle_hash: str,
    strategy_release_id: str,
    decision_timeframe: str,
    bar: OHLCVBar,
) -> str:
    return replay_decision_event_id(
        bundle_hash=bundle_hash,
        strategy_release_id=strategy_release_id,
        decision_timeframe=decision_timeframe,
        decision_open_time=bar.open_time,
    )


def replay_decision_event_id(
    *,
    bundle_hash: str,
    strategy_release_id: str,
    decision_timeframe: str,
    decision_open_time: datetime,
) -> str:
    """Dùng cùng identity metadata cho observation và inventory proof, giữ nguyên bytes."""
    return _hash(
        {
            "bundle_hash": bundle_hash,
            "decision_open_time": _utc_iso(decision_open_time),
            "decision_timeframe": decision_timeframe,
            "strategy_release_id": strategy_release_id,
        }
    )


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _hash(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
