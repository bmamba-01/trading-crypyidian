from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid5

import pytest

from trading_bot.application.decision_snapshot_builder import (
    DecisionSnapshotBuildError,
    build_decision_snapshot_from_bars,
    canonical_market_bar_event_id,
)
from trading_bot.domain.market.models import MarketBarSourceIdentity, OHLCVBar
from trading_bot.domain.strategy.contracts import FeatureQuality

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BASE_TIME = datetime(2026, 11, 8, 0, tzinfo=UTC)


def _bar(timeframe: str, index: int, *, record_suffix: str = "") -> OHLCVBar:
    interval = {"1h": timedelta(hours=1), "4h": timedelta(hours=4)}[timeframe]
    opened = BASE_TIME + interval * index
    closed = opened + interval - timedelta(milliseconds=1)
    checksum = f"{index + 1:064x}"
    record_id = uuid5(
        WORKSPACE_ID,
        f"{timeframe}:{opened.isoformat()}:{record_suffix}",
    )
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe=timeframe,
        open_time=opened,
        close_time=closed,
        event_at=closed,
        received_at=closed,
        available_at=closed,
        open=Decimal("100") + index,
        high=Decimal("102") + index,
        low=Decimal("99") + index,
        close=Decimal("101") + index,
        volume=Decimal("2"),
        quote_volume=Decimal("202"),
        is_closed=True,
        source="BINANCE_USDM_REST_BACKFILL",
        source_cursor=f"{timeframe}:{opened.isoformat()}",
        checksum=checksum,
        source_identity=MarketBarSourceIdentity(
            workspace_id=WORKSPACE_ID,
            record_id=record_id,
            logical_key=f"market:{timeframe}:{opened.isoformat()}",
            revision=1,
            supersedes_record_id=None,
            checksum=checksum,
        ),
    )


def _inputs() -> dict[str, tuple[OHLCVBar, ...]]:
    return {
        "1h": tuple(_bar("1h", index) for index in range(8)),
        "4h": tuple(_bar("4h", index) for index in range(2)),
    }


def test_builder_generates_stable_id_and_declared_feature_order() -> None:
    bars = _inputs()
    decision_time = BASE_TIME + timedelta(hours=8) - timedelta(milliseconds=1)

    first = build_decision_snapshot_from_bars(
        workspace_id=WORKSPACE_ID,
        symbol="BTCUSDT",
        strategy_release_id="h01-v1",
        decision_time=decision_time,
        series_quality_as_of=decision_time,
        bars_by_timeframe=bars,
        position_is_flat=True,
        source_identity_required=True,
    )
    second = build_decision_snapshot_from_bars(
        workspace_id=WORKSPACE_ID,
        symbol="BTCUSDT",
        strategy_release_id="h01-v1",
        decision_time=decision_time,
        series_quality_as_of=decision_time,
        bars_by_timeframe={"4h": bars["4h"], "1h": bars["1h"]},
        position_is_flat=True,
        source_identity_required=True,
    )

    assert first.snapshot_id == second.snapshot_id
    assert tuple(series.timeframe for series in first.series) == ("1h", "4h")
    assert tuple((item.timeframe, item.name) for item in first.features) == (
        ("1h", "ema_20"),
        ("1h", "ema_80"),
        ("1h", "adx_14"),
        ("1h", "atr_14"),
        ("4h", "ema_200"),
    )
    assert first.decision_time == decision_time
    assert first.get_series("1h").quote_volumes == (202.0,) * 8


def test_source_record_revision_is_bound_to_snapshot_identity() -> None:
    bars = _inputs()
    decision_time = BASE_TIME + timedelta(hours=8) - timedelta(milliseconds=1)
    original = build_decision_snapshot_from_bars(
        workspace_id=WORKSPACE_ID,
        symbol="BTCUSDT",
        strategy_release_id="h01-v1",
        decision_time=decision_time,
        series_quality_as_of=decision_time,
        bars_by_timeframe=bars,
        position_is_flat=True,
        source_identity_required=True,
    )
    revised_first_bar = replace(
        bars["1h"][0],
        source_identity=replace(
            bars["1h"][0].source_identity,
            record_id=uuid5(WORKSPACE_ID, "same-bar-new-record"),
        ),
    )
    revised = build_decision_snapshot_from_bars(
        workspace_id=WORKSPACE_ID,
        symbol="BTCUSDT",
        strategy_release_id="h01-v1",
        decision_time=decision_time,
        series_quality_as_of=decision_time,
        bars_by_timeframe={"1h": (revised_first_bar, *bars["1h"][1:]), "4h": bars["4h"]},
        position_is_flat=True,
        source_identity_required=True,
    )

    assert revised.snapshot_id != original.snapshot_id


def test_raw_reference_is_bound_to_event_and_snapshot_identity() -> None:
    bars = _inputs()
    decision_time = BASE_TIME + timedelta(hours=8) - timedelta(milliseconds=1)
    original_bar = bars["1h"][0]
    referenced_bar = replace(original_bar, raw_reference="raw-reference-row:123")

    assert canonical_market_bar_event_id(
        referenced_bar,
        workspace_id=WORKSPACE_ID,
    ) != canonical_market_bar_event_id(original_bar, workspace_id=WORKSPACE_ID)

    original = build_decision_snapshot_from_bars(
        workspace_id=WORKSPACE_ID,
        symbol="BTCUSDT",
        strategy_release_id="h01-v1",
        decision_time=decision_time,
        series_quality_as_of=decision_time,
        bars_by_timeframe=bars,
        position_is_flat=True,
        source_identity_required=True,
    )
    revised = build_decision_snapshot_from_bars(
        workspace_id=WORKSPACE_ID,
        symbol="BTCUSDT",
        strategy_release_id="h01-v1",
        decision_time=decision_time,
        series_quality_as_of=decision_time,
        bars_by_timeframe={
            "1h": (referenced_bar, *bars["1h"][1:]),
            "4h": bars["4h"],
        },
        position_is_flat=True,
        source_identity_required=True,
    )

    assert revised.snapshot_id != original.snapshot_id


def test_builder_rejects_wrong_workspace_or_unhydrated_required_identity() -> None:
    bars = _inputs()
    decision_time = BASE_TIME + timedelta(hours=8) - timedelta(milliseconds=1)
    wrong_workspace = replace(
        bars["1h"][0],
        source_identity=replace(
            bars["1h"][0].source_identity,
            workspace_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        ),
    )

    with pytest.raises(DecisionSnapshotBuildError, match="workspace"):
        build_decision_snapshot_from_bars(
            workspace_id=WORKSPACE_ID,
            symbol="BTCUSDT",
            strategy_release_id="h01-v1",
            decision_time=decision_time,
            series_quality_as_of=decision_time,
            bars_by_timeframe={"1h": (wrong_workspace, *bars["1h"][1:]), "4h": bars["4h"]},
            position_is_flat=True,
            source_identity_required=True,
        )

    unhydrated = replace(bars["1h"][0], source_identity=None)
    with pytest.raises(DecisionSnapshotBuildError, match="identity"):
        build_decision_snapshot_from_bars(
            workspace_id=WORKSPACE_ID,
            symbol="BTCUSDT",
            strategy_release_id="h01-v1",
            decision_time=decision_time,
            series_quality_as_of=decision_time,
            bars_by_timeframe={"1h": (unhydrated, *bars["1h"][1:]), "4h": bars["4h"]},
            position_is_flat=True,
            source_identity_required=True,
        )


def test_evaluation_freshness_is_separate_from_frozen_evidence_cutoff() -> None:
    bars = _inputs()
    evidence_cutoff = BASE_TIME + timedelta(hours=8) - timedelta(milliseconds=1)
    evaluation_time = evidence_cutoff + timedelta(hours=5)

    snapshot = build_decision_snapshot_from_bars(
        workspace_id=WORKSPACE_ID,
        symbol="BTCUSDT",
        strategy_release_id="h01-v1",
        decision_time=evidence_cutoff,
        series_quality_as_of=evaluation_time,
        bars_by_timeframe=bars,
        position_is_flat=True,
        source_identity_required=True,
    )

    assert snapshot.decision_time == evidence_cutoff
    assert snapshot.get_series("1h").quality.value == "stale"
    assert all(item.available_at <= evidence_cutoff for item in snapshot.features)
    assert FeatureQuality.STALE not in {item.quality for item in snapshot.features}
    h1_lineage = snapshot.timeframe_lineage[0]
    assert h1_lineage.as_of == evidence_cutoff
    assert h1_lineage.freshness_as_of == evaluation_time
    assert h1_lineage.series_quality.value == "stale"


def test_snapshot_hash_binds_freshness_cutoff_even_when_quality_stays_ready() -> None:
    bars = _inputs()
    evidence_cutoff = BASE_TIME + timedelta(hours=8) - timedelta(milliseconds=1)
    first = build_decision_snapshot_from_bars(
        workspace_id=WORKSPACE_ID,
        symbol="BTCUSDT",
        strategy_release_id="h01-v1",
        decision_time=evidence_cutoff,
        series_quality_as_of=evidence_cutoff,
        bars_by_timeframe=bars,
        position_is_flat=True,
        source_identity_required=True,
    )
    later_freshness_check = evidence_cutoff + timedelta(minutes=1)
    second = build_decision_snapshot_from_bars(
        workspace_id=WORKSPACE_ID,
        symbol="BTCUSDT",
        strategy_release_id="h01-v1",
        decision_time=evidence_cutoff,
        series_quality_as_of=later_freshness_check,
        bars_by_timeframe=bars,
        position_is_flat=True,
        source_identity_required=True,
    )

    assert first.get_series("1h").quality is second.get_series("1h").quality
    assert first.freshness_as_of == evidence_cutoff
    assert second.freshness_as_of == later_freshness_check
    assert first.snapshot_id != second.snapshot_id
