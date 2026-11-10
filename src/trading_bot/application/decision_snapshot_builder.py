"""Dựng và định danh DecisionSnapshot point-in-time dùng chung cho runtime/replay."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from trading_bot.application.backtest_dataset_manifest import MarketBarIdentityMetadata
from trading_bot.application.strategy_market_requirements import (
    feature_requirements_for_release,
    feature_settings_for,
)
from trading_bot.domain.market.feature_snapshot import (
    DataFreshness,
    FeatureCalculator,
    FeatureSnapshot,
)
from trading_bot.domain.market.models import MarketBarSourceIdentity, MarketSnapshot, OHLCVBar
from trading_bot.domain.market.validation import BarSeriesQuality, inspect_bar_series
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    DecisionSnapshotTimeframeLineage,
    FeatureObservation,
    FeatureQuality,
    MarketSeries,
    SeriesQuality,
)

DECISION_SNAPSHOT_SCHEMA_VERSION = "crypidian-decision-snapshot-v2"
REGIME_RULES_VERSION = "crypidian-regime-rules-v1"


class DecisionSnapshotBuildError(ValueError):
    """Từ chối snapshot thiếu lineage, sai scope hoặc chứa dữ liệu tương lai."""


def build_decision_snapshot_from_bars(
    *,
    workspace_id: UUID,
    symbol: str,
    strategy_release_id: str,
    decision_time: datetime,
    series_quality_as_of: datetime,
    bars_by_timeframe: Mapping[str, Sequence[OHLCVBar]],
    position_is_flat: bool | None = None,
    macro_blackout: bool | None = None,
    macro_evidence_ids: tuple[str, ...] = (),
    macro_evidence_available_at: datetime | None = None,
    macro_is_fresh: bool | None = None,
    source_identity_required: bool,
    legacy_identity_namespace: str | None = None,
) -> DecisionSnapshot:
    """Tạo snapshot và content ID từ cùng contract ở runtime và replay.

    `decision_time` là cutoff của evidence đã đóng băng. `series_quality_as_of` là
    mốc kiểm freshness tại lúc đánh giá và có thể muộn hơn cutoff; nó không làm
    thay đổi feature calculation hay thời điểm dữ liệu được phép nhìn thấy.
    """
    _require_aware(decision_time, "decision_time")
    _require_aware(series_quality_as_of, "series_quality_as_of")
    if series_quality_as_of < decision_time:
        raise DecisionSnapshotBuildError(
            "Mốc kiểm freshness không được trước cutoff của DecisionSnapshot."
        )
    if type(workspace_id) is not UUID:
        raise DecisionSnapshotBuildError("workspace_id phải là UUID.")
    if not symbol.strip():
        raise DecisionSnapshotBuildError("symbol không được để trống.")
    if not isinstance(source_identity_required, bool):
        raise DecisionSnapshotBuildError("source_identity_required phải là bool.")
    if not source_identity_required and not (legacy_identity_namespace or "").strip():
        raise DecisionSnapshotBuildError(
            "Replay legacy cần namespace bundle để giữ provenance riêng."
        )

    try:
        requirements = feature_requirements_for_release(strategy_release_id)
    except ValueError as error:
        raise DecisionSnapshotBuildError(str(error)) from error
    if set(bars_by_timeframe) - set(requirements):
        raise DecisionSnapshotBuildError("Input chứa timeframe ngoài contract của release.")

    series: list[MarketSeries] = []
    features: list[FeatureObservation] = []
    feature_contracts: list[dict[str, object]] = []
    feature_snapshot_payloads: list[dict[str, object]] = []
    source_lineage: list[dict[str, object]] = []
    timeframe_lineage: list[DecisionSnapshotTimeframeLineage] = []

    for timeframe, feature_names in requirements.items():
        settings = feature_settings_for(timeframe)
        input_bars = tuple(bars_by_timeframe.get(timeframe, ()))
        visible_bars = tuple(
            sorted(
                (
                    bar
                    for bar in input_bars
                    if bar.is_closed
                    and bar.close_time <= decision_time
                    and bar.event_at <= decision_time
                    and bar.available_at <= decision_time
                ),
                key=lambda bar: (bar.open_time, bar.event_at, bar.source_cursor or ""),
            )
        )
        if any(bar.symbol != symbol or bar.timeframe != timeframe for bar in visible_bars):
            raise DecisionSnapshotBuildError(
                "Bar không khớp symbol/timeframe được khai báo cho DecisionSnapshot."
            )
        event_ids, provenance = _event_ids(
            visible_bars,
            workspace_id=workspace_id,
            source_identity_required=source_identity_required,
            legacy_identity_namespace=legacy_identity_namespace,
        )
        source_bar_ids = _source_bar_ids(
            visible_bars,
            legacy_identity_namespace=legacy_identity_namespace,
        )
        feature_snapshot = FeatureCalculator(settings).calculate(
            MarketSnapshot(as_of=decision_time, bars=visible_bars),
            workspace_id=workspace_id,
            input_event_ids=event_ids,
            symbol=symbol,
            timeframe=timeframe,
            computed_at=decision_time,
        )
        previous_snapshot = (
            FeatureCalculator(settings).calculate(
                MarketSnapshot(as_of=decision_time, bars=visible_bars[:-1]),
                workspace_id=workspace_id,
                input_event_ids=event_ids[:-1],
                symbol=symbol,
                timeframe=timeframe,
                computed_at=decision_time,
            )
            if visible_bars
            else None
        )

        quality_report = inspect_bar_series(
            visible_bars,
            as_of=series_quality_as_of,
            expected_interval=settings.expected_interval,
            max_event_age=settings.max_event_age,
            expected_range_start=visible_bars[0].open_time if visible_bars else None,
            expected_range_end=(
                visible_bars[-1].open_time + settings.expected_interval if visible_bars else None
            ),
            backfill_complete=True if visible_bars else False,
        )
        series_quality = _series_quality(quality_report)
        quote_volumes = quote_volumes_for_bars(visible_bars)
        series.append(
            MarketSeries(
                timeframe=timeframe,
                bars=visible_bars,
                quote_volumes=quote_volumes,
                quality=series_quality,
                expected_interval=settings.expected_interval,
                max_event_age=settings.max_event_age,
                backfill_complete=True if visible_bars else False,
            )
        )

        feature_contracts.append(
            {
                "timeframe": timeframe,
                "feature_order": list(feature_names),
                "schema_version": settings.schema_version,
                "schema_hash": feature_snapshot.schema_hash,
                "formula_version": settings.formula_version,
                "formula_hash": feature_snapshot.formula_hash,
                "code_hash": feature_snapshot.code_hash,
            }
        )
        feature_snapshot_payloads.append(feature_snapshot.canonical_payload())
        source_lineage.append({"timeframe": timeframe, "bars": list(provenance)})

        freshness = (
            DataFreshness.UNKNOWN
            if not visible_bars
            else DataFreshness.STALE
            if quality_report.stale
            else feature_snapshot.data_freshness
        )
        timeframe_lineage.append(
            DecisionSnapshotTimeframeLineage(
                workspace_id=workspace_id,
                symbol=symbol,
                timeframe=timeframe,
                feature_snapshot_id=feature_snapshot.snapshot_id,
                schema_version=settings.schema_version,
                schema_hash=feature_snapshot.schema_hash,
                formula_version=settings.formula_version,
                formula_hash=feature_snapshot.formula_hash,
                code_hash=feature_snapshot.code_hash,
                feature_order=tuple(feature_names),
                source_event_ids=event_ids,
                source_bar_ids=source_bar_ids,
                source_event_digest=_hash(
                    {
                        "timeframe": timeframe,
                        "source_event_ids": list(event_ids),
                        "source_bar_ids": list(source_bar_ids),
                    }
                ),
                as_of=decision_time.astimezone(UTC),
                freshness_as_of=series_quality_as_of.astimezone(UTC),
                available_at=(
                    max(bar.available_at for bar in visible_bars) if visible_bars else None
                ),
                freshness=freshness,
                series_quality=series_quality,
                data_coverage=feature_snapshot.data_coverage,
                warmup_by_feature=tuple(
                    (name, feature_snapshot.warmup.get(name, False)) for name in feature_names
                ),
                quality_by_feature=tuple(
                    (
                        name,
                        _feature_quality(feature_snapshot, feature_snapshot.features.get(name)),
                    )
                    for name in feature_names
                ),
                quality_flags=feature_snapshot.quality_flags,
                regime_version=REGIME_RULES_VERSION,
                regime=feature_snapshot.regime.label,
                regime_confidence=feature_snapshot.regime.confidence,
                regime_coverage=feature_snapshot.regime.coverage,
                regime_reason_code=feature_snapshot.regime.reason_code,
                source_identity_pinned=(
                    source_identity_required
                    and bool(visible_bars)
                    and all(
                        type(bar.source_identity) is MarketBarSourceIdentity for bar in visible_bars
                    )
                ),
            )
        )

        if not visible_bars:
            continue
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

    decision_time_utc = decision_time.astimezone(UTC)
    canonical_payload = {
        "schema_version": DECISION_SNAPSHOT_SCHEMA_VERSION,
        "workspace_id": str(workspace_id),
        "symbol": symbol,
        "strategy_release_id": strategy_release_id,
        "decision_time": _utc_iso(decision_time_utc),
        "freshness_as_of": _utc_iso(series_quality_as_of.astimezone(UTC)),
        "feature_contracts": feature_contracts,
        "feature_snapshots": feature_snapshot_payloads,
        "features": [
            {
                "name": item.name,
                "timeframe": item.timeframe,
                "value": item.value,
                "previous_value": item.previous_value,
                "available_at": _utc_iso(item.available_at),
                "quality": item.quality.value,
            }
            for item in features
        ],
        "series": [
            {
                "timeframe": item.timeframe,
                "quality": item.quality.value,
                "expected_interval_microseconds": _timedelta_microseconds(item.expected_interval),
                "max_event_age_microseconds": _timedelta_microseconds(item.max_event_age),
                "backfill_complete": item.backfill_complete,
                "quote_volumes": item.quote_volumes,
            }
            for item in series
        ],
        "source_lineage": source_lineage,
        "position_is_flat": position_is_flat,
        "macro_blackout": macro_blackout,
        "macro_evidence_ids": list(macro_evidence_ids),
        "macro_evidence_available_at": (
            None if macro_evidence_available_at is None else _utc_iso(macro_evidence_available_at)
        ),
        "macro_is_fresh": macro_is_fresh,
    }
    snapshot_id = _hash(canonical_payload)
    return DecisionSnapshot(
        symbol=symbol,
        decision_time=decision_time_utc,
        snapshot_id=snapshot_id,
        series=tuple(series),
        features=tuple(features),
        position_is_flat=position_is_flat,
        macro_blackout=macro_blackout,
        macro_evidence_ids=macro_evidence_ids,
        macro_evidence_available_at=macro_evidence_available_at,
        macro_is_fresh=macro_is_fresh,
        timeframe_lineage=tuple(timeframe_lineage),
        freshness_as_of=series_quality_as_of.astimezone(UTC),
    )


def canonical_market_bar_event_id(bar: OHLCVBar, *, workspace_id: UUID) -> str:
    """Tạo event reference ổn định từ identity DB-authoritative của một bar."""
    identity = bar.source_identity
    if type(identity) is not MarketBarSourceIdentity:
        raise DecisionSnapshotBuildError("Bar thiếu MarketBar source identity.")
    if identity.workspace_id != workspace_id:
        raise DecisionSnapshotBuildError("Source identity bar khác workspace.")
    if identity.checksum != bar.checksum:
        raise DecisionSnapshotBuildError("Checksum không khớp MarketBar source identity.")
    return canonical_market_bar_metadata_event_id(
        MarketBarIdentityMetadata.from_bar(bar), workspace_id=workspace_id
    )


def canonical_market_bar_metadata_event_id(
    bar: MarketBarIdentityMetadata, *, workspace_id: UUID
) -> str:
    """Băm cùng serializer nguồn từ metadata, không cần truy cập OHLCV."""
    if (
        type(bar) is not MarketBarIdentityMetadata
        or bar.source_identity.workspace_id != workspace_id
    ):
        raise DecisionSnapshotBuildError("Source identity metadata khác workspace.")
    return f"market-bar:{_hash(_source_identity_payload(bar, bar.source_identity))}"


def _event_ids(
    bars: tuple[OHLCVBar, ...],
    *,
    workspace_id: UUID,
    source_identity_required: bool,
    legacy_identity_namespace: str | None,
) -> tuple[tuple[str, ...], tuple[dict[str, object], ...]]:
    event_ids: list[str] = []
    provenance: list[dict[str, object]] = []
    identity_presence = tuple(bar.source_identity is not None for bar in bars)
    if any(identity_presence) and not all(identity_presence) and source_identity_required:
        raise DecisionSnapshotBuildError("Source identity không được thiếu trong một phần chuỗi.")

    for bar in bars:
        identity = bar.source_identity
        if source_identity_required:
            if type(identity) is not MarketBarSourceIdentity:
                raise DecisionSnapshotBuildError("Bar thiếu MarketBar source identity.")
            identity_payload = _source_identity_payload(bar, identity)
            event_id = canonical_market_bar_event_id(bar, workspace_id=workspace_id)
        else:
            identity_payload = {
                "mode": "legacy_bundle_bar_identity_v1",
                "bundle_namespace": legacy_identity_namespace,
                "symbol": bar.symbol,
                "timeframe": bar.timeframe,
                "open_time": _utc_iso(bar.open_time),
                "source": bar.source,
                "checksum": bar.checksum,
            }
            event_id = f"market-bar:{_hash(identity_payload)}"
        event_ids.append(event_id)
        provenance.append(
            {
                "identity": identity_payload,
                "event_id": event_id,
                "bar": {
                    "close_time": _utc_iso(bar.close_time),
                    "event_at": _utc_iso(bar.event_at),
                    "received_at": _utc_iso(bar.received_at),
                    "available_at": _utc_iso(bar.available_at),
                    "open": _decimal_text(bar.open),
                    "high": _decimal_text(bar.high),
                    "low": _decimal_text(bar.low),
                    "close": _decimal_text(bar.close),
                    "volume": _decimal_text(bar.volume),
                    "quote_volume": (
                        None if bar.quote_volume is None else _decimal_text(bar.quote_volume)
                    ),
                    "is_closed": bar.is_closed,
                    "source_cursor": bar.source_cursor,
                    "raw_reference": bar.raw_reference,
                },
            }
        )
    return tuple(event_ids), tuple(provenance)


def _source_identity_payload(
    bar: OHLCVBar | MarketBarIdentityMetadata, identity: MarketBarSourceIdentity
) -> dict[str, object]:
    return {
        "mode": "market_bar_source_identity_v2",
        "workspace_id": str(identity.workspace_id),
        "record_id": str(identity.record_id),
        "logical_key": identity.logical_key,
        "revision": identity.revision,
        "supersedes_record_id": (
            None if identity.supersedes_record_id is None else str(identity.supersedes_record_id)
        ),
        "checksum": identity.checksum,
        "source": bar.source,
        "raw_reference": bar.raw_reference,
        "symbol": bar.symbol,
        "timeframe": bar.timeframe,
        "open_time": _utc_iso(bar.open_time),
    }


def _source_bar_ids(
    bars: tuple[OHLCVBar, ...],
    *,
    legacy_identity_namespace: str | None,
) -> tuple[str, ...]:
    """Trả đúng record ID nguồn; legacy chỉ nhận ID bundle-local có namespace."""
    values: list[str] = []
    for bar in bars:
        identity = bar.source_identity
        if type(identity) is MarketBarSourceIdentity:
            values.append(f"market-bar-record:{identity.record_id}")
        else:
            values.append(
                "legacy-market-bar:"
                + _hash(
                    {
                        "bundle_namespace": legacy_identity_namespace,
                        "mode": "legacy_market_bar_identity_v1",
                        "symbol": bar.symbol,
                        "timeframe": bar.timeframe,
                        "open_time": _utc_iso(bar.open_time),
                        "source": bar.source,
                        "checksum": bar.checksum,
                    }
                )
            )
    return tuple(values)


def _feature_quality(snapshot: FeatureSnapshot, value: float | None) -> FeatureQuality:
    if snapshot.data_freshness is DataFreshness.STALE:
        return FeatureQuality.STALE
    if value is None:
        return FeatureQuality.MISSING
    if not snapshot.is_usable:
        return FeatureQuality.INVALID
    return FeatureQuality.READY


def _series_quality(report: BarSeriesQuality) -> SeriesQuality:
    if report.is_usable:
        return SeriesQuality.READY
    if report.stale:
        return SeriesQuality.STALE
    if report.duplicate_keys or report.future_available_count or report.unclosed_count:
        return SeriesQuality.INVALID
    return SeriesQuality.INCOMPLETE


def quote_volumes_for_bars(bars: tuple[OHLCVBar, ...]) -> tuple[float, ...] | None:
    if not bars or any(bar.quote_volume is None for bar in bars):
        return None
    values = tuple(float(bar.quote_volume) for bar in bars if bar.quote_volume is not None)
    return values if all(math.isfinite(value) for value in values) else None


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _timedelta_microseconds(value: object) -> int | None:
    if value is None:
        return None
    total_seconds = value.total_seconds()  # type: ignore[attr-defined]
    return int(total_seconds * 1_000_000)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DecisionSnapshotBuildError(f"{name} phải có múi giờ.")


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _hash(value: object) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
