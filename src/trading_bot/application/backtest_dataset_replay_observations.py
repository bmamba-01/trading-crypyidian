"""Dựng replay observation xác định từ dataset bundle đã kiểm quyền và hash."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from datetime import UTC, datetime

from trading_bot.application.backtest import ReplayObservation
from trading_bot.application.backtest_dataset_replay_bundle import BacktestDatasetReplayBundle
from trading_bot.application.strategy_market_requirements import (
    entry_timeframe_for_release,
    feature_requirements_for_release,
    feature_settings_for,
)
from trading_bot.domain.market.feature_snapshot import (
    DataFreshness,
    FeatureCalculator,
    FeatureSnapshot,
)
from trading_bot.domain.market.models import MarketSnapshot, OHLCVBar
from trading_bot.domain.market.validation import BarSeriesQuality, inspect_bar_series
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    FeatureObservation,
    FeatureQuality,
    MarketSeries,
    SeriesQuality,
)


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
    for decision_bar in decision_bars:
        decision_time = decision_bar.available_at.astimezone(UTC)
        series: list[MarketSeries] = []
        features: list[FeatureObservation] = []
        feature_snapshot_ids: list[str] = []

        for timeframe, feature_names in requirements.items():
            component = components.get(timeframe)
            if component is None:
                raise ReplayObservationBuildError(
                    f"Bundle thiếu timeframe bắt buộc {timeframe} cho {strategy_release_id}."
                )
            settings = feature_settings_for(timeframe)
            visible_bars = tuple(
                sorted(
                    (
                        bar
                        for bar in component.bars
                        if bar.is_closed
                        and bar.close_time <= decision_time
                        and bar.event_at <= decision_time
                        and bar.available_at <= decision_time
                    ),
                    key=lambda bar: bar.open_time,
                )
            )
            if not visible_bars:
                series.append(
                    MarketSeries(
                        timeframe=timeframe,
                        bars=(),
                        quality=SeriesQuality.INCOMPLETE,
                        expected_interval=settings.expected_interval,
                        max_event_age=settings.max_event_age,
                        backfill_complete=False,
                    )
                )
                continue

            event_ids = tuple(
                _bar_event_id(bundle.dataset_hash, timeframe, bar) for bar in visible_bars
            )
            feature_snapshot = FeatureCalculator(settings).calculate(
                MarketSnapshot(as_of=decision_time, bars=visible_bars),
                workspace_id=bundle.workspace_id,
                input_event_ids=event_ids,
                symbol=bundle.symbol,
                timeframe=timeframe,
                computed_at=decision_time,
            )
            previous_snapshot = (
                FeatureCalculator(settings).calculate(
                    MarketSnapshot(as_of=decision_time, bars=visible_bars[:-1]),
                    workspace_id=bundle.workspace_id,
                    input_event_ids=event_ids[:-1],
                    symbol=bundle.symbol,
                    timeframe=timeframe,
                    computed_at=decision_time,
                )
                if len(visible_bars) > 1
                else None
            )
            quality_report = inspect_bar_series(
                visible_bars,
                as_of=decision_time,
                expected_interval=settings.expected_interval,
                max_event_age=settings.max_event_age,
                expected_range_start=visible_bars[0].open_time,
                expected_range_end=visible_bars[-1].open_time + settings.expected_interval,
                backfill_complete=True,
            )
            series.append(
                MarketSeries(
                    timeframe=timeframe,
                    bars=visible_bars,
                    quote_volumes=_quote_volumes(visible_bars),
                    quality=_series_quality(quality_report),
                    expected_interval=settings.expected_interval,
                    max_event_age=settings.max_event_age,
                    backfill_complete=True,
                )
            )
            feature_snapshot_ids.append(feature_snapshot.snapshot_id)
            feature_available_at = max(bar.available_at for bar in visible_bars)
            for name in feature_names:
                value = feature_snapshot.features.get(name)
                previous_value = (
                    previous_snapshot.features.get(name) if previous_snapshot is not None else None
                )
                features.append(
                    FeatureObservation(
                        feature_id=f"{feature_snapshot.snapshot_id}:{name}",
                        name=name,
                        timeframe=timeframe,
                        value=value,
                        previous_value=previous_value,
                        available_at=feature_available_at,
                        quality=_feature_quality(feature_snapshot, value),
                    )
                )

        position_is_flat = (
            position_is_flat_at(decision_time) if position_is_flat_at is not None else None
        )
        event_id = _event_id(
            bundle.dataset_hash,
            strategy_release_id,
            decision_timeframe,
            decision_bar,
        )
        snapshot_id = _snapshot_id(
            bundle_hash=bundle.dataset_hash,
            event_id=event_id,
            strategy_release_id=strategy_release_id,
            decision_time=decision_time,
            feature_snapshot_ids=tuple(feature_snapshot_ids),
            series=tuple(series),
            position_is_flat=position_is_flat,
        )
        snapshot = DecisionSnapshot(
            symbol=bundle.symbol,
            decision_time=decision_time,
            snapshot_id=snapshot_id,
            series=tuple(series),
            features=tuple(features),
            position_is_flat=position_is_flat,
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


def _bar_event_id(bundle_hash: str, timeframe: str, bar: OHLCVBar) -> str:
    return f"{bundle_hash}:{timeframe}:{_utc_iso(bar.open_time)}"


def _event_id(
    bundle_hash: str,
    strategy_release_id: str,
    decision_timeframe: str,
    bar: OHLCVBar,
) -> str:
    return _hash(
        {
            "bundle_hash": bundle_hash,
            "decision_open_time": _utc_iso(bar.open_time),
            "decision_timeframe": decision_timeframe,
            "strategy_release_id": strategy_release_id,
        }
    )


def _snapshot_id(
    *,
    bundle_hash: str,
    event_id: str,
    strategy_release_id: str,
    decision_time: datetime,
    feature_snapshot_ids: tuple[str, ...],
    series: tuple[MarketSeries, ...],
    position_is_flat: bool | None,
) -> str:
    return _hash(
        {
            "bundle_hash": bundle_hash,
            "decision_time": _utc_iso(decision_time),
            "event_id": event_id,
            "feature_snapshot_ids": list(feature_snapshot_ids),
            "position_is_flat": position_is_flat,
            "series_bar_keys": {
                item.timeframe: [
                    _bar_event_id(bundle_hash, item.timeframe, bar) for bar in item.bars
                ]
                for item in series
            },
            "strategy_release_id": strategy_release_id,
        }
    )


def _series_quality(report: BarSeriesQuality) -> SeriesQuality:
    if report.is_usable:
        return SeriesQuality.READY
    if report.stale:
        return SeriesQuality.STALE
    if report.duplicate_keys or report.future_available_count or report.unclosed_count:
        return SeriesQuality.INVALID
    return SeriesQuality.INCOMPLETE


def _feature_quality(snapshot: FeatureSnapshot, value: float | None) -> FeatureQuality:
    if snapshot.data_freshness is DataFreshness.STALE:
        return FeatureQuality.STALE
    if value is None:
        return FeatureQuality.MISSING
    if not snapshot.is_usable:
        return FeatureQuality.INVALID
    return FeatureQuality.READY


def _quote_volumes(bars: tuple[OHLCVBar, ...]) -> tuple[float, ...] | None:
    if not bars or any(bar.quote_volume is None for bar in bars):
        return None
    values = tuple(float(bar.quote_volume) for bar in bars if bar.quote_volume is not None)
    return values if all(math.isfinite(value) for value in values) else None


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
