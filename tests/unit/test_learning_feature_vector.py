from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest

from trading_bot.application import market_signal_composition
from trading_bot.application.backtest_dataset_manifest import build_backtest_dataset_manifest
from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayInput
from trading_bot.application.backtest_dataset_replay_bundle import (
    BacktestDatasetReplayBundle,
    build_backtest_dataset_replay_bundle,
)
from trading_bot.application.backtest_dataset_replay_observations import (
    build_backtest_dataset_replay_observations,
)
from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.application.learning_feature_vector import (
    LearningFeatureVectorBuildError,
    LearningFeatureVectorBuildReason,
    _unit_for_feature,
    build_learning_feature_vector,
)
from trading_bot.application.market_signal_composition import PostgresMarketDecisionSnapshotProvider
from trading_bot.application.strategy_market_requirements import FEATURES_BY_RELEASE
from trading_bot.domain.market.models import MarketBarSourceIdentity, OHLCVBar
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    FeatureQuality,
    SeriesQuality,
)

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BASE_TIME = datetime(2026, 11, 1, tzinfo=UTC)
RELEASE_HASH = hashlib.sha256(b"h01-v1-approved-content").hexdigest()


def _bars(
    timeframe: str,
    *,
    count: int,
    last_open_time: datetime,
) -> tuple[OHLCVBar, ...]:
    bars: list[OHLCVBar] = []
    interval = {"1h": timedelta(hours=1), "4h": timedelta(hours=4)}[timeframe]
    for index in range(count):
        opened = last_open_time - interval * (count - index - 1)
        closed = opened + interval - timedelta(milliseconds=1)
        checksum = hashlib.sha256(f"bar:{index}".encode()).hexdigest()
        bars.append(
            OHLCVBar(
                symbol="BTCUSDT",
                timeframe=timeframe,
                open_time=opened,
                close_time=closed,
                event_at=closed,
                received_at=closed,
                available_at=closed,
                open=Decimal("100") + index,
                high=Decimal("103") + index,
                low=Decimal("99") + index,
                close=Decimal("102") + index,
                volume=Decimal("2"),
                quote_volume=Decimal("204"),
                is_closed=True,
                source="BINANCE_USDM_REST_BACKFILL",
                source_cursor=f"{timeframe}:{opened.isoformat()}",
                checksum=checksum,
                raw_reference=str(uuid5(WORKSPACE_ID, f"raw:{timeframe}:{opened.isoformat()}")),
                source_identity=MarketBarSourceIdentity(
                    workspace_id=WORKSPACE_ID,
                    record_id=uuid5(WORKSPACE_ID, f"{timeframe}:{opened.isoformat()}"),
                    logical_key=f"market:{timeframe}:{opened.isoformat()}",
                    revision=1,
                    supersedes_record_id=None,
                    checksum=checksum,
                ),
            )
        )
    return tuple(bars)


def _rights(checked_at: datetime) -> BackfillRightsEvidence:
    return BackfillRightsEvidence(
        workspace_id=WORKSPACE_ID,
        provider="BINANCE_USDM_REST",
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        terms_url="https://example.invalid/terms",
        policy_fingerprint=hashlib.sha256(b"fixture-policy").hexdigest(),
        verified_at=checked_at - timedelta(days=1),
        checked_at=checked_at - timedelta(seconds=1),
        valid_until=checked_at + timedelta(days=1),
    )


def _replay_bundle(
    bars_by_timeframe: dict[str, tuple[OHLCVBar, ...]],
) -> BacktestDatasetReplayBundle:
    evaluation_start = bars_by_timeframe["1h"][-4].open_time
    range_end = evaluation_start + timedelta(hours=4)
    inputs: list[BacktestDatasetReplayInput] = []
    for timeframe, bars in bars_by_timeframe.items():
        interval = {"1h": timedelta(hours=1), "4h": timedelta(hours=4)}[timeframe]
        rights = _rights(range_end)
        request = RestBackfillRequest(
            symbol="BTCUSDT",
            interval=timeframe,
            start=bars[0].open_time,
            end=range_end,
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        )
        manifest = build_backtest_dataset_manifest(
            request,
            evaluation_start=evaluation_start,
            warmup_bars=int((evaluation_start - bars[0].open_time) / interval),
            bars=bars,
            rights_evidence=rights,
            prepared_at=range_end,
            schema_version=4,
        )
        inputs.append(BacktestDatasetReplayInput(manifest, bars, rights))
    return build_backtest_dataset_replay_bundle(tuple(inputs))


def _market_row(bar: OHLCVBar) -> SimpleNamespace:
    identity = bar.source_identity
    assert identity is not None
    return SimpleNamespace(
        id=identity.record_id,
        workspace_id=identity.workspace_id,
        logical_key=identity.logical_key,
        revision=identity.revision,
        supersedes_id=identity.supersedes_record_id,
        symbol=bar.symbol,
        timeframe=bar.timeframe,
        open_time=bar.open_time,
        close_time=bar.close_time,
        event_at=bar.event_at,
        received_at=bar.received_at,
        available_at=bar.available_at,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        quote_volume=bar.quote_volume,
        is_closed=bar.is_closed,
        source=bar.source,
        source_cursor=bar.source_cursor,
        checksum=bar.checksum,
        raw_reference_id=None if bar.raw_reference is None else UUID(bar.raw_reference),
    )


class _FakeUnitOfWork:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.session = object()

    async def __aenter__(self) -> _FakeUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


def _unsafe_replace(instance: object, **changes: object) -> object:
    clone = object.__new__(type(instance))
    for name in instance.__dataclass_fields__:
        object.__setattr__(clone, name, changes.get(name, getattr(instance, name)))
    return clone


def _with_first_lineage(
    snapshot: DecisionSnapshot,
    lineage: object,
    *,
    unsafe: bool = False,
) -> DecisionSnapshot:
    values = (lineage, *snapshot.timeframe_lineage[1:])
    if unsafe:
        return _unsafe_replace(snapshot, timeframe_lineage=values)  # type: ignore[return-value]
    return replace(snapshot, timeframe_lineage=values)  # type: ignore[arg-type]


@pytest.fixture
async def runtime_replay_snapshots(monkeypatch: pytest.MonkeyPatch) -> tuple[DecisionSnapshot, ...]:
    bars_by_timeframe = {
        "1h": _bars(
            "1h",
            count=224,
            last_open_time=BASE_TIME + timedelta(hours=223),
        ),
        "4h": _bars(
            "4h",
            count=206,
            last_open_time=BASE_TIME + timedelta(hours=220),
        ),
    }
    replay = build_backtest_dataset_replay_observations(
        _replay_bundle(bars_by_timeframe),
        strategy_release_id="h01-v1",
        position_is_flat_at=lambda _time: True,
    )[-1].snapshot
    evaluation_time = replay.decision_time
    rows = tuple(
        _market_row(bar) for timeframe in ("1h", "4h") for bar in bars_by_timeframe[timeframe]
    )

    async def latest_rows(_session: object, **_kwargs: object) -> tuple[SimpleNamespace, ...]:
        return rows

    monkeypatch.setattr(market_signal_composition, "UnitOfWork", _FakeUnitOfWork)
    monkeypatch.setattr(market_signal_composition, "_latest_bar_rows", latest_rows)
    runtime = await PostgresMarketDecisionSnapshotProvider(object()).snapshot_for(
        evidence=SimpleNamespace(
            workspace_id=WORKSPACE_ID,
            symbol="BTCUSDT",
            observed_at=evaluation_time,
        ),
        context=SimpleNamespace(workspace_id=WORKSPACE_ID, strategy_release_id="h01-v1"),
        evaluated_at=evaluation_time,
    )
    return runtime, replay


def test_vector_builder_fails_closed_for_legacy_snapshot_without_timeframe_lineage() -> None:
    snapshot = DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=datetime(2026, 11, 8, tzinfo=UTC),
        snapshot_id="legacy-snapshot",
        series=(),
        features=(),
    )

    with pytest.raises(LearningFeatureVectorBuildError) as raised:
        build_learning_feature_vector(
            snapshot,
            strategy_release_id="h01-v1",
            strategy_release_content_hash="a" * 64,
        )

    assert raised.value.reason_code is LearningFeatureVectorBuildReason.MISSING_LINEAGE


@pytest.mark.asyncio
async def test_runtime_and_replay_build_identical_learning_feature_vectors(
    runtime_replay_snapshots: tuple[DecisionSnapshot, ...],
) -> None:
    runtime, replay = runtime_replay_snapshots

    runtime_vector = build_learning_feature_vector(
        runtime,
        strategy_release_id="h01-v1",
        strategy_release_content_hash=RELEASE_HASH,
    )
    replay_vector = build_learning_feature_vector(
        replay,
        strategy_release_id="h01-v1",
        strategy_release_content_hash=RELEASE_HASH,
    )

    assert runtime_vector == replay_vector
    assert runtime_vector.snapshot_id == runtime.snapshot_id == replay.snapshot_id
    assert runtime_vector.snapshot_hash == runtime.snapshot_id
    assert runtime_vector.feature_schema_hash == replay_vector.feature_schema_hash
    assert runtime_vector.vector_hash == replay_vector.vector_hash
    assert runtime_vector.timeframe_lineage == replay_vector.timeframe_lineage
    assert tuple(feature.key for feature in runtime_vector.features) == (
        "1h:ema_20",
        "1h:ema_80",
        "1h:adx_14",
        "1h:atr_14",
        "4h:ema_200",
    )
    assert tuple(item.timeframe for item in runtime_vector.timeframe_lineage) == ("1h", "4h")
    assert runtime.freshness_as_of == replay.freshness_as_of
    assert runtime_vector.timeframe_lineage[0].freshness_as_of == runtime.freshness_as_of
    assert all(item.feature_snapshot_id for item in runtime_vector.timeframe_lineage)
    assert all(item.source_event_digest for item in runtime_vector.timeframe_lineage)
    for item in runtime_vector.timeframe_lineage:
        assert len(item.source_event_ids) == len(item.source_bar_ids)
        assert all(source_id.startswith("market-bar-record:") for source_id in item.source_bar_ids)
        assert item.as_of == runtime.decision_time
        assert item.freshness_as_of == runtime.decision_time
    assert not hasattr(runtime_vector, "source_rights_fingerprint")


@pytest.mark.asyncio
async def test_vector_and_lineage_reject_mutable_list_fields(
    runtime_replay_snapshots: tuple[DecisionSnapshot, ...],
) -> None:
    snapshot, _ = runtime_replay_snapshots
    vector = build_learning_feature_vector(
        snapshot,
        strategy_release_id="h01-v1",
        strategy_release_content_hash=RELEASE_HASH,
    )
    lineage = snapshot.timeframe_lineage[0]

    for replace_call in (
        lambda: replace(lineage, feature_order=list(lineage.feature_order)),
        lambda: replace(lineage, source_event_ids=list(lineage.source_event_ids)),
        lambda: replace(lineage, source_bar_ids=list(lineage.source_bar_ids)),
        lambda: replace(lineage, warmup_by_feature=list(lineage.warmup_by_feature)),
        lambda: replace(lineage, quality_by_feature=list(lineage.quality_by_feature)),
        lambda: replace(lineage, quality_flags=list(lineage.quality_flags)),
        lambda: replace(vector, feature_keys=list(vector.feature_keys)),
        lambda: replace(vector, features=list(vector.features)),
        lambda: replace(vector, timeframe_lineage=list(vector.timeframe_lineage)),
    ):
        with pytest.raises(ValueError, match="tuple bất biến"):
            replace_call()


def test_vector_hash_binds_release_content_hash_but_schema_hash_does_not(
    runtime_replay_snapshots: tuple[DecisionSnapshot, ...],
) -> None:
    snapshot, _ = runtime_replay_snapshots
    first = build_learning_feature_vector(
        snapshot,
        strategy_release_id="h01-v1",
        strategy_release_content_hash=RELEASE_HASH,
    )
    changed_release = build_learning_feature_vector(
        snapshot,
        strategy_release_id="h01-v1",
        strategy_release_content_hash=hashlib.sha256(b"changed release").hexdigest(),
    )

    assert changed_release.feature_schema_hash == first.feature_schema_hash
    assert changed_release.vector_hash != first.vector_hash
    with pytest.raises(ValueError, match="feature_schema_hash không khớp"):
        replace(first, feature_schema_hash="0" * 64)


def test_integer_feature_input_has_same_float64_canonical_vector_hash(
    runtime_replay_snapshots: tuple[DecisionSnapshot, ...],
) -> None:
    snapshot, _ = runtime_replay_snapshots
    vector = build_learning_feature_vector(
        snapshot,
        strategy_release_id="h01-v1",
        strategy_release_content_hash=RELEASE_HASH,
    )
    feature_index = next(
        index for index, feature in enumerate(vector.features) if feature.name == "atr_14"
    )
    feature = vector.features[feature_index]
    assert feature.value.is_integer()
    integer_feature = replace(feature, value=int(feature.value))
    integer_features = (
        *vector.features[:feature_index],
        integer_feature,
        *vector.features[feature_index + 1 :],
    )
    integer_vector = replace(vector, features=integer_features)

    assert integer_vector.vector_hash == vector.vector_hash
    assert type(integer_feature.value) is float


def test_vector_builder_rejects_tampered_bar_with_original_snapshot_id(
    runtime_replay_snapshots: tuple[DecisionSnapshot, ...],
) -> None:
    snapshot, _ = runtime_replay_snapshots
    first_series = snapshot.series[0]
    bars = list(first_series.bars)
    bars[-1] = replace(bars[-1], close=bars[-1].close + Decimal("0.25"))
    changed_series = replace(first_series, bars=tuple(bars))
    tampered = replace(snapshot, series=(changed_series, *snapshot.series[1:]))

    with pytest.raises(LearningFeatureVectorBuildError) as raised:
        build_learning_feature_vector(
            tampered,
            strategy_release_id="h01-v1",
            strategy_release_content_hash=RELEASE_HASH,
        )

    assert raised.value.reason_code.value == "SNAPSHOT_CONTENT_MISMATCH"
    assert tampered.snapshot_id == snapshot.snapshot_id


def test_vector_builder_rejects_tampered_raw_reference_with_original_snapshot_id(
    runtime_replay_snapshots: tuple[DecisionSnapshot, ...],
) -> None:
    snapshot, _ = runtime_replay_snapshots
    first_series = snapshot.series[0]
    bars = list(first_series.bars)
    original_bar = bars[-1]
    bars[-1] = replace(original_bar, raw_reference="raw-reference-row:tampered")
    assert bars[-1].source_identity == original_bar.source_identity
    assert bars[-1].checksum == original_bar.checksum
    changed_series = replace(first_series, bars=tuple(bars))
    tampered = replace(snapshot, series=(changed_series, *snapshot.series[1:]))

    with pytest.raises(LearningFeatureVectorBuildError) as raised:
        build_learning_feature_vector(
            tampered,
            strategy_release_id="h01-v1",
            strategy_release_content_hash=RELEASE_HASH,
        )

    assert raised.value.reason_code.value == "SNAPSHOT_CONTENT_MISMATCH"
    assert tampered.snapshot_id == snapshot.snapshot_id


def test_vector_builder_rejects_tampered_feature_value_with_original_snapshot_id(
    runtime_replay_snapshots: tuple[DecisionSnapshot, ...],
) -> None:
    snapshot, _ = runtime_replay_snapshots
    changed_feature = replace(snapshot.features[0], value=snapshot.features[0].value + 0.25)
    tampered = replace(snapshot, features=(changed_feature, *snapshot.features[1:]))

    with pytest.raises(LearningFeatureVectorBuildError) as raised:
        build_learning_feature_vector(
            tampered,
            strategy_release_id="h01-v1",
            strategy_release_content_hash=RELEASE_HASH,
        )

    assert raised.value.reason_code.value == "SNAPSHOT_CONTENT_MISMATCH"
    assert tampered.snapshot_id == snapshot.snapshot_id


def test_vector_builder_rejects_tampered_source_lineage_with_original_snapshot_id(
    runtime_replay_snapshots: tuple[DecisionSnapshot, ...],
) -> None:
    snapshot, _ = runtime_replay_snapshots
    lineage = snapshot.timeframe_lineage[0]
    source_event_ids = (f"{lineage.source_event_ids[0]}:changed", *lineage.source_event_ids[1:])
    digest_payload = {
        "timeframe": lineage.timeframe,
        "source_event_ids": list(source_event_ids),
        "source_bar_ids": list(lineage.source_bar_ids),
    }
    digest = hashlib.sha256(
        json.dumps(
            digest_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    changed_lineage = replace(
        lineage,
        source_event_ids=source_event_ids,
        source_event_digest=digest,
    )
    tampered = replace(
        snapshot,
        timeframe_lineage=(changed_lineage, *snapshot.timeframe_lineage[1:]),
    )

    with pytest.raises(LearningFeatureVectorBuildError) as raised:
        build_learning_feature_vector(
            tampered,
            strategy_release_id="h01-v1",
            strategy_release_content_hash=RELEASE_HASH,
        )

    assert raised.value.reason_code.value == "SNAPSHOT_CONTENT_MISMATCH"
    assert tampered.snapshot_id == snapshot.snapshot_id


def test_vector_builder_rejects_freshness_cutoff_tampering_with_original_snapshot_id(
    runtime_replay_snapshots: tuple[DecisionSnapshot, ...],
) -> None:
    snapshot, _ = runtime_replay_snapshots
    freshness_cutoff = snapshot.timeframe_lineage[0].freshness_as_of + timedelta(minutes=1)
    changed_lineage = tuple(
        replace(item, freshness_as_of=freshness_cutoff) for item in snapshot.timeframe_lineage
    )
    tampered = replace(
        snapshot,
        timeframe_lineage=changed_lineage,
        freshness_as_of=freshness_cutoff,
    )

    with pytest.raises(LearningFeatureVectorBuildError) as raised:
        build_learning_feature_vector(
            tampered,
            strategy_release_id="h01-v1",
            strategy_release_content_hash=RELEASE_HASH,
        )

    assert raised.value.reason_code.value == "SNAPSHOT_CONTENT_MISMATCH"
    assert tampered.snapshot_id == snapshot.snapshot_id


def test_decision_snapshot_and_market_series_copy_mutable_collection_inputs(
    runtime_replay_snapshots: tuple[DecisionSnapshot, ...],
) -> None:
    original, _ = runtime_replay_snapshots
    mutable_series = list(original.series)
    mutable_features = list(original.features)
    mutable_macro_ids = list(original.macro_evidence_ids) or ["macro-evidence-fixture"]
    mutable_lineage = list(original.timeframe_lineage)
    first_series = mutable_series[0]
    mutable_bars = list(first_series.bars)
    mutable_quotes = list(first_series.quote_volumes or ())
    mutable_series[0] = replace(
        first_series,
        bars=mutable_bars,
        quote_volumes=mutable_quotes,
    )
    copied = DecisionSnapshot(
        symbol=original.symbol,
        decision_time=original.decision_time,
        snapshot_id=original.snapshot_id,
        series=mutable_series,
        features=mutable_features,
        position_is_flat=original.position_is_flat,
        macro_blackout=original.macro_blackout,
        macro_evidence_ids=mutable_macro_ids,
        macro_evidence_available_at=original.macro_evidence_available_at,
        macro_is_fresh=original.macro_is_fresh,
        timeframe_lineage=mutable_lineage,
        freshness_as_of=original.freshness_as_of,
    )

    mutable_series.clear()
    mutable_features.clear()
    mutable_macro_ids.clear()
    mutable_lineage.clear()
    mutable_bars.clear()
    mutable_quotes.clear()

    assert isinstance(copied.series, tuple)
    assert isinstance(copied.features, tuple)
    assert isinstance(copied.macro_evidence_ids, tuple)
    assert isinstance(copied.timeframe_lineage, tuple)
    assert copied.series
    assert copied.features
    assert copied.macro_evidence_ids == ("macro-evidence-fixture",)
    assert copied.timeframe_lineage
    assert isinstance(copied.series[0].bars, tuple)
    assert isinstance(copied.series[0].quote_volumes, tuple)
    assert copied.series[0].bars
    assert copied.series[0].quote_volumes


@pytest.mark.asyncio
async def test_vector_builder_returns_reason_code_for_contract_mismatches(
    runtime_replay_snapshots: tuple[DecisionSnapshot, ...],
) -> None:
    snapshot, _ = runtime_replay_snapshots
    lineage = snapshot.timeframe_lineage[0]
    changed_feature = replace(
        snapshot.features[0], available_at=snapshot.decision_time + timedelta(seconds=1)
    )
    future = replace(snapshot, features=(changed_feature, *snapshot.features[1:]))
    reordered_features = replace(
        snapshot,
        features=(snapshot.features[1], snapshot.features[0], *snapshot.features[2:]),
    )
    stale_lineage = replace(lineage, series_quality=SeriesQuality.STALE)
    stale = _with_first_lineage(snapshot, stale_lineage)
    stale_feature = replace(
        snapshot,
        features=(
            replace(snapshot.features[0], quality=FeatureQuality.STALE),
            *snapshot.features[1:],
        ),
    )
    unpinned_lineage = replace(lineage, source_identity_pinned=False)
    unpinned = _with_first_lineage(snapshot, unpinned_lineage)
    low_coverage = _with_first_lineage(snapshot, replace(lineage, data_coverage=0.5))
    missing_features = replace(snapshot, features=())
    duplicate_features = _unsafe_replace(
        snapshot,
        features=(snapshot.features[0], snapshot.features[0]),
    )
    nonfinite_feature = _unsafe_replace(
        snapshot.features[0],
        value=float("inf"),
    )
    nonfinite = _unsafe_replace(
        snapshot,
        features=(nonfinite_feature, *snapshot.features[1:]),
    )
    future_source = _with_first_lineage(
        snapshot,
        replace(lineage, available_at=snapshot.decision_time + timedelta(seconds=1)),
    )
    warmup_incomplete = _with_first_lineage(
        snapshot,
        replace(lineage, warmup_by_feature=(("ema_20", False), *lineage.warmup_by_feature[1:])),
    )
    duplicate_lineage = _unsafe_replace(
        snapshot,
        timeframe_lineage=(lineage, lineage, *snapshot.timeframe_lineage[1:]),
    )
    missing_source = _unsafe_replace(
        snapshot,
        timeframe_lineage=(
            _unsafe_replace(lineage, source_event_ids=(), source_bar_ids=()),
            *snapshot.timeframe_lineage[1:],
        ),
    )
    duplicate_source = _unsafe_replace(
        snapshot,
        timeframe_lineage=(
            _unsafe_replace(
                lineage,
                source_event_ids=(lineage.source_event_ids[0], lineage.source_event_ids[0]),
                source_bar_ids=(lineage.source_bar_ids[0], lineage.source_bar_ids[0]),
            ),
            *snapshot.timeframe_lineage[1:],
        ),
    )
    bad_digest = _unsafe_replace(
        snapshot,
        timeframe_lineage=(
            _unsafe_replace(lineage, source_event_digest="0" * 64),
            *snapshot.timeframe_lineage[1:],
        ),
    )
    wrong_schema_order = _unsafe_replace(
        snapshot,
        timeframe_lineage=(
            _unsafe_replace(lineage, feature_order=("wrong_feature",)),
            *snapshot.timeframe_lineage[1:],
        ),
    )
    missing_timeframe_snapshot_id = _unsafe_replace(
        snapshot,
        timeframe_lineage=(
            _unsafe_replace(lineage, feature_snapshot_id=""),
            *snapshot.timeframe_lineage[1:],
        ),
    )

    cases = (
        (future, LearningFeatureVectorBuildReason.FUTURE_FEATURE),
        (reordered_features, LearningFeatureVectorBuildReason.FEATURE_ORDER_MISMATCH),
        (future_source, LearningFeatureVectorBuildReason.FUTURE_SOURCE),
        (stale, LearningFeatureVectorBuildReason.STALE_SOURCE),
        (stale_feature, LearningFeatureVectorBuildReason.STALE_SOURCE),
        (unpinned, LearningFeatureVectorBuildReason.UNPINNED_SOURCE),
        (low_coverage, LearningFeatureVectorBuildReason.LOW_COVERAGE),
        (missing_features, LearningFeatureVectorBuildReason.MISSING_FEATURE),
        (duplicate_features, LearningFeatureVectorBuildReason.DUPLICATE_FEATURE),
        (nonfinite, LearningFeatureVectorBuildReason.NON_FINITE_FEATURE),
        (warmup_incomplete, LearningFeatureVectorBuildReason.WARMUP_INCOMPLETE),
        (duplicate_lineage, LearningFeatureVectorBuildReason.DUPLICATE_LINEAGE),
        (missing_source, LearningFeatureVectorBuildReason.MISSING_PROVENANCE),
        (duplicate_source, LearningFeatureVectorBuildReason.DUPLICATE_PROVENANCE),
        (bad_digest, LearningFeatureVectorBuildReason.SOURCE_DIGEST_MISMATCH),
        (wrong_schema_order, LearningFeatureVectorBuildReason.FEATURE_SCHEMA_MISMATCH),
        (
            missing_timeframe_snapshot_id,
            LearningFeatureVectorBuildReason.MISSING_PROVENANCE,
        ),
    )
    for candidate, expected_reason in cases:
        with pytest.raises(LearningFeatureVectorBuildError) as raised:
            build_learning_feature_vector(
                candidate,
                strategy_release_id="h01-v1",
                strategy_release_content_hash=RELEASE_HASH,
            )
        assert raised.value.reason_code is expected_reason


def test_feature_vector_rejects_noncanonical_release_hash() -> None:
    snapshot = DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=datetime(2026, 11, 8, tzinfo=UTC),
        snapshot_id="legacy-snapshot",
        series=(),
        features=(),
    )
    with pytest.raises(LearningFeatureVectorBuildError) as raised:
        build_learning_feature_vector(
            snapshot,
            strategy_release_id="h01-v1",
            strategy_release_content_hash="not-a-hash",
        )
    assert raised.value.reason_code is LearningFeatureVectorBuildReason.INVALID_RELEASE_HASH


def test_every_builtin_strategy_feature_has_an_explicit_vector_unit() -> None:
    feature_names = {
        name
        for requirements in FEATURES_BY_RELEASE.values()
        for timeframe_features in requirements.values()
        for name in timeframe_features
    }
    assert all(_unit_for_feature(name) for name in feature_names)
