from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from trading_bot.application.backtest import validate_replay_observation
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
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.strategy.contracts import SeriesQuality

NOW = datetime(2026, 11, 3, 8, tzinfo=UTC)
START = datetime(2026, 11, 3, 0, tzinfo=UTC)
EVALUATION_START = datetime(2026, 11, 3, 4, tzinfo=UTC)
RANGE_END = NOW
WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")
POLICY_FINGERPRINT = hashlib.sha256(b"replay-observation-policy").hexdigest()


def evidence() -> BackfillRightsEvidence:
    return BackfillRightsEvidence(
        workspace_id=WORKSPACE_ID,
        provider="BINANCE_USDM_REST",
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        terms_url="https://example.invalid/terms",
        policy_fingerprint=POLICY_FINGERPRINT,
        verified_at=NOW - timedelta(days=1),
        checked_at=NOW - timedelta(seconds=1),
        valid_until=NOW + timedelta(days=1),
    )


def interval_for(timeframe: str) -> timedelta:
    return {
        "1m": timedelta(minutes=1),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
    }[timeframe]


def bars_for(timeframe: str, *, late_last_bar: bool = False) -> tuple[OHLCVBar, ...]:
    interval = interval_for(timeframe)
    count = int((RANGE_END - START) / interval)
    bars: list[OHLCVBar] = []
    for index in range(count):
        open_time = START + index * interval
        close_time = open_time + interval - timedelta(milliseconds=1)
        available_at = close_time
        if late_last_bar and index == count - 1:
            available_at = RANGE_END + timedelta(seconds=1)
        price = Decimal(100 + index)
        bars.append(
            OHLCVBar(
                symbol="BTCUSDT",
                timeframe=timeframe,
                open_time=open_time,
                close_time=close_time,
                event_at=close_time,
                received_at=available_at,
                available_at=available_at,
                open=price,
                high=price + Decimal("2"),
                low=price - Decimal("1"),
                close=price + Decimal("1"),
                volume=Decimal("1.25"),
                quote_volume=Decimal("125.5"),
                is_closed=True,
                source="BINANCE_USDM_REST_BACKFILL",
                source_cursor=f"{timeframe}:{open_time.isoformat()}",
                checksum=hashlib.sha256(
                    f"{timeframe}:{open_time.isoformat()}".encode()
                ).hexdigest(),
            )
        )
    return tuple(bars)


def replay_input(
    timeframe: str,
    *,
    bars: tuple[OHLCVBar, ...] | None = None,
) -> BacktestDatasetReplayInput:
    interval = interval_for(timeframe)
    observed = bars if bars is not None else bars_for(timeframe)
    if not observed:
        raise ValueError("Fixture replay cần ít nhất một bar.")
    warmup_bars = int((EVALUATION_START - observed[0].open_time) / interval)
    request = RestBackfillRequest(
        symbol="BTCUSDT",
        interval=timeframe,
        start=observed[0].open_time,
        end=RANGE_END,
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
    )
    manifest = build_backtest_dataset_manifest(
        request,
        evaluation_start=EVALUATION_START,
        warmup_bars=warmup_bars,
        bars=observed,
        rights_evidence=evidence(),
        prepared_at=NOW
        + (
            timedelta(seconds=2)
            if any(bar.available_at > NOW for bar in observed)
            else timedelta(0)
        ),
    )
    return BacktestDatasetReplayInput(manifest, observed, evidence())


def bundle(
    *,
    timeframes: tuple[str, ...] = ("1h", "4h"),
    late_entry: bool = False,
    missing_quote_volume: bool = False,
    no_higher_timeframe_warmup: bool = False,
) -> BacktestDatasetReplayBundle:
    inputs: list[BacktestDatasetReplayInput] = []
    for timeframe in timeframes:
        bars = bars_for(timeframe, late_last_bar=late_entry and timeframe == "1h")
        if no_higher_timeframe_warmup and timeframe == "4h":
            bars = tuple(bar for bar in bars if bar.open_time >= EVALUATION_START)
        if missing_quote_volume and timeframe == "1h":
            bars = (replace(bars[0], quote_volume=None), *bars[1:])
        inputs.append(replay_input(timeframe, bars=bars))
    return build_backtest_dataset_replay_bundle(tuple(inputs))


def test_h01_observations_filter_higher_timeframe_bars_by_available_time() -> None:
    observations = build_backtest_dataset_replay_observations(
        bundle(),
        strategy_release_id="h01-v1",
        position_is_flat_at=lambda _time: True,
    )

    assert len(observations) == 4
    first = observations[0]
    assert first.event_at == START + timedelta(hours=5) - timedelta(milliseconds=1)
    assert first.available_at == first.snapshot.decision_time
    assert first.snapshot.position_is_flat is True
    assert first.snapshot.macro_blackout is None
    assert tuple(bar.open_time for bar in first.snapshot.get_series("1h").bars) == tuple(
        START + timedelta(hours=index) for index in range(5)
    )
    assert tuple(bar.open_time for bar in first.snapshot.get_series("4h").bars) == (START,)
    assert first.snapshot.get_series("1h").quality is SeriesQuality.READY
    assert observations[-1].snapshot.get_series("4h").bars[-1].open_time == EVALUATION_START
    validate_replay_observation(first)


def test_builder_marks_unclosed_higher_timeframe_warmup_incomplete_until_available() -> None:
    observations = build_backtest_dataset_replay_observations(
        bundle(no_higher_timeframe_warmup=True),
        strategy_release_id="h01-v1",
    )

    assert observations[0].snapshot.get_series("4h").bars == ()
    assert observations[0].snapshot.get_series("4h").quality is SeriesQuality.INCOMPLETE
    assert observations[-1].snapshot.get_series("4h").bars[-1].open_time == EVALUATION_START


def test_builder_produces_deterministic_snapshot_and_event_ids() -> None:
    source = bundle()
    first = build_backtest_dataset_replay_observations(
        source,
        strategy_release_id="h01-v1",
        position_is_flat_at=lambda _time: True,
    )
    second = build_backtest_dataset_replay_observations(
        source,
        strategy_release_id="h01-v1",
        position_is_flat_at=lambda _time: True,
    )

    assert tuple(item.event_id for item in first) == tuple(item.event_id for item in second)
    assert tuple(item.snapshot.snapshot_id for item in first) == tuple(
        item.snapshot.snapshot_id for item in second
    )


def test_builder_keeps_missing_quote_volume_and_unknown_runtime_context_fail_closed() -> None:
    observations = build_backtest_dataset_replay_observations(
        bundle(missing_quote_volume=True),
        strategy_release_id="h03-v1",
    )

    assert observations
    first = observations[0].snapshot
    assert first.get_series("1h").quote_volumes is None
    assert first.position_is_flat is None
    assert first.macro_blackout is None
    assert first.macro_evidence_ids == ()


def test_builder_rejects_late_entry_event_outside_evaluation_range() -> None:
    with pytest.raises(ValueError, match="available sau range_end"):
        build_backtest_dataset_replay_observations(
            bundle(timeframes=("1h",), late_entry=True),
            strategy_release_id="h03-v1",
        )


def test_builder_rejects_unsupported_release_and_empty_decision_timeframe() -> None:
    with pytest.raises(ValueError, match="release chưa được hỗ trợ"):
        build_backtest_dataset_replay_observations(
            bundle(timeframes=("1h",)),
            strategy_release_id="future-v1",
        )

    with pytest.raises(ValueError, match="thiếu timeframe quyết định"):
        build_backtest_dataset_replay_observations(
            bundle(timeframes=("4h",)),
            strategy_release_id="h01-v1",
        )

    with pytest.raises(ValueError, match="Bundle thiếu timeframe bắt buộc 4h"):
        build_backtest_dataset_replay_observations(
            bundle(timeframes=("1h",)),
            strategy_release_id="h01-v1",
        )
